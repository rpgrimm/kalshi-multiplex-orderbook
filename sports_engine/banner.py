"""Startup ASCII banner. Swap banners/default.txt (or pass a stem) to change it."""

from __future__ import annotations

from pathlib import Path

BANNER_DIR = Path(__file__).resolve().parent / "banners"
DEFAULT_BANNER = BANNER_DIR / "default.txt"


def load_banner(name: str | None = None) -> str:
    """Return banner text with no trailing newline. Missing file → empty string."""
    stem = str(name or "").strip() or "default"
    path = BANNER_DIR / f"{stem}.txt"
    if not path.is_file():
        path = DEFAULT_BANNER
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""
