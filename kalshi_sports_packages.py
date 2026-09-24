#!/usr/bin/env python3
"""Stored JSON bet packages for the sports --browse UI.

Load / match / resolve only. The trader owns keyboard arm/confirm/send.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

WarnFn = Callable[[str], None]

USER_PACKAGES_DIR = Path.home() / ".config" / "kalshi-multiplex-orderbook" / "packages"
EXAMPLES_PACKAGES_REL = Path("examples") / "packages"

# Kalshi game codes are DATE + away + home, e.g. 26SEP17DETBUF / 26SEP14DENKC.
_GAME_DATE_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]+)$")

# Two- and three-letter NFL (and occasional Kalshi) abbreviations used to split
# the concatenated team blob and to read a ticker suffix after SERIES-GAMECODE-.
NFL_TEAM_ABBREVS = frozenset(
    {
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAC",
        "JAX",
        "KC",
        "LA",
        "LAC",
        "LAR",
        "LV",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SEA",
        "SF",
        "TB",
        "TEN",
        "WAS",
        "WSH",
    }
)


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True)
class LegMatch:
    series: str
    same_team_as_trigger: bool = False
    floor_strike: float | None = None


@dataclass(frozen=True)
class PackageTrigger:
    series: str
    exclude_title_substrings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageLeg:
    id: str
    action: str = "buy_yes"
    count: str | int = "session"
    role: str | None = None
    required: bool = False
    match: LegMatch | None = None


@dataclass(frozen=True)
class Package:
    id: str
    title: str
    enabled: bool
    trigger: PackageTrigger
    legs: tuple[PackageLeg, ...]
    source_path: str = ""
    source_kind: str = "shipped"  # cli | user | shipped


@dataclass
class ResolvedLeg:
    id: str
    required: bool
    role: str | None
    row: Any | None
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.row is not None


@dataclass
class ResolvedPackage:
    package: Package
    trigger_row: Any
    legs: list[ResolvedLeg] = field(default_factory=list)

    def resolved_legs(self) -> list[ResolvedLeg]:
        return [leg for leg in self.legs if leg.row is not None]

    def unresolved_legs(self) -> list[ResolvedLeg]:
        return [leg for leg in self.legs if leg.row is None]

    def required_unresolved(self) -> list[ResolvedLeg]:
        return [leg for leg in self.unresolved_legs() if leg.required]

    def trigger_leg(self) -> ResolvedLeg | None:
        for leg in self.legs:
            if (leg.role or "") == "trigger":
                return leg
        ticker = str(getattr(self.trigger_row, "ticker", "") or "")
        for leg in self.legs:
            row = leg.row
            if row is not None and str(getattr(row, "ticker", "") or "") == ticker:
                return leg
        return None


def package_search_paths(
    *,
    script_file: str | Path,
    packages_dir: str | Path | None = None,
) -> list[tuple[Path, str]]:
    """Load-order dirs: CLI, user config, examples next to the script.

    Each entry is (path, source_kind) with source_kind in cli|user|shipped.
    """
    out: list[tuple[Path, str]] = []
    if packages_dir:
        out.append((Path(packages_dir).expanduser(), "cli"))
    out.append((USER_PACKAGES_DIR, "user"))
    out.append((Path(script_file).resolve().parent / EXAMPLES_PACKAGES_REL, "shipped"))
    return out


def expand_package_files(paths: Iterable[str | Path | tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Flatten files/dirs to (json_path, source_kind) in load order."""
    files: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for item in paths:
        if isinstance(item, tuple):
            path, kind = item
        else:
            path, kind = Path(item), "shipped"
        path = Path(path).expanduser()
        candidates: list[Path]
        if path.is_dir():
            candidates = sorted(p for p in path.glob("*.json") if p.is_file())
        elif path.is_file():
            candidates = [path]
        else:
            continue
        for cand in candidates:
            resolved = cand.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append((cand, kind))
    return files


def _parse_leg(raw: dict[str, Any]) -> PackageLeg:
    leg_id = str(raw.get("id") or "").strip()
    if not leg_id:
        raise ValueError("leg missing id")
    action = str(raw.get("action") or "buy_yes").strip() or "buy_yes"
    if action != "buy_yes":
        raise ValueError(f"leg {leg_id}: action {action!r} not supported in v1")
    count = raw.get("count", "session")
    if count is None:
        count = "session"
    if not (isinstance(count, int) or count == "session"):
        if isinstance(count, str) and count.isdigit():
            count = int(count)
        elif count != "session":
            raise ValueError(f"leg {leg_id}: invalid count {count!r}")
    role = raw.get("role")
    role_s = str(role).strip() if role else None
    match_raw = raw.get("match")
    match: LegMatch | None = None
    if isinstance(match_raw, dict):
        series = str(match_raw.get("series") or "").strip().upper()
        if not series:
            raise ValueError(f"leg {leg_id}: match.series required")
        floor = match_raw.get("floor_strike")
        floor_f: float | None
        if floor is None or floor == "":
            floor_f = None
        else:
            floor_f = float(floor)
        match = LegMatch(
            series=series,
            same_team_as_trigger=bool(match_raw.get("same_team_as_trigger")),
            floor_strike=floor_f,
        )
    elif match_raw is not None:
        raise ValueError(f"leg {leg_id}: match must be an object")
    return PackageLeg(
        id=leg_id,
        action=action,
        count=count,
        role=role_s,
        required=bool(raw.get("required", False)),
        match=match,
    )


def _parse_package(raw: dict[str, Any], *, source_path: str, source_kind: str) -> Package:
    pkg_id = str(raw.get("id") or "").strip()
    if not pkg_id:
        raise ValueError("missing id")
    trigger_raw = raw.get("trigger")
    if not isinstance(trigger_raw, dict):
        raise ValueError("trigger must be an object")
    series = str(trigger_raw.get("series") or "").strip().upper()
    if not series:
        raise ValueError("trigger.series required")
    excludes_raw = trigger_raw.get("exclude_title_substrings") or []
    if isinstance(excludes_raw, str):
        excludes: tuple[str, ...] = (excludes_raw,)
    elif isinstance(excludes_raw, list):
        excludes = tuple(str(x) for x in excludes_raw if str(x))
    else:
        raise ValueError("trigger.exclude_title_substrings must be a list")
    legs_raw = raw.get("legs")
    if not isinstance(legs_raw, list) or not legs_raw:
        raise ValueError("legs must be a non-empty list")
    legs = tuple(_parse_leg(x) for x in legs_raw if isinstance(x, dict))
    if len(legs) != len(legs_raw):
        raise ValueError("each leg must be an object")
    title = str(raw.get("title") or pkg_id)
    enabled = bool(raw.get("enabled", True))
    return Package(
        id=pkg_id,
        title=title,
        enabled=enabled,
        trigger=PackageTrigger(series=series, exclude_title_substrings=excludes),
        legs=legs,
        source_path=source_path,
        source_kind=source_kind,
    )


def _packages_from_payload(
    payload: Any,
    *,
    source_path: str,
    source_kind: str,
) -> list[Package]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        raise ValueError("JSON root must be an object or array")
    packages: list[Package] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("package entry must be an object")
        packages.append(
            _parse_package(item, source_path=source_path, source_kind=source_kind)
        )
    return packages


def load_packages(
    paths: Iterable[str | Path | tuple[Path, str]],
    *,
    warn: WarnFn | None = None,
) -> list[Package]:
    """Load packages from JSON files or directories. First id wins."""
    warn_fn = warn or _eprint
    loaded: list[Package] = []
    seen_ids: set[str] = set()
    files = expand_package_files(paths)
    for path, kind in files:
        try:
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
            parsed = _packages_from_payload(
                payload, source_path=str(path), source_kind=kind
            )
        except Exception as exc:  # noqa: BLE001
            warn_fn(f"packages: skip {path}: {exc}")
            continue
        for pkg in parsed:
            if pkg.id in seen_ids:
                continue
            seen_ids.add(pkg.id)
            loaded.append(pkg)
    return loaded


def load_browse_packages(
    *,
    script_file: str | Path,
    packages_dir: str | Path | None = None,
    no_packages: bool = False,
    warn: WarnFn | None = None,
) -> tuple[list[Package], str, int]:
    """Load browse packages and a one-line stderr summary.

    Returns (packages, summary, owner_file_count).
    """
    warn_fn = warn or _eprint
    if no_packages:
        return [], "packages: disabled (--no-packages)", 0
    if packages_dir:
        cli_path = Path(packages_dir).expanduser()
        if not cli_path.exists():
            warn_fn(f"packages: skip {cli_path}: directory not found")
    search = package_search_paths(script_file=script_file, packages_dir=packages_dir)
    files = expand_package_files(search)
    owner_files = [p for p, kind in files if kind in {"cli", "user"}]
    packages = load_packages(search, warn=warn_fn)
    enabled = [pkg for pkg in packages if pkg.enabled]
    ids = ", ".join(pkg.id for pkg in enabled) if enabled else "(none)"
    summary = f"packages: {ids} (and {len(owner_files)} owner files)"
    return packages, summary, len(owner_files)


def ticker_parts(ticker: str) -> tuple[str, str, str]:
    """Return (series, game_code, suffix) from SERIES-GAMECODE-REST."""
    text = str(ticker or "").upper().strip()
    parts = [p for p in text.split("-") if p]
    if len(parts) < 2:
        return text, "", ""
    series = parts[0]
    game_code = parts[1]
    suffix = "-".join(parts[2:]) if len(parts) > 2 else ""
    return series, game_code, suffix


def team_codes_from_game_code(game_code: str) -> tuple[str, ...]:
    """Parse the two team abbreviations concatenated after the date blob."""
    text = str(game_code or "").upper().strip()
    matched = _GAME_DATE_RE.match(text)
    blob = matched.group(2) if matched else text
    if not blob:
        return ()
    found: list[tuple[str, str]] = []
    for a_len in (2, 3):
        b_len = len(blob) - a_len
        if b_len not in (2, 3):
            continue
        a, b = blob[:a_len], blob[a_len:]
        if a in NFL_TEAM_ABBREVS and b in NFL_TEAM_ABBREVS:
            found.append((a, b))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        # Prefer splits that use 3-letter codes (NYJ/NYG over NY).
        found.sort(key=lambda pair: -(len(pair[0]) + len(pair[1])))
        return found[0]
    if len(blob) == 4:
        return (blob[:2], blob[2:])
    if len(blob) == 6:
        return (blob[:3], blob[3:])
    if len(blob) == 5:
        if blob[:3] in NFL_TEAM_ABBREVS:
            return (blob[:3], blob[3:])
        if blob[2:] in NFL_TEAM_ABBREVS:
            return (blob[:2], blob[2:])
        return (blob[:3], blob[3:])
    return ()


def team_abbrev_from_ticker(ticker: str) -> str | None:
    _series, game_code, suffix = ticker_parts(ticker)
    if not suffix:
        return None
    codes = team_codes_from_game_code(game_code)
    if not codes:
        return None
    hits = [code for code in codes if suffix.startswith(code)]
    if not hits:
        return None
    return max(hits, key=len)


def football_team_uuid(row: Any) -> str | None:
    raw = _as_dict(getattr(row, "raw", None))
    custom = raw.get("custom")
    if not isinstance(custom, dict):
        return None
    val = custom.get("football_team")
    if val is None:
        return None
    text = str(val).strip()
    return text.lower() if text else None


def team_key(row: Any) -> str | None:
    """Preferred team identity: football_team UUID, else ticker-suffix abbrev."""
    uid = football_team_uuid(row)
    if uid:
        return uid
    ticker = str(getattr(row, "ticker", "") or "")
    return team_abbrev_from_ticker(ticker)


def same_team_as_trigger(trigger_row: Any, row: Any) -> bool:
    t_uid = football_team_uuid(trigger_row)
    r_uid = football_team_uuid(row)
    if t_uid and r_uid:
        return t_uid == r_uid
    t_abbr = team_abbrev_from_ticker(str(getattr(trigger_row, "ticker", "") or ""))
    r_abbr = team_abbrev_from_ticker(str(getattr(row, "ticker", "") or ""))
    if t_abbr and r_abbr:
        return t_abbr == r_abbr
    t_key = team_key(trigger_row)
    r_key = team_key(row)
    return bool(t_key and r_key and t_key == r_key)


def _raw_floor_strike(row: Any) -> float | None:
    raw = _as_dict(getattr(row, "raw", None))
    val = raw.get("floor_strike")
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def floor_strike_matches(row: Any, expected: float) -> bool:
    got = _raw_floor_strike(row)
    if got is not None:
        return abs(got - float(expected)) < 1e-9
    ticker = str(getattr(row, "ticker", "") or "").upper()
    title = str(getattr(row, "title", "") or "")
    yes_sub = str(getattr(row, "yes_sub_title", "") or "")
    exp = float(expected)
    if abs(exp - 0.5) < 1e-9:
        return ticker.endswith("-1")
    if abs(exp - 6.5) < 1e-9:
        blob = f"{title} {yes_sub}"
        return "6.5" in blob
    return False


def _title_excluded(row: Any, substrings: Sequence[str]) -> bool:
    title = str(getattr(row, "title", "") or "")
    return any(sub and sub in title for sub in substrings)


def row_series(row: Any) -> str:
    series = str(getattr(row, "series_ticker", "") or "").upper()
    if series:
        return series
    ticker = str(getattr(row, "ticker", "") or "")
    return ticker_parts(ticker)[0]


def trigger_matches(package: Package, row: Any) -> bool:
    if not package.enabled:
        return False
    if row_series(row) != package.trigger.series.upper():
        return False
    if _title_excluded(row, package.trigger.exclude_title_substrings):
        return False
    return True


def package_for_trigger(packages: Sequence[Package], row: Any) -> Package | None:
    for pkg in packages:
        if trigger_matches(pkg, row):
            return pkg
    return None


def _match_related_leg(
    leg: PackageLeg, trigger_row: Any, rows: Sequence[Any]
) -> tuple[Any | None, str | None]:
    spec = leg.match
    if spec is None:
        return None, "unresolved: no match spec"
    trigger_ticker = str(getattr(trigger_row, "ticker", "") or "").upper()
    hits: list[Any] = []
    for row in rows:
        ticker = str(getattr(row, "ticker", "") or "").upper()
        if ticker and ticker == trigger_ticker:
            continue
        if row_series(row) != spec.series.upper():
            continue
        if spec.floor_strike is not None and not floor_strike_matches(row, spec.floor_strike):
            continue
        if spec.same_team_as_trigger and not same_team_as_trigger(trigger_row, row):
            continue
        hits.append(row)
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "unresolved: no match"
    return None, f"ambiguous: {len(hits)} matches"


def resolve_package(package: Package, trigger_row: Any, rows: Sequence[Any]) -> ResolvedPackage:
    resolved = ResolvedPackage(package=package, trigger_row=trigger_row, legs=[])
    for leg in package.legs:
        if (leg.role or "") == "trigger":
            resolved.legs.append(
                ResolvedLeg(
                    id=leg.id,
                    required=bool(leg.required),
                    role="trigger",
                    row=trigger_row,
                    reason=None,
                )
            )
            continue
        row, reason = _match_related_leg(leg, trigger_row, rows)
        resolved.legs.append(
            ResolvedLeg(
                id=leg.id,
                required=bool(leg.required),
                role=leg.role,
                row=row,
                reason=reason,
            )
        )
    return resolved


def short_leg_label(row: Any) -> str:
    ticker = str(getattr(row, "ticker", "") or "")
    _series, _game, suffix = ticker_parts(ticker)
    return suffix or ticker


def preview_status_line(resolved: ResolvedPackage, *, count_yes: int, mode: str) -> str:
    ok_labels: list[str] = []
    missed: list[str] = []
    for leg in resolved.legs:
        if leg.row is not None:
            ok_labels.append(short_leg_label(leg.row))
        else:
            tag = leg.id if not leg.reason else f"{leg.id} ({leg.reason})"
            missed.append(tag)
    missed_bit = ", ".join(missed) if missed else "none"
    ok_bit = " + ".join(ok_labels) if ok_labels else "(none)"
    req = resolved.required_unresolved()
    if req:
        req_ids = ", ".join(leg.id for leg in req)
        return (
            f"PKG {resolved.package.id}: {ok_bit} · missed {missed_bit} · "
            f"required unresolved ({req_ids}) — cannot send"
        )
    return (
        f"PKG {resolved.package.id}: {ok_bit} · missed {missed_bit} · "
        f"Enter sends BUY YES x{count_yes} ({mode}) · 1=trigger only · Esc cancels"
    )


def send_summary_line(
    resolved: ResolvedPackage,
    *,
    sent_ids: Sequence[str],
    missed_ids: Sequence[str],
    trigger_only: bool = False,
) -> str:
    total = 1 if trigger_only else len(resolved.legs)
    ok_n = len(sent_ids)
    extra = " trigger-only" if trigger_only else ""
    line = f"PKG {resolved.package.id}{extra} {ok_n}/{total} ok"
    if missed_ids:
        line += " · missed " + ", ".join(missed_ids)
    return line
