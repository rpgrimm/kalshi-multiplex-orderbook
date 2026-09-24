"""Parse compact operator scripts: `/ wa Enter td Enter re Enter`."""

from __future__ import annotations

KEY_ALIASES = {
    "enter": ("enter", "enter"),
    "return": ("enter", "enter"),
    "tab": ("char", "\t"),
    "esc": ("escape", "esc"),
    "escape": ("escape", "esc"),
    "space": ("char", " "),
}


def parse_key_script(text: str) -> list[tuple[str, str]]:
    """Split a script into (kind, value) steps.

    Words other than key names are typed as characters. A leading space is
    inserted before a word when the previous step was Enter/Tab (the
    runner does that using live filter_text).
    """
    out: list[tuple[str, str]] = []
    for tok in str(text or "").replace(",", " ").split():
        low = tok.lower()
        if tok == "/":
            out.append(("slash", "/"))
        elif low in KEY_ALIASES:
            out.append(KEY_ALIASES[low])
        else:
            out.append(("word", tok))
    return out
