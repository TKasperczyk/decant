"""Core compaction logic: boundary finding, summarization, and splicing."""

from __future__ import annotations

import json
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .auth import CLAUDE_CODE_SYSTEM_PROMPT
from .models import (
    BOUNDARY_TRANSCRIPT_MAX_CHARS,
    MODELS,
    SUMMARY_MAX_TOKENS,
    SUMMARY_TRANSCRIPT_MAX_CHARS,
)
from .session import (
    Exchange,
    collect_tail_uuids,
    extract_exchanges,
    get_session_metadata,
    load_messages,
    save_messages,
    walk_main_chain,
)


def _format_exchanges_for_llm(exchanges: list[Exchange], max_chars: int) -> str:
    """Format exchanges into a labeled transcript for LLM consumption.

    If the transcript exceeds max_chars, keeps beginning and end sections
    to avoid biasing topic detection toward early messages only.
    """
    all_lines = [
        f"[MSG uuid={ex.uuid}] {ex.role.upper()}: {ex.text}"
        for ex in exchanges
    ]
    full = "\n\n".join(all_lines)
    if len(full) <= max_chars:
        return full

    # Keep beginning and end for balanced topic detection
    half = max_chars // 2
    return (
        full[:half]
        + "\n\n... [middle section truncated due to length] ...\n\n"
        + full[-half:]
    )


def find_boundary_by_topic(
    exchanges: list[Exchange],
    topic: str,
    client: anthropic.Anthropic,
    model: str,
) -> str:
    """Use an LLM to find the first message matching a topic.

    Returns the UUID of the boundary message (first message of the section to keep).
    """
    transcript = _format_exchanges_for_llm(exchanges, BOUNDARY_TRANSCRIPT_MAX_CHARS)

    system = (
        f"{CLAUDE_CODE_SYSTEM_PROMPT}\n\n"
        "You are a conversation analyst. You will be given a transcript of a "
        "Claude Code session with labeled message UUIDs. Your job is to find "
        "the first message where the conversation shifts to the specified topic.\n\n"
        "Rules:\n"
        "- Return ONLY the UUID of the first message that matches the topic\n"
        "- If the topic spans multiple messages, return the UUID of the FIRST one\n"
        "- If the topic is discussed from the very start, return the UUID of the first message\n"
        "- If the topic is never discussed, return 'NOT_FOUND'\n"
        "- Return nothing else, just the UUID or NOT_FOUND"
    )

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"Find the first message about this topic: {topic}\n\n"
                f"Transcript:\n{transcript}"
            ),
        }],
    )

    result = response.content[0].text.strip()

    if result == "NOT_FOUND":
        raise ValueError(f"Topic '{topic}' not found in the conversation.")

    # Validate it's a real UUID from our exchanges
    valid_uuids = {ex.uuid for ex in exchanges}
    if result not in valid_uuids:
        # Try to extract a UUID from the response
        for token in result.split():
            clean = token.strip("'\"`,.")
            if clean in valid_uuids:
                return clean
        raise ValueError(
            f"LLM returned an invalid UUID: {result}. "
            f"Try rephrasing the topic or use --last N instead."
        )

    return result


def find_boundary_by_count(
    messages: list[dict],
    count: int,
) -> str:
    """Find the boundary by keeping the last N user turns.

    Returns the UUID of the boundary message (first message to keep).
    """
    exchanges = extract_exchanges(messages)
    user_exchanges = [ex for ex in exchanges if ex.role == "user"]

    if count >= len(user_exchanges):
        raise ValueError(
            f"Requested to keep last {count} user turns, "
            f"but only {len(user_exchanges)} exist. Nothing to compact."
        )

    if count <= 0:
        raise ValueError("--last must be a positive integer.")

    # The boundary is the (len - count)th user exchange
    boundary_exchange = user_exchanges[-(count)]
    return boundary_exchange.uuid


def summarize_head(
    messages: list[dict],
    boundary_uuid: str,
    client: anthropic.Anthropic,
    model: str,
) -> str:
    """Summarize all messages before the boundary.

    Extracts a detailed transcript of the head section and asks the LLM
    to produce a structured summary.
    """
    # Build head-only transcript
    chain = walk_main_chain(messages)
    head_msgs = []
    for msg in chain:
        if msg.get("uuid") == boundary_uuid:
            break
        head_msgs.append(msg)

    if not head_msgs:
        return "[No content before boundary to summarize.]"

    # Extract transcript from head messages only
    transcript_lines = []
    for msg in head_msgs:
        msg_type = msg.get("type")
        if msg_type not in ("user", "assistant"):
            continue
        inner = msg.get("message", {})
        role = inner.get("role", "")
        content = inner.get("content")
        if not content:
            continue

        if isinstance(content, str):
            transcript_lines.append(f"[{role.upper()}]: {content}")
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        if name == "Bash":
                            parts.append(f"  [Bash: {str(inp.get('command', '?'))[:120]}]")
                        elif name in ("Read", "Write", "Edit"):
                            parts.append(f"  [{name}: {inp.get('file_path', '?')}]")
                        elif name == "Task":
                            parts.append(f"  [Task: {inp.get('description', '?')}]")
                        else:
                            parts.append(f"  [{name}]")
                    elif btype == "tool_result":
                        is_err = block.get("is_error", False)
                        rc = block.get("content", "")
                        preview = rc[:150] if isinstance(rc, str) else str(rc)[:150]
                        parts.append(f"  [result{'(ERR)' if is_err else ''}: {preview}...]")
            if parts:
                transcript_lines.append(f"[{role.upper()}]: " + "\n".join(parts))

    transcript = "\n\n".join(transcript_lines)

    # Truncate if too long
    if len(transcript) > SUMMARY_TRANSCRIPT_MAX_CHARS:
        # Keep beginning and end for context
        half = SUMMARY_TRANSCRIPT_MAX_CHARS // 2
        transcript = (
            transcript[:half]
            + "\n\n... [middle section truncated] ...\n\n"
            + transcript[-half:]
        )

    system = (
        f"{CLAUDE_CODE_SYSTEM_PROMPT}\n\n"
        "You are a conversation summarizer. Given a transcript of a Claude Code "
        "session, produce a concise but thorough summary that captures:\n\n"
        "1. **Topics discussed** - What was the conversation about?\n"
        "2. **Key decisions** - What choices were made and why?\n"
        "3. **Actions taken** - What files were modified, commands run, etc.?\n"
        "4. **Current state** - What was accomplished by the end of this section?\n"
        "5. **Unresolved items** - Anything left incomplete or pending?\n\n"
        "Format the summary as a clear, structured overview. Use bullet points.\n"
        "Be specific about file names, function names, and technical details.\n"
        "Keep it under 1500 words. Do not include preamble or meta-commentary."
    )

    response = client.messages.create(
        model=model,
        max_tokens=SUMMARY_MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": f"Summarize this conversation section:\n\n{transcript}",
        }],
    )

    return response.content[0].text.strip()


def build_summary_message(summary_text: str, metadata: dict) -> dict:
    """Construct a summary JSONL message that acts as the new root."""
    return {
        "type": "summary",
        "uuid": str(uuid_mod.uuid4()),
        "parentUuid": None,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sessionId": metadata.get("sessionId", ""),
        "isSidechain": False,
        "userType": metadata.get("userType", "external"),
        "cwd": metadata.get("cwd", ""),
        "version": metadata.get("version", ""),
        "gitBranch": metadata.get("gitBranch", ""),
        "summary": summary_text,
    }


def run_strip(session_path: Path) -> None:
    """Run Cozempic noise stripping as a pre-step.

    Attempts to import cozempic, falls back to subprocess.
    """
    try:
        from cozempic.session import load_messages as coz_load, save_messages as coz_save
        from cozempic.executor import run_prescription
        from cozempic.registry import PRESCRIPTIONS

        messages = coz_load(session_path)
        strategy_names = PRESCRIPTIONS["standard"]
        new_messages, results = run_prescription(messages, strategy_names, {"thinking_mode": "remove"})

        original_bytes = sum(b for _, _, b in messages)
        final_bytes = sum(b for _, _, b in new_messages)
        savings = original_bytes - final_bytes
        pct = (savings / original_bytes * 100) if original_bytes > 0 else 0

        coz_save(session_path, new_messages, create_backup=False)
        print(f"  Stripped {savings:,} bytes ({pct:.1f}%) via cozempic")
        return
    except ImportError:
        pass

    # Fall back to subprocess
    import subprocess
    try:
        result = subprocess.run(
            ["cozempic", "treat", str(session_path), "-rx", "standard", "--execute"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"  Stripped via cozempic (subprocess)")
            if result.stdout:
                # Print last line which usually has the summary
                for line in result.stdout.strip().split("\n")[-3:]:
                    print(f"    {line}")
        else:
            print(f"  Warning: cozempic failed: {result.stderr[:200]}")
    except FileNotFoundError:
        print("  Warning: cozempic not found. Install with: pip install cozempic")
        print("  Skipping noise stripping.")
    except subprocess.TimeoutExpired:
        print("  Warning: cozempic timed out after 60s. Skipping.")


def compact(
    session_path: Path,
    boundary_uuid: str,
    summary_text: str,
    *,
    strip: bool = False,
    backup: bool = True,
) -> dict:
    """Perform the full compaction: splice summary + tail into the session file.

    Returns a dict with stats about the operation.
    """
    original_bytes = session_path.stat().st_size

    # Create backup BEFORE any modifications (including strip)
    backup_path = None
    if backup:
        from datetime import datetime
        import shutil
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = session_path.with_suffix(f".{ts}.jsonl.bak")
        shutil.copy2(session_path, backup_path)

    if strip:
        print("  Running noise stripping...")
        run_strip(session_path)

    # Reload messages (may have been modified by strip)
    messages = load_messages(session_path)
    original_count = len(messages)

    # Validate boundary UUID still exists after strip
    all_uuids = {m.get("uuid") for m in messages if m.get("uuid")}
    if boundary_uuid not in all_uuids:
        raise RuntimeError(
            f"Boundary UUID {boundary_uuid} not found in session after processing. "
            f"This can happen if cozempic removed the boundary message. "
            f"Backup at: {backup_path}"
        )

    # Collect tail UUIDs (boundary and all descendants)
    tail_uuids = collect_tail_uuids(messages, boundary_uuid)

    if len(tail_uuids) == 0:
        raise RuntimeError(
            f"No messages found in tail starting from boundary {boundary_uuid}. "
            f"This should not happen. Backup at: {backup_path}"
        )

    # Build the summary message
    metadata = get_session_metadata(messages)
    summary_msg = build_summary_message(summary_text, metadata)

    # Reparent the boundary message to point to the summary
    for msg in messages:
        if msg.get("uuid") == boundary_uuid:
            msg["parentUuid"] = summary_msg["uuid"]
            break

    # Structural message types that should be preserved even without UUID in tail
    STRUCTURAL_TYPES = {"file-history-snapshot", "queue-operation", "summary"}

    # Build new message list: summary + all tail messages (in original order)
    new_messages = [summary_msg]
    for msg in messages:
        uid = msg.get("uuid")
        msg_type = msg.get("type", "")

        if uid and uid in tail_uuids:
            new_messages.append(msg)
        elif msg_type in STRUCTURAL_TYPES and not uid:
            # Keep structural messages without UUIDs
            new_messages.append(msg)
        elif msg_type == "file-history-snapshot":
            # Keep if it references a tail message
            mid = msg.get("messageId", "")
            if mid in tail_uuids:
                new_messages.append(msg)

    # Save (backup already created above, so skip in save_messages)
    save_messages(session_path, new_messages, backup=False)
    final_bytes = session_path.stat().st_size

    return {
        "original_messages": original_count,
        "final_messages": len(new_messages),
        "removed_messages": original_count - len(new_messages) + 1,  # +1 for added summary
        "original_bytes": original_bytes,
        "final_bytes": final_bytes,
        "saved_bytes": original_bytes - final_bytes,
        "backup_path": backup_path,
    }
