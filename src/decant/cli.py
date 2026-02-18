"""CLI entry point for decant."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .models import DEFAULT_MODEL, MODELS


def cmd_compact(args: argparse.Namespace) -> None:
    """Run the compact command."""
    from .auth import create_client
    from .compactor import (
        compact,
        find_boundary_by_count,
        find_boundary_by_topic,
        summarize_head,
    )
    from .session import extract_exchanges, find_session, load_messages

    # Resolve session
    session_path = find_session(args.session)
    if not session_path:
        print(f"Error: session '{args.session}' not found.", file=sys.stderr)
        print("Use 'decant list' to see available sessions.", file=sys.stderr)
        sys.exit(1)

    print(f"Session: {session_path}")
    size_mb = session_path.stat().st_size / (1024 * 1024)
    print(f"Size: {size_mb:.2f} MB")

    # Resolve model
    model_key = args.model or DEFAULT_MODEL
    model_id = MODELS.get(model_key)
    if not model_id:
        print(f"Error: unknown model '{model_key}'. Choose from: {', '.join(MODELS)}", file=sys.stderr)
        sys.exit(1)

    print(f"Model: {model_key} ({model_id})")

    # Load messages
    messages = load_messages(session_path)
    print(f"Messages: {len(messages)}")

    # Find boundary
    try:
        client = None
        if args.topic:
            print(f"\nFinding boundary for topic: '{args.topic}'...")
            client = create_client()
            exchanges = extract_exchanges(messages)
            if not exchanges:
                print("Error: no conversational exchanges found in session.", file=sys.stderr)
                sys.exit(1)
            print(f"  Extracted {len(exchanges)} exchanges")
            boundary_uuid = find_boundary_by_topic(exchanges, args.topic, client, model_id)
            # Find the exchange for display
            for ex in exchanges:
                if ex.uuid == boundary_uuid:
                    preview = ex.text[:80].replace("\n", " ")
                    print(f"  Boundary: [{ex.role}] {preview}...")
                    break
        elif args.last is not None:
            print(f"\nKeeping last {args.last} user turns...")
            boundary_uuid = find_boundary_by_count(messages, args.last)
            exchanges = extract_exchanges(messages)
            for ex in exchanges:
                if ex.uuid == boundary_uuid:
                    preview = ex.text[:80].replace("\n", " ")
                    print(f"  Boundary: [{ex.role}] {preview}...")
                    break
        else:
            print("Error: specify --topic or --last.", file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Summarize head
    try:
        if not args.dry_run:
            print("\nSummarizing head section...")
            if not client:
                client = create_client()
            summary = summarize_head(messages, boundary_uuid, client, model_id)
            print(f"  Summary: {len(summary)} chars")

            # Preview summary
            print("\n--- Summary Preview ---")
            # Show first 500 chars
            preview = summary[:500]
            if len(summary) > 500:
                preview += "\n..."
            print(preview)
            print("--- End Preview ---\n")

            # Compact
            print("Compacting...")
            stats = compact(
                session_path,
                boundary_uuid,
                summary,
                strip=args.strip,
                backup=not args.no_backup,
            )

            print(f"\nDone.")
            print(f"  Messages: {stats['original_messages']} -> {stats['final_messages']}")
            saved_mb = stats["saved_bytes"] / (1024 * 1024)
            pct = (stats["saved_bytes"] / stats["original_bytes"] * 100) if stats["original_bytes"] > 0 else 0
            print(f"  Size: {stats['original_bytes'] / (1024*1024):.2f} MB -> {stats['final_bytes'] / (1024*1024):.2f} MB ({saved_mb:.2f} MB saved, {pct:.1f}%)")
            if stats["backup_path"]:
                print(f"  Backup: {stats['backup_path']}")
        else:
            print("\n[DRY RUN] Would summarize and compact here.")
            print(f"  Boundary UUID: {boundary_uuid}")

            # Show what would be removed vs kept
            from .session import collect_tail_uuids
            tail_uuids = collect_tail_uuids(messages, boundary_uuid)
            head_count = len(messages) - len(tail_uuids)
            print(f"  Head (to summarize): ~{head_count} messages")
            print(f"  Tail (to keep): ~{len(tail_uuids)} messages")
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error (unexpected): {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """List available sessions."""
    from .session import list_sessions

    sessions = list_sessions(project=args.project)
    if not sessions:
        print("No sessions found.")
        return

    for s in sessions:
        size_mb = s.path.stat().st_size / (1024 * 1024) if s.path.exists() else 0
        project = s.project_path or s.path.parent.name
        # Truncate project path for display
        if len(project) > 40:
            project = "..." + project[-37:]

        summary = s.summary or s.first_prompt or "(no summary)"
        if len(summary) > 60:
            summary = summary[:57] + "..."

        modified = s.modified[:10] if s.modified else "?"
        sid_short = s.session_id[:8]

        print(f"  {sid_short}  {size_mb:6.2f}MB  {modified}  {project}")
        print(f"           {summary}")


def cmd_show(args: argparse.Namespace) -> None:
    """Show session exchanges."""
    from .session import extract_exchanges, find_session, load_messages

    session_path = find_session(args.session)
    if not session_path:
        print(f"Error: session '{args.session}' not found.", file=sys.stderr)
        sys.exit(1)

    messages = load_messages(session_path)
    exchanges = extract_exchanges(messages)

    print(f"Session: {session_path}")
    print(f"Messages: {len(messages)} total, {len(exchanges)} exchanges\n")

    for i, ex in enumerate(exchanges):
        prefix = "USER" if ex.role == "user" else "ASST"
        text = ex.text
        if len(text) > 200 and not args.full:
            text = text[:200] + "..."
        print(f"[{i+1}] {prefix} (uuid={ex.uuid[:8]}): {text}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="decant",
        description="Selective offline compaction for Claude Code sessions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    # compact
    p_compact = sub.add_parser("compact", help="Compact a session by summarizing old exchanges")
    p_compact.add_argument("session", help="Session UUID, UUID prefix, or path to JSONL")
    group = p_compact.add_mutually_exclusive_group(required=True)
    group.add_argument("--topic", "-t", help="Keep from this topic onward, summarize everything before")
    group.add_argument("--last", "-l", type=int, help="Keep last N user turns, summarize everything before")
    p_compact.add_argument("--model", "-m", choices=list(MODELS.keys()), default=DEFAULT_MODEL,
                           help=f"Model for summarization (default: {DEFAULT_MODEL})")
    p_compact.add_argument("--strip", "-s", action="store_true",
                           help="Run cozempic noise stripping before compaction")
    p_compact.add_argument("--dry-run", "-n", action="store_true",
                           help="Preview what would be compacted without making changes")
    p_compact.add_argument("--no-backup", action="store_true",
                           help="Skip creating a backup of the original session file")

    # list
    p_list = sub.add_parser("list", help="List available sessions")
    p_list.add_argument("--project", "-p", help="Filter by project name")

    # show
    p_show = sub.add_parser("show", help="Show session exchanges")
    p_show.add_argument("session", help="Session UUID, UUID prefix, or path to JSONL")
    p_show.add_argument("--full", "-f", action="store_true", help="Show full message text")

    args = parser.parse_args()

    if args.command == "compact":
        cmd_compact(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "show":
        cmd_show(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
