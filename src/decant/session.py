"""JSONL session parsing, discovery, and tree walking."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


@dataclass
class Exchange:
    """A single conversational exchange (user prompt or assistant text response)."""
    uuid: str
    role: str  # "user" or "assistant"
    text: str
    timestamp: str
    line_index: int


@dataclass
class SessionInfo:
    """Session metadata from sessions-index.json."""
    session_id: str
    path: Path
    summary: str = ""
    first_prompt: str = ""
    created: str = ""
    modified: str = ""
    git_branch: str = ""
    project_path: str = ""
    message_count: int = 0


def load_messages(path: Path) -> list[dict[str, Any]]:
    """Load all messages from a session JSONL file.

    Returns list of message dicts, each annotated with '_line_index'.
    """
    messages = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg["_line_index"] = i
                messages.append(msg)
            except json.JSONDecodeError:
                # Preserve unparseable lines
                messages.append({"_raw": line, "_parse_error": True, "_line_index": i})
    return messages


def save_messages(path: Path, messages: list[dict[str, Any]], *, backup: bool = True) -> Path | None:
    """Write messages back to JSONL. Creates timestamped backup if requested.

    Uses atomic write (write to temp, then rename) to prevent corruption on crash.
    """
    import tempfile
    backup_path = None
    if backup and path.exists():
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_suffix(f".{ts}.jsonl.bak")
        import shutil
        shutil.copy2(path, backup_path)

    # Write to temp file in same directory, then atomic rename
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".jsonl.tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            for msg in messages:
                # Strip internal annotation
                clean = {k: v for k, v in msg.items() if not k.startswith("_")}
                if msg.get("_parse_error"):
                    f.write(msg["_raw"] + "\n")
                else:
                    f.write(json.dumps(clean, separators=(",", ":")) + "\n")
        Path(tmp_path).rename(path)
    except Exception:
        # Clean up temp file on failure
        Path(tmp_path).unlink(missing_ok=True)
        raise

    return backup_path


def find_session(session_id: str) -> Path | None:
    """Find a session JSONL file by UUID or UUID prefix."""
    if not PROJECTS_DIR.exists():
        return None

    # Full path provided
    candidate = Path(session_id)
    if candidate.exists() and candidate.suffix == ".jsonl":
        return candidate

    # Search all project dirs
    matches: list[Path] = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        # Exact match — return immediately
        exact = project_dir / f"{session_id}.jsonl"
        if exact.exists():
            return exact
        # Prefix match — collect all candidates
        if len(session_id) >= 6:
            for jsonl in project_dir.glob("*.jsonl"):
                if jsonl.stem.startswith(session_id):
                    matches.append(jsonl)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous prefix '{session_id}' matches {len(matches)} sessions:", file=__import__('sys').stderr)
        for m in matches[:5]:
            print(f"  {m.stem}", file=__import__('sys').stderr)
        return None

    return None


def cwd_project_dir() -> str | None:
    """Return the Claude project directory name for the current working directory.

    Claude Code maps ``/home/user/my-project`` to ``-home-user-my-project``
    inside ``~/.claude/projects/``.  Returns *None* if the directory doesn't
    exist (no sessions for the current cwd).
    """
    import os
    cwd = os.getcwd()
    dirname = cwd.replace("/", "-")
    candidate = PROJECTS_DIR / dirname
    return dirname if candidate.is_dir() else None


def list_sessions(
    project: str | None = None,
    project_dir_name: str | None = None,
) -> list[SessionInfo]:
    """List all sessions, optionally filtered.

    Args:
        project: Substring match against project directory name (the old
            ``--project`` / ``-p`` flag).
        project_dir_name: Exact project directory name to match (used for
            cwd-based filtering).
    """
    sessions = []
    if not PROJECTS_DIR.exists():
        return sessions

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        if project_dir_name and project_dir.name != project_dir_name:
            continue
        if project and project.lower() not in project_dir.name.lower():
            continue

        # Try sessions-index.json
        index_path = project_dir / "sessions-index.json"
        if index_path.exists():
            try:
                index_data = json.loads(index_path.read_text())
                # Format: {"version": 1, "entries": [...]}
                entries = index_data.get("entries", []) if isinstance(index_data, dict) else index_data
                for entry in entries:
                    sid = entry.get("sessionId", "")
                    jsonl_path = project_dir / f"{sid}.jsonl"
                    if not jsonl_path.exists():
                        continue
                    size_mb = jsonl_path.stat().st_size / (1024 * 1024)
                    sessions.append(SessionInfo(
                        session_id=sid,
                        path=jsonl_path,
                        summary=entry.get("summary", ""),
                        first_prompt=entry.get("firstPrompt", ""),
                        created=entry.get("created", ""),
                        modified=entry.get("modified", ""),
                        git_branch=entry.get("gitBranch", ""),
                        project_path=entry.get("projectPath", ""),
                        message_count=entry.get("messageCount", 0),
                    ))
            except (json.JSONDecodeError, KeyError):
                pass
        else:
            # Fall back to scanning for JSONL files
            for jsonl in sorted(project_dir.glob("*.jsonl")):
                sessions.append(SessionInfo(
                    session_id=jsonl.stem,
                    path=jsonl,
                ))

    # Sort by modified date (most recent first)
    sessions.sort(key=lambda s: s.modified or s.created, reverse=True)
    return sessions


def build_uuid_map(messages: list[dict]) -> dict[str, dict]:
    """Build uuid -> message lookup."""
    return {m["uuid"]: m for m in messages if "uuid" in m}


def build_children_map(messages: list[dict]) -> dict[str, list[str]]:
    """Build parent_uuid -> [child_uuids] lookup."""
    children: dict[str, list[str]] = defaultdict(list)
    for msg in messages:
        parent = msg.get("parentUuid")
        uid = msg.get("uuid")
        if parent and uid:
            children[parent].append(uid)
    return children


def find_last_main_chain_message(messages: list[dict]) -> dict | None:
    """Find the last message on the main chain (non-sidechain)."""
    # Walk backward through messages to find last non-sidechain message
    for msg in reversed(messages):
        if not msg.get("isSidechain", False) and msg.get("uuid") and msg.get("type") in ("user", "assistant"):
            return msg
    return None


def walk_main_chain(messages: list[dict]) -> list[dict]:
    """Walk the main chain backward from the last message, return in chronological order.

    The main chain is the sequence of messages linked by parentUuid,
    starting from the last non-sidechain message.
    """
    uuid_map = build_uuid_map(messages)
    last = find_last_main_chain_message(messages)
    if not last:
        return []

    chain = []
    visited: set[str] = set()
    current = last
    while current:
        uid = current.get("uuid", "")
        if uid in visited:
            break  # Cycle detected
        visited.add(uid)
        chain.append(current)
        parent_uuid = current.get("parentUuid")
        if not parent_uuid:
            break
        current = uuid_map.get(parent_uuid)

    chain.reverse()
    return chain


def extract_exchanges(messages: list[dict]) -> list[Exchange]:
    """Extract clean conversational exchanges from the main chain.

    Filters to user prompts and assistant text responses only.
    Omits tool calls, tool results, thinking blocks, progress messages.
    """
    chain = walk_main_chain(messages)
    exchanges = []

    for msg in chain:
        msg_type = msg.get("type")
        if msg_type not in ("user", "assistant"):
            continue

        inner = msg.get("message", {})
        role = inner.get("role", "")
        content = inner.get("content")
        uuid = msg.get("uuid", "")
        timestamp = msg.get("timestamp", "")
        line_index = msg.get("_line_index", -1)

        if not content:
            continue

        # Extract text only
        if isinstance(content, str):
            # Simple string content (user prompt)
            text = content.strip()
            if text:
                exchanges.append(Exchange(uuid=uuid, role=role, text=text,
                                          timestamp=timestamp, line_index=line_index))
        elif isinstance(content, list):
            # Content block array - check if it's a tool result (skip those)
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if has_tool_result:
                continue

            # Extract text blocks only
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            text = "\n".join(text_parts).strip()
            if text:
                exchanges.append(Exchange(uuid=uuid, role=role, text=text,
                                          timestamp=timestamp, line_index=line_index))

    return exchanges


def extract_detailed_transcript(messages: list[dict]) -> str:
    """Extract a more detailed transcript including tool usage summaries.

    Used for the summarization step (more detail than exchanges).
    """
    chain = walk_main_chain(messages)
    lines = []

    for msg in chain:
        msg_type = msg.get("type")
        if msg_type not in ("user", "assistant"):
            continue

        inner = msg.get("message", {})
        role = inner.get("role", "")
        content = inner.get("content")

        if not content:
            continue

        if isinstance(content, str):
            lines.append(f"[{role.upper()}]: {content}")
        elif isinstance(content, list):
            parts = []
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if has_tool_result:
                # Summarize tool results briefly
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "?")
                        is_error = block.get("is_error", False)
                        result_content = block.get("content", "")
                        preview = ""
                        if isinstance(result_content, str):
                            preview = result_content[:200]
                        status = "ERROR" if is_error else "ok"
                        parts.append(f"  [tool result ({status}): {preview}...]")
                if parts:
                    lines.append(f"[TOOL RESULT]: {''.join(parts)}")
                continue

            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        # Brief summary of tool call
                        if name == "Bash":
                            cmd = inp.get("command", "?")[:120]
                            parts.append(f"  [Bash: {cmd}]")
                        elif name in ("Read", "Write", "Edit"):
                            fp = inp.get("file_path", "?")
                            parts.append(f"  [{name}: {fp}]")
                        elif name == "Grep":
                            pattern = inp.get("pattern", "?")
                            parts.append(f"  [Grep: {pattern}]")
                        elif name == "Glob":
                            pattern = inp.get("pattern", "?")
                            parts.append(f"  [Glob: {pattern}]")
                        elif name == "Task":
                            desc = inp.get("description", "?")
                            parts.append(f"  [Task: {desc}]")
                        else:
                            parts.append(f"  [{name}]")
                    # Skip thinking blocks

            if parts:
                text = "\n".join(parts)
                lines.append(f"[{role.upper()}]: {text}")

    return "\n\n".join(lines)


def collect_tail_uuids(messages: list[dict], boundary_uuid: str) -> set[str]:
    """Collect all message UUIDs that belong to the tail (boundary and descendants).

    Uses BFS from the boundary message through the children graph.
    """
    children_map = build_children_map(messages)

    tail: set[str] = set()
    queue = [boundary_uuid]
    while queue:
        uid = queue.pop(0)
        if uid in tail:
            continue  # Cycle protection
        tail.add(uid)
        queue.extend(children_map.get(uid, []))

    return tail


def get_session_metadata(messages: list[dict]) -> dict:
    """Extract common metadata fields from the first message."""
    for msg in messages:
        if msg.get("sessionId"):
            return {
                "sessionId": msg.get("sessionId", ""),
                "cwd": msg.get("cwd", ""),
                "version": msg.get("version", ""),
                "gitBranch": msg.get("gitBranch", ""),
                "slug": msg.get("slug", ""),
                "userType": msg.get("userType", "external"),
            }
    return {}
