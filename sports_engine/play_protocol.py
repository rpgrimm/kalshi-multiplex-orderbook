"""Name-first play query: reserved tokens are not title search."""

from __future__ import annotations

from dataclasses import dataclass

KIND_TD = frozenset({"td", "touchdown", "touchdowns"})
KIND_FG = frozenset({"fg", "fieldgoal", "fieldgoals"})
KIND_SAF = frozenset({"saf", "safety", "safeties"})
KIND_QEND = frozenset({"qend", "endq", "endqtr"})
INTENT_REC = frozenset({"re", "rec", "recv", "receiving"})
INTENT_RUSH = frozenset({"ru", "rush", "rushing"})
INTENT_DEF = frozenset({"d", "def", "dst", "st"})
PAT_TOKENS = frozenset({"pat", "xp", "xpt", "extra"})
TWO_PT_TOKENS = frozenset({"2pt", "twopt", "two"})
NO_PAT_TOKENS = frozenset({"nopat", "noxp", "misspat"})
NO_2PT_TOKENS = frozenset({"no2pt", "miss2pt"})
EXTRA_TOKENS = PAT_TOKENS | TWO_PT_TOKENS | NO_PAT_TOKENS | NO_2PT_TOKENS
RESERVED = KIND_TD | KIND_FG | KIND_SAF | KIND_QEND | INTENT_REC | INTENT_RUSH | INTENT_DEF | EXTRA_TOKENS


@dataclass(frozen=True)
class PlayQuery:
    name_tokens: tuple[str, ...]
    kind: str | None  # "td" | "fg" | "saf" | "qend" | None
    intent: str | None  # "receiving" | "rush" | "defense" | None
    pat: bool = False
    extra: str | None = None  # pat | 2pt | nopat | no2pt
    raw: str = ""

    def is_complete_td(self) -> bool:
        return bool(self.name_tokens) and self.kind == "td" and self.intent in {"receiving", "rush"}

    def is_complete_fg(self) -> bool:
        return self.kind == "fg" and bool(self.name_tokens)

    def filter_tokens(self) -> list[str]:
        """Tokens that may AND against market haystack. Never includes re/ru/pat/d."""
        toks = list(self.name_tokens)
        if self.kind == "td":
            toks.append("td")
            toks = [t for t in toks if len(t) >= 2 or t == "td"]
        if self.kind == "fg":
            # 1-char team prefix matches the game code (KCMIA) on every ticker.
            toks = [t for t in toks if len(t) >= 2]
            toks.append("fg")
        if self.kind == "saf":
            toks = [t for t in toks if len(t) >= 2]
            return toks
        if self.kind == "qend":
            return []
        return toks


def parse_play_query(text: str) -> PlayQuery:
    raw = str(text or "")
    intent: str | None = None
    kind: str | None = None
    pat = False
    extra: str | None = None
    name: list[str] = []
    for tok in raw.lower().split():
        if tok in INTENT_REC:
            intent = "receiving"
        elif tok in INTENT_RUSH:
            intent = "rush"
        elif tok in INTENT_DEF:
            intent = "defense"
        elif tok in KIND_TD:
            kind = "td"
        elif tok in KIND_FG:
            kind = "fg"
        elif tok in KIND_SAF:
            kind = "saf"
        elif tok in KIND_QEND:
            kind = "qend"
        elif tok in NO_PAT_TOKENS:
            extra = "nopat"
        elif tok in NO_2PT_TOKENS:
            extra = "no2pt"
        elif tok in TWO_PT_TOKENS:
            extra = "2pt"
        elif tok in PAT_TOKENS:
            pat = True
            extra = extra or "pat"
        else:
            name.append(tok)
    return PlayQuery(
        name_tokens=tuple(name), kind=kind, intent=intent, pat=pat, extra=extra, raw=raw
    )


def last_token(text: str) -> tuple[str, str]:
    """Return (prefix_including_trailing_space, last_token)."""
    raw = str(text or "")
    if raw.endswith(" "):
        return raw, ""
    parts = raw.rsplit(" ", 1)
    if len(parts) == 1:
        return "", parts[0]
    return parts[0] + " ", parts[1]


def tab_complete_player(text: str, names: list[str]) -> tuple[str, list[str]]:
    """Complete the last token against player last names / full names.

    Returns (new_text, remaining_hits). Reserved tokens are not completed.
    """
    prefix, tok = last_token(text)
    if not tok or tok.lower() in RESERVED:
        return text, []
    needle = tok.lower()
    hits: list[str] = []
    seen: set[str] = set()
    for name in names:
        parts = name.lower().split()
        last = parts[-1] if parts else name.lower()
        if last.startswith(needle) or name.lower().startswith(needle):
            label = last  # type-speed: complete to last name
            if label not in seen:
                seen.add(label)
                hits.append(label)
        elif any(p.startswith(needle) for p in parts[:-1]):
            label = last
            if label not in seen:
                seen.add(label)
                hits.append(label)
    if not hits:
        return text, []
    if len(hits) == 1:
        return prefix + hits[0], hits
    common = _common_prefix(hits)
    if len(common) > len(needle):
        return prefix + common, hits
    return text, hits


def _common_prefix(words: list[str]) -> str:
    if not words:
        return ""
    first = words[0]
    for i, ch in enumerate(first):
        if any(w[i] != ch if i < len(w) else True for w in words[1:]):
            return first[:i]
    return first
