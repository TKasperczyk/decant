"""Model ID constants and defaults."""

MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
}

DEFAULT_MODEL = "haiku"

# Max tokens for summarization output
SUMMARY_MAX_TOKENS = 4096

# Max characters of transcript to send for boundary finding
BOUNDARY_TRANSCRIPT_MAX_CHARS = 100_000

# Max characters of transcript to send for summarization
SUMMARY_TRANSCRIPT_MAX_CHARS = 200_000
