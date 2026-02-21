"""CLI entry point for decant."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import ui
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
        ui.error(f"session '{args.session}' not found.")
        ui.hint("Use 'decant list' to see available sessions.")
        sys.exit(1)

    size_mb = session_path.stat().st_size / (1024 * 1024)

    # Resolve model
    model_key = args.model or DEFAULT_MODEL
    model_id = MODELS.get(model_key)
    if not model_id:
        ui.error(f"unknown model '{model_key}'. Choose from: {', '.join(MODELS)}")
        sys.exit(1)

    # Load messages
    messages = load_messages(session_path)

    # Session info header
    KW = 10
    print(ui.kv("Session", ui.dim(str(session_path)), KW))
    print(ui.kv("Size", f"{size_mb:.2f} MB", KW))
    print(ui.kv("Model", f"{model_key} {ui.accent(f'({model_id})')}", KW))
    print(ui.kv("Messages", f"{len(messages):,}", KW))

    # Create backup BEFORE any modifications (strip or compact)
    backup_path = None
    if not args.no_backup and not args.dry_run:
        from datetime import datetime
        import shutil
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = session_path.with_suffix(f".{ts}.jsonl.bak")
        shutil.copy2(session_path, backup_path)

    # Strip noise before boundary finding and summarization
    if args.strip and not args.dry_run:
        from .compactor import run_strip
        print(f"\n  {ui.header('Stripping noise...')}")
        messages, strip_stats = run_strip(messages)
        saved_kb = strip_stats["saved_bytes"] / 1024
        for name, saved in strip_stats["breakdown"].items():
            if saved > 0:
                print(ui.bullet(f"{name:<24} {ui.dim(f'{saved / 1024:.1f} KB')}", indent=4))
        pct_str = f"{strip_stats['pct']:.1f}%"
        print(ui.bullet(
            f"Removed {strip_stats['removed_messages']} messages, "
            f"saved {saved_kb:.1f} KB {ui.dim(f'({pct_str})')}",
            indent=4,
        ))
        # Write stripped messages back so compact() reads clean data
        from .session import save_messages
        save_messages(session_path, messages, backup=False)

    # Find boundary
    try:
        client = None
        if args.topic:
            client = create_client()
            exchanges = extract_exchanges(messages)
            if not exchanges:
                ui.error("no conversational exchanges found in session.")
                sys.exit(1)
            print(f"\n  {ui.dim(f'Extracted {len(exchanges)} exchanges')}")
            with ui.Spinner(f"Finding boundary for topic: {ui.accent(repr(args.topic))}") as sp:
                boundary_uuid = find_boundary_by_topic(exchanges, args.topic, client, model_id)
            # Find the exchange for display
            for ex in exchanges:
                if ex.uuid == boundary_uuid:
                    preview = ex.text[:80].replace("\n", " ")
                    sp.done(f"[{ex.role}] {preview}{ui.sym.ellipsis}")
                    break
            else:
                sp.done()
        elif args.last is not None:
            print(f"\n  Keeping last {ui.header(str(args.last))} user turns")
            boundary_uuid = find_boundary_by_count(messages, args.last)
            exchanges = extract_exchanges(messages)
            for ex in exchanges:
                if ex.uuid == boundary_uuid:
                    preview = ex.text[:80].replace("\n", " ")
                    print(f"  {ui.success(ui.sym.check)} Boundary: {ui.dim(f'[{ex.role}]')} {preview}{ui.sym.ellipsis}")
                    break
        else:
            ui.error("specify --topic or --last.")
            sys.exit(1)
    except ValueError as e:
        ui.error(str(e))
        sys.exit(1)
    except RuntimeError as e:
        ui.error(str(e))
        sys.exit(1)

    # Summarize head
    try:
        if not args.dry_run:
            if not client:
                client = create_client()
            with ui.Spinner("Summarizing head section") as sp:
                summary = summarize_head(messages, boundary_uuid, client, model_id)
            sp.done(f"{len(summary):,} chars")

            # Preview summary
            print()
            print(ui.titled_rule("Summary Preview"))
            preview = summary[:500]
            if len(summary) > 500:
                preview += f"\n{ui.dim(ui.sym.ellipsis)}"
            print(preview)
            print(ui.rule())
            print()

            # Compact
            with ui.Spinner("Compacting session") as sp:
                stats = compact(
                    session_path,
                    boundary_uuid,
                    summary,
                    backup=False,  # Already created above
                )
            sp.done()

            # Completion stats
            original_mb = stats["original_bytes"] / (1024 * 1024)
            final_mb = stats["final_bytes"] / (1024 * 1024)
            saved_mb = stats["saved_bytes"] / (1024 * 1024)
            pct = (stats["saved_bytes"] / stats["original_bytes"] * 100) if stats["original_bytes"] > 0 else 0

            print(f"\n  {ui.success(ui.sym.check)} {ui.header('Done')}")
            KR = 10
            print(ui.kv("Messages", f"{stats['original_messages']:,} {ui.dim(ui.sym.arrow)} {stats['final_messages']:,}", KR))
            print(ui.kv("Size",
                f"{original_mb:.2f} MB {ui.dim(ui.sym.arrow)} {final_mb:.2f} MB  "
                f"{ui.success(f'{saved_mb:.2f} MB saved')} {ui.dim(f'({pct:.1f}%)')}", KR))
            if backup_path:
                print(ui.kv("Backup", ui.dim(str(backup_path)), KR))
        else:
            # Dry run
            print()
            print(ui.titled_rule(ui.warn("Dry Run")))
            print()
            from .session import collect_tail_uuids
            tail_uuids = collect_tail_uuids(messages, boundary_uuid)
            head_count = len(messages) - len(tail_uuids)
            KD = 10
            print(ui.kv("Boundary", ui.dim(boundary_uuid), KD))
            print(ui.kv("Head", f"~{head_count:,} messages {ui.dim('(to summarize)')}", KD))
            print(ui.kv("Tail", f"~{len(tail_uuids):,} messages {ui.dim('(to keep)')}", KD))
    except (ValueError, RuntimeError) as e:
        ui.error(str(e))
        sys.exit(1)
    except Exception as e:
        ui.error(f"(unexpected) {e}")
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """List available sessions."""
    from .session import cwd_project_dir, list_sessions

    # Default: show sessions for the current project (cwd).
    # --all/-a: show everything.  --project/-p: substring filter.
    if args.all:
        sessions = list_sessions(project=args.project)
    elif args.project:
        sessions = list_sessions(project=args.project)
    else:
        cwd_dir = cwd_project_dir()
        if cwd_dir:
            sessions = list_sessions(project_dir_name=cwd_dir)
        else:
            sessions = []

    if not sessions:
        if not args.all and not args.project:
            ui.error("no sessions for this project.")
            ui.hint("Use 'decant list --all' to list all sessions.")
        else:
            ui.error("no sessions found.")
        return

    for i, s in enumerate(sessions):
        size_mb = s.path.stat().st_size / (1024 * 1024) if s.path.exists() else 0
        project = s.project_path or s.path.parent.name
        if len(project) > 40:
            project = ui.sym.ellipsis + project[-37:]

        summary = s.summary or s.first_prompt or "(no summary)"
        if len(summary) > 60:
            summary = summary[:57] + ui.sym.ellipsis

        modified = s.modified[:10] if s.modified else "?"
        sid_short = s.session_id[:8]

        print(f"  {ui.dim(sid_short)}  {size_mb:5.1f} MB  {modified}  {ui.dim(project)}")
        print(f"             {ui.dim(summary)}")
        if i < len(sessions) - 1:
            print()


def cmd_show(args: argparse.Namespace) -> None:
    """Show session exchanges."""
    from .session import extract_exchanges, find_session, load_messages

    session_path = find_session(args.session)
    if not session_path:
        ui.error(f"session '{args.session}' not found.")
        sys.exit(1)

    messages = load_messages(session_path)
    exchanges = extract_exchanges(messages)

    print(ui.kv("Session", ui.dim(str(session_path)), 10))
    print(f"             {ui.dim(f'{len(messages)} messages, {len(exchanges)} exchanges')}")
    print()
    print(ui.rule())
    print()

    for i, ex in enumerate(exchanges):
        role_str = ui.label("USER") if ex.role == "user" else ui.header("ASST")
        text = ex.text
        if len(text) > 200 and not args.full:
            text = text[:200] + ui.sym.ellipsis
        print(f"  {ui.header(f'#{i+1}')}  {role_str}  {ui.dim(ex.uuid[:8])}")
        print(f"      {text}")
        print()


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
                           help="Strip noise (progress ticks, thinking blocks, metadata, oversized tool output) before compaction")
    p_compact.add_argument("--dry-run", "-n", action="store_true",
                           help="Preview what would be compacted without making changes")
    p_compact.add_argument("--no-backup", action="store_true",
                           help="Skip creating a backup of the original session file")

    # list
    p_list = sub.add_parser("list", help="List available sessions")
    p_list.add_argument("--all", "-a", action="store_true",
                         help="List sessions across all projects (default: current project only)")
    p_list.add_argument("--project", "-p", help="Filter by project name substring")

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
