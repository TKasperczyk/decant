"""Native noise stripping for session messages.

Operates in-memory on message lists. No external dependencies.
Inspired by cozempic's strategy approach but standalone.

Strategies applied:
  - progress-collapse: remove consecutive progress tick messages (~40-48%)
  - thinking-strip: remove thinking blocks and signatures (~2-5%)
  - metadata-strip: remove usage stats, stop_reason, costUSD, duration (~1-3%)
  - tool-output-trim: trim oversized tool_result blocks >8KB (~1-8%)
"""

from __future__ import annotations

import copy
import json
from typing import Any


# Tool output trimming thresholds
TOOL_OUTPUT_MAX_BYTES = 8192
TOOL_OUTPUT_MAX_LINES = 100

# Metadata fields to strip
STRIP_INNER = {"usage", "stop_reason", "stop_sequence"}
STRIP_OUTER = {"costUSD", "duration", "apiDuration"}


def _msg_bytes(msg: dict) -> int:
    """Estimate serialized size of a message."""
    return len(json.dumps(msg, separators=(",", ":")).encode("utf-8"))


def _get_content_blocks(msg: dict) -> list[dict]:
    """Extract content blocks from a message's inner content."""
    inner = msg.get("message", {})
    content = inner.get("content")
    if isinstance(content, list):
        return content
    return []


def _set_content_blocks(msg: dict, blocks: list[dict]) -> dict:
    """Return a copy of msg with content blocks replaced."""
    new = copy.deepcopy(msg)
    if "message" in new:
        new["message"]["content"] = blocks
    return new


def _collapse_progress(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove consecutive progress messages, keeping only the last in each run."""
    result = []
    saved = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("type") == "progress":
            # Collect the full run of progress messages
            run_start = i
            while i + 1 < len(messages) and messages[i + 1].get("type") == "progress":
                i += 1
            # Keep only the last one
            for j in range(run_start, i):
                saved += _msg_bytes(messages[j])
            result.append(messages[i])
        else:
            result.append(msg)
        i += 1
    return result, saved


def _strip_thinking(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove thinking blocks and signature fields from assistant messages."""
    result = []
    saved = 0
    for msg in messages:
        if msg.get("type") != "assistant":
            result.append(msg)
            continue

        blocks = _get_content_blocks(msg)
        if not blocks:
            result.append(msg)
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            btype = block.get("type", "")
            if btype == "thinking":
                changed = True
                continue  # Drop entirely
            if "signature" in block:
                new_blocks.append({k: v for k, v in block.items() if k != "signature"})
                changed = True
            else:
                new_blocks.append(block)

        if changed:
            orig_size = _msg_bytes(msg)
            new_msg = _set_content_blocks(msg, new_blocks)
            new_size = _msg_bytes(new_msg)
            if new_size < orig_size:
                saved += orig_size - new_size
                result.append(new_msg)
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result, saved


def _strip_metadata(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove usage stats, stop_reason, costUSD, duration fields."""
    result = []
    saved = 0
    for msg in messages:
        new_msg = copy.deepcopy(msg)
        changed = False

        inner = new_msg.get("message", {})
        for field in STRIP_INNER:
            if field in inner:
                del inner[field]
                changed = True

        for field in STRIP_OUTER:
            if field in new_msg:
                del new_msg[field]
                changed = True

        if changed:
            orig_size = _msg_bytes(msg)
            new_size = _msg_bytes(new_msg)
            if new_size < orig_size:
                saved += orig_size - new_size
                result.append(new_msg)
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result, saved


def _trim_tool_output(messages: list[dict]) -> tuple[list[dict], int]:
    """Trim oversized tool_result blocks."""
    result = []
    saved = 0
    for msg in messages:
        blocks = _get_content_blocks(msg)
        if not blocks:
            result.append(msg)
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            if block.get("type") != "tool_result":
                new_blocks.append(block)
                continue

            content = block.get("content", "")
            if not isinstance(content, str):
                new_blocks.append(block)
                continue

            content_bytes = len(content.encode("utf-8"))
            content_lines = content.count("\n") + 1

            if content_bytes <= TOOL_OUTPUT_MAX_BYTES and content_lines <= TOOL_OUTPUT_MAX_LINES:
                new_blocks.append(block)
                continue

            # Trim by lines first, then by bytes
            lines = content.split("\n")
            if len(lines) > TOOL_OUTPUT_MAX_LINES:
                keep = TOOL_OUTPUT_MAX_LINES // 2
                trimmed = (
                    lines[:keep]
                    + [f"\n... [{len(lines) - TOOL_OUTPUT_MAX_LINES} lines trimmed] ...\n"]
                    + lines[-keep:]
                )
                new_content = "\n".join(trimmed)
            else:
                half = TOOL_OUTPUT_MAX_BYTES // 2
                new_content = (
                    content[:half]
                    + f"\n... [{content_bytes - TOOL_OUTPUT_MAX_BYTES} bytes trimmed] ...\n"
                    + content[-half:]
                )

            new_blocks.append({**block, "content": new_content})
            changed = True

        if changed:
            orig_size = _msg_bytes(msg)
            new_msg = _set_content_blocks(msg, new_blocks)
            new_size = _msg_bytes(new_msg)
            if new_size < orig_size:
                saved += orig_size - new_size
                result.append(new_msg)
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result, saved


def strip_messages(messages: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """Apply all stripping strategies to a message list.

    Returns (stripped_messages, stats_dict).
    """
    original_bytes = sum(_msg_bytes(m) for m in messages)
    original_count = len(messages)
    breakdown: dict[str, int] = {}

    messages, saved = _collapse_progress(messages)
    breakdown["progress-collapse"] = saved

    messages, saved = _strip_thinking(messages)
    breakdown["thinking-strip"] = saved

    messages, saved = _strip_metadata(messages)
    breakdown["metadata-strip"] = saved

    messages, saved = _trim_tool_output(messages)
    breakdown["tool-output-trim"] = saved

    total_saved = sum(breakdown.values())
    pct = (total_saved / original_bytes * 100) if original_bytes > 0 else 0

    stats = {
        "original_count": original_count,
        "final_count": len(messages),
        "removed_messages": original_count - len(messages),
        "original_bytes": original_bytes,
        "saved_bytes": total_saved,
        "pct": pct,
        "breakdown": breakdown,
    }
    return messages, stats
