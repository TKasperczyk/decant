"""Terminal formatting utilities for decant CLI output.

Zero-dependency module using raw ANSI escape codes.
Supports NO_COLOR, FORCE_COLOR, DECANT_ASCII, and TTY detection.
"""

from __future__ import annotations

import locale
import os
import sys
import threading
import time


# ---------------------------------------------------------------------------
# Detection (evaluated once at import)
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


def _supports_unicode() -> bool:
    if os.environ.get("DECANT_ASCII") is not None:
        return False
    encoding = locale.getpreferredencoding(False)
    return encoding.lower().replace("-", "") in ("utf8",)


COLOR: bool = _supports_color()
UNICODE: bool = _supports_unicode()


# ---------------------------------------------------------------------------
# ANSI escape builder
# ---------------------------------------------------------------------------

def _sgr(*codes: int) -> str:
    if not COLOR:
        return ""
    return f"\033[{';'.join(str(c) for c in codes)}m"


_RESET = _sgr(0)


# ---------------------------------------------------------------------------
# Style functions
# ---------------------------------------------------------------------------

def header(text: str) -> str:
    """Bold bright white -- titles, completion messages."""
    return f"{_sgr(1, 97)}{text}{_RESET}"


def label(text: str) -> str:
    """Bold cyan -- key names in key-value pairs."""
    return f"{_sgr(1, 36)}{text}{_RESET}"


def success(text: str) -> str:
    """Bold green -- checkmarks, positive results."""
    return f"{_sgr(1, 32)}{text}{_RESET}"


def warn(text: str) -> str:
    """Bold yellow -- dry run notices, warnings."""
    return f"{_sgr(1, 33)}{text}{_RESET}"


def error_style(text: str) -> str:
    """Bold red -- error prefixes."""
    return f"{_sgr(1, 31)}{text}{_RESET}"


def dim(text: str) -> str:
    """Dim -- secondary info, UUIDs, paths, rules."""
    return f"{_sgr(2)}{text}{_RESET}"


def accent(text: str) -> str:
    """Magenta -- model names, topics, highlighted values."""
    return f"{_sgr(35)}{text}{_RESET}"


# ---------------------------------------------------------------------------
# Symbol table
# ---------------------------------------------------------------------------

class _Symbols:
    __slots__ = (
        "check", "cross", "arrow", "bullet", "ellipsis",
        "bar_h", "spinner_frames",
    )

    def __init__(self, unicode: bool) -> None:
        if unicode:
            self.check = "\u2713"
            self.cross = "\u2717"
            self.arrow = "\u2192"
            self.bullet = "\u2022"
            self.ellipsis = "\u2026"
            self.bar_h = "\u2500"
            self.spinner_frames = list("\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f")
        else:
            self.check = "+"
            self.cross = "x"
            self.arrow = "->"
            self.bullet = "*"
            self.ellipsis = "..."
            self.bar_h = "-"
            self.spinner_frames = list("|/-\\")


sym = _Symbols(UNICODE)


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        return 80


def kv(key: str, val: str, key_width: int = 10) -> str:
    """Format a right-aligned key + value pair.

    ``  Session  /path/to/file``
    """
    return f"  {label(key.rjust(key_width))}  {val}"


def rule(width: int = 0) -> str:
    """Dim horizontal line."""
    w = width or _term_width()
    return dim(sym.bar_h * w)


def titled_rule(title: str, width: int = 0) -> str:
    """``-- Title ----------------------------------------``

    The title is rendered as-is -- caller controls styling.
    If no styling is applied, it defaults to header().
    """
    w = width or _term_width()
    # Strip ANSI to measure visible length for padding
    import re
    visible = re.sub(r"\033\[[0-9;]*m", "", title)
    styled_title = title if title != visible else header(title)
    prefix_len = 4 + len(visible)  # "-- " + title + " "
    remaining = max(0, w - prefix_len)
    return dim(f"{sym.bar_h}{sym.bar_h} ") + styled_title + dim(f" {sym.bar_h * remaining}")


def bullet(text: str, indent: int = 2) -> str:
    """Bulleted line: ``  * text``"""
    pad = " " * indent
    return f"{pad}{dim(sym.bullet)} {text}"


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

class Spinner:
    """Animated spinner for long-running operations.

    Writes to stderr so piped stdout stays clean.
    Degrades to a static line when stderr is not a TTY.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._animate = COLOR and hasattr(sys.stderr, "isatty") and sys.stderr.isatty()

    def __enter__(self) -> Spinner:
        if not self._animate:
            sys.stderr.write(f"  {self.message}...\n")
            sys.stderr.flush()
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        if self._animate:
            sys.stderr.write(f"\r\033[K")
            sys.stderr.flush()

    def _spin(self) -> None:
        frames = sym.spinner_frames
        i = 0
        while not self._stop.is_set():
            frame = frames[i % len(frames)]
            sys.stderr.write(f"\r  {accent(frame)} {self.message}")
            sys.stderr.flush()
            i += 1
            self._stop.wait(0.08)

    def done(self, detail: str = "") -> None:
        """Print a checkmark completion line after the spinner exits."""
        parts = [f"  {success(sym.check)} {self.message}"]
        if detail:
            parts.append(f" {dim(detail)}")
        sys.stderr.write("".join(parts) + "\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Error / hint helpers (write to stderr)
# ---------------------------------------------------------------------------

def error(msg: str) -> None:
    sys.stderr.write(f"{error_style('error:')} {msg}\n")
    sys.stderr.flush()


def hint(msg: str) -> None:
    sys.stderr.write(f"{dim(warn('hint:'))} {dim(msg)}\n")
    sys.stderr.flush()
