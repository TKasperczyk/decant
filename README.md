```
     _                      _
  __| | ___  ___ __ _ _ __ | |_
 / _` |/ _ \/ __/ _` | '_ \| __|
| (_| |  __/ (_| (_| | | | | |_
 \__,_|\___|\___\__,_|_| |_|\__|
```

# decant

Selective offline compaction for Claude Code sessions.

Claude Code sessions accumulate context over time: tool calls, file reads, thinking blocks, progress ticks. When a session gets bloated, the built-in compaction summarizes everything. But sometimes you want to keep the recent work intact and only summarize the old stuff. That's what decant does.

You pick a point in the conversation, either by topic ("keep everything about the API refactor") or by count ("keep the last 5 exchanges"), and decant summarizes everything before that point, splices the summary in, and preserves the rest. The output matches Claude Code's native compaction format, so `claude --resume` works normally.

## Install

```bash
cd ~/Programming/decant
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Optional noise stripping (removes thinking blocks, duplicate file reads, progress ticks before summarization):

```bash
pip install cozempic
```

## Usage

### List sessions

```bash
decant list
decant list --project claude-memory
```

### Preview a session's exchanges

```bash
decant show <session-id>
decant show <session-id> --full
```

### Compact by topic

Keep everything from a topic onward, summarize everything before it:

```bash
decant compact <session-id> --topic "API refactor"
decant compact <session-id> --topic "the bug fix" --model sonnet
```

### Compact by count

Keep the last N user turns, summarize the rest:

```bash
decant compact <session-id> --last 5
decant compact <session-id> --last 10 --model opus
```

### Options

- `--model haiku|sonnet|opus` - which model to use for boundary finding and summarization (default: haiku)
- `--strip` - run [cozempic](https://github.com/Ruya-AI/cozempic) noise stripping before compaction
- `--dry-run` - preview what would happen without touching anything
- `--no-backup` - skip creating a `.bak` file (not recommended)

Session IDs can be the full UUID, a prefix (minimum 6 chars), or a direct path to a `.jsonl` file.

## How it works

1. Parses the session JSONL and reconstructs the conversation tree via `parentUuid` chains
2. Finds the boundary message, either by asking an LLM to locate a topic, or by counting user turns from the end
3. Optionally strips noise (thinking blocks, progress ticks, stale file reads) via cozempic
4. Sends the head section to the LLM for summarization
5. Writes a summary record matching Claude Code's native format (`{type, summary, leafUuid}`)
6. Sets the boundary message as the new tree root (`parentUuid: null`)
7. Drops all head messages and their sidechains, keeps everything from the boundary forward

A timestamped backup is created before any modifications.

## Authentication

Decant needs Anthropic API access for the LLM calls. It checks these in order:

1. `ANTHROPIC_API_KEY` or `OPENCODE_API_KEY` environment variable
2. `ANTHROPIC_AUTH_TOKEN` environment variable
3. Claude Code OAuth credentials (`~/.claude/.credentials.json`)
4. Kira credentials (`~/.kira/credentials.json`)

If you're already authenticated with Claude Code, it just works. Decant reuses those credentials with automatic token refresh.

## Why "decant"?

You're pouring the conversation through a filter. The clear, recent context comes through. The sediment stays behind as a summary.
