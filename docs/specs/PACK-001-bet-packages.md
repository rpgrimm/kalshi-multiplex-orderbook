# PACK-001 — Sports bet packages (First TD cluster)

**Status:** specced (2026-09-17)
**Slice:** first keyboard-testable package: store + resolve + confirm + send
**Repo:** `rpgrimm/kalshi-multiplex-orderbook` @ `main` (`5d2f47a`)
**Branch (local only):** `feat/sports-bet-packages`
**Do not push / open PR / --live prod** unless the owner explicitly says so.

---

## 1. Problem

In-game, a First TD scorer is not a single bet. The owner wants one action on that player market to also send related YES bets, for example:

1. The highlighted **player First TD** market
2. That **team’s QB 1+ passing TDs**
3. **Over 6.5 first-quarter points**

Packages must be **stored** (editable, not hardcoded only) and **initiated from the sports `--browse` UI**.

---

## 2. Non-goals (this slice)

- Mention-trader changes (safety freeze)
- BUY NO / SELL in packages
- Roster files, ESPN, or external player IDs
- Auto-fire without a keystroke
- Firing on D/ST or “No Touchdown” First TD rows
- NBA/other leagues
- Pushing to GitHub

---

## 3. Live market facts (DET@BUF 2026-09-17, prod catalog)

Use these shapes; do not invent other ticker schemes.

**First TD** (`KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10`)

- Title: `Khalil Shakir: 1st Touchdown`
- `raw.custom.football_team` / `football_player` UUIDs
- Ticker suffix after `{SERIES}-{GAMECODE}-` starts with team abbrev (`BUF`, `DET`)
- Skip: `…-BUFBUFDST` (`D/ST`), `…-DETNO-TD` (`No Touchdown`)

**Pass TDs** (`KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1`)

- Title: `Josh Allen: 1+ passing touchdowns`
- `floor_strike` `0.5` = 1+; `1.5` = 2+; …
- Same `football_team` UUID as that team’s First TD players

**1Q total** (`KXNFL1QTOTAL-26SEP17DETBUF-7`)

- Title: `Will there be over 6.5 1Q points scored?`
- `floor_strike` `6.5`
- Game-level (no team UUID)

`MarketRow.raw` is already kept. Preserve `floor_strike` / `custom` from catalog JSON.

---

## 4. Storage

### 4.1 Shipped example

`examples/packages/first_td_cluster.json` (committed)

### 4.2 Owner overrides

Load extra/override JSON from (first match wins per `id`):

1. `--packages-dir DIR` if set
2. `~/.config/kalshi-multiplex-orderbook/packages/*.json`
3. `examples/packages/*.json` relative to the script (repo checkout)

`--no-packages` disables the feature (Enter stays single BUY YES).

Invalid files: skip with a stderr line at browse start; do not crash.

### 4.3 Schema (v1)

```json
{
  "id": "first_td_cluster",
  "title": "First TD cluster",
  "enabled": true,
  "trigger": {
    "series": "KXNFLFIRSTTD",
    "exclude_title_substrings": ["D/ST", "No Touchdown"]
  },
  "legs": [
    {
      "id": "player_first_td",
      "role": "trigger",
      "action": "buy_yes",
      "count": "session"
    },
    {
      "id": "qb_1_pass_td",
      "action": "buy_yes",
      "count": "session",
      "required": false,
      "match": {
        "series": "KXNFLPASSTDS",
        "same_team_as_trigger": true,
        "floor_strike": 0.5
      }
    },
    {
      "id": "q1_over_6_5",
      "action": "buy_yes",
      "count": "session",
      "required": false,
      "match": {
        "series": "KXNFL1QTOTAL",
        "floor_strike": 6.5
      }
    }
  ]
}
```

Rules:

- `role: "trigger"` is the selected market; no extra match.
- `count: "session"` → `--count-yes` / `--count`. Integer count allowed per leg later; not required in v1.
- `action` is `buy_yes` only in v1.
- `required: false` (default): unresolved leg is reported, package still sendable.
- `required: true`: cannot confirm until resolved (none in the shipped example).
- `same_team_as_trigger`: prefer `raw.custom.football_team`; fallback parse team abbrev from ticker suffix after `{SERIES}-{GAMECODE}-` (leading `BUF`/`DET`/`KC`/`NYJ`/… using the two team codes in the game id).
- `floor_strike`: numeric equality on `raw.floor_strike` (float). If missing, also accept ticker ending `-1` for 1+ pass TD and title/yes_sub containing `6.5` for the 1Q leg.
- Ambiguous match (2+ rows): pick the unique row if exactly one; if several, pick none and mark the leg unresolved with a reason (`ambiguous: N matches`). Never send both QBs.
- Game scope: only markets already in the current browse `rows` (same game discovery).

---

## 5. Keyboard (sports `--browse` only)

Keep current Enter=BUY YES for markets that **do not** match any enabled package trigger.

When the selected market **does** match `first_td_cluster` (or any enabled package):

1. **Enter** arms the package (do not submit yet). Same confirm pattern as SELL YES EXIT ALL / quit.
2. Status line lists resolved tickers (short labels) and unresolved leg ids.
3. **Enter** again submits BUY YES for every resolved leg (session count, ask+slippage IOC, existing `buy_yes_for_market`).
4. **`1`** (one) while armed: send **trigger market only** (escape hatch).
5. **Esc** / Backspace: cancel arm; no orders.
6. Quotes: `ensure_quotes` for all resolved tickers before submit. If a leg has no YES ask, skip that leg and say so; still send legs that have books. If the trigger itself has no ask, block the whole send (same as today).

Dry-run default still logs payloads only. `--live` submits each resolved leg sequentially (existing debounce/order_busy). One status line summary after: `PKG first_td_cluster 2/3 ok · missed qb_1_pass_td`.

Ctrl-H: add a short “Packages” section.

Do not change mention-trader keys.

---

## 6. Code shape

New module **`kalshi_sports_packages.py`** (keep `kalshi_sports_trader.py` from growing another 1k lines of match logic):

- `load_packages(paths) -> list[Package]`
- `team_key(row) -> str | None` (UUID or abbrev)
- `package_for_trigger(packages, row) -> Package | None`
- `resolve_package(package, trigger_row, rows) -> ResolvedPackage`
- helpers covered by unit tests

Wire into browse `handle_enter` / `BrowserState` (arm flags like `sell_confirm`).

CLI:

- `--packages-dir`
- `--no-packages`

Startup (browse): one stderr line `packages: first_td_cluster (and N owner files)`.

---

## 7. Tests (no network)

`tests/test_sports_packages.py` using **unittest** (no extra dep).

Fixture: DET@BUF rows from the catalog snapshot in this spec (Shakir First TD, Allen 1+/2+, Goff 1+, 6.5 1Q, D/ST, No TD).

Must pass:

1. Shakir First TD resolves to {Shakir FIRSTTD, Allen PASSTDS-1, 1QTOTAL 6.5} — **not** Goff, **not** Allen 2+.
2. Goff’s First TD (if in fixture) resolves to Goff 1+ pass TD, not Allen.
3. D/ST First TD is **not** a trigger.
4. No Touchdown is **not** a trigger.
5. Missing 1Q market → trigger + QB still resolve; 1Q listed unresolved.
6. Shipped JSON loads.

Also `python3 -m py_compile kalshi_sports_packages.py kalshi_sports_trader.py`.

---

## 8. Docs in the product tree (this branch)

- README sports section: packages exist; Enter on First TD arms the cluster; `--no-packages` opt-out.
- Do **not** rewrite the whole stale product plan in this slice. One README paragraph is enough.
- Fix the existing README lie only if you touch that paragraph: `--live` **does** place orders. Do not expand that into a docs rewrite.

---

## 9. Acceptance (owner keyboard)

```bash
./kalshi_sports_trader.py --demo kxnflgame-26sep17detbuf --browse --count-yes 1
```

(or whatever open game id)

1. Filter `shakir` (or a First TD player).
2. Enter → see package preview (player + QB 1+ + 6.5 1Q), not an immediate single order.
3. Esc → nothing sent.
4. Enter, Enter → dry-run three payloads in the memory log (`L`).
5. On a spread market, Enter still single BUY YES.
6. `--no-packages`: First TD Enter is single BUY YES again.

---

## 10. Out of scope follow-ups (backlog, not this PR)

- Per-leg counts / slippage
- Owner editor TUI
- Team First TD (`KXNFLFIRSTTDTEAM`) as an alternate trigger
- 1Q winner / team total legs
- Package key `p` vs Enter (if owner hates confirm)
