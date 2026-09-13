# Product Plan — kalshi-multiplex-orderbook (factory takeover)

**Status:** Takeover plan (2026-09-13)
**Owner:** rpgrimm
**Repo:** https://github.com/rpgrimm/kalshi-multiplex-orderbook
**Local tree:** factory `product-repo` clone (no push in this planning pass)

---

## 1. One-sentence product (updated)

A **terminal-first Kalshi operator toolkit**: keep the battle-tested **mention/broadcast trader**, and add a new **sports game trader** that lets the owner jump a live game’s lines/props with keyboard-heavy navigation and fast order entry.

---

## 2. What already exists

| Asset | State |
|-------|-------|
| `kalshi_broadcast_word_trader.py` (~8.1k lines, v50.1) | Main mention/broadcast product; type-as-you-hear YES + gated END NO |
| `cmd_adv` | Advanced-tier launcher / trade-control defaults |
| `kx_orderbooks/` | Single-WS multiplex orderbook library |
| `examples/` | Series watch / poll / callback demos |
| `docs/` | Prior product plan, backlog, safety freeze, UX-001 |
| GitHub `main` | Imported and active (not empty) |

Prior factory focus: formalize + speed/safety on the **mention** path. Several backlog items remain open (perf baselines, coupling audit, modularization, tests).

---

## 3. New owner direction (this factory)

### Goal
CLI for trading **sports events as they happen** — game lines, player props, team props, game props — with efficient keyboard navigation and autocomplete.

### New entrypoint
`kalshi_sports_trader.py`
- Owner message also wrote `kalsh_sports_trader.py` (typo?). Default to **`kalshi_sports_trader.py`** unless owner insists otherwise.
- Inspiration: `kalshi_broadcast_word_trader.py` (text-focused, shortcut-heavy), but **do not** start by forking the whole monolith.

### Target UX (multi-iteration)
1. Accept a game/event id such as `kxnflgame-26sep13atlpit` (from URL tail).
2. Discover **all markets associated with that game**.
3. Present a keyboard UI to navigate categories:
   - Game lines
   - Player props
   - Team props
   - Game props
4. Autocomplete wherever possible.
5. Later: submit orders (still dry-run by default; live explicit).

### Operating style
- **Small iterations**
- Owner tests each slice
- No big-bang rewrite

---

## 4. Critical discovery fact (blocks naive v1)

For `KXNFLGAME-26SEP13ATLPIT`:

- `GET /markets?event_ticker=KXNFLGAME-26SEP13ATLPIT` → **only moneyline** (2 markets: ATL / PIT)
- Related books live under **other series** with the **same game code** `26SEP13ATLPIT`, e.g.:
  - `KXNFLSPREAD-26SEP13ATLPIT`
  - `KXNFLTOTAL-26SEP13ATLPIT`
  - `KXNFLTEAMTOTAL-26SEP13ATLPIT`
  - `KXNFLREC-26SEP13ATLPIT`, `KXNFLPASSYDS-…`, `KXNFLRSHYDS-…`, `KXNFLFIRSTTD-…`, quarters/halves, etc.

So “markets for this game” means:

```text
parse input → series + game_code
enumerate candidate sports series (NFL/league family)
for each series: query event_ticker = SERIES-GAMECODE (paginate)
merge + classify + print
```

Not: “only markets under the KXNFLGAME event ticker.”

Rate limits: naive fan-out across ~100 series will 429. Need caching, bounded candidate sets, backoff, and/or smarter series filters.

---

## 5. Product principles

1. **Sports trader is additive** — don’t break mention-trader safety freeze while building sports.
2. **Steal patterns, not the whole file** — reuse auth/host, public REST pagination ideas, keyboard philosophy; keep sports script thin at first.
3. **Read path before write path** — discovery → browse → quotes → orders.
4. **Dry-run default** when orders appear; `--demo`/`--prod` + `--live` discipline stays.
5. **Iterate with owner at the keyboard** — each slice must be manually testable.
6. **No publish/spend/live-prod changes** without explicit approval.

---

## 6. Architecture direction (evolutionary)

```text
kalshi_sports_trader.py          (new operator app)
  ├── CLI parse game id / URL tail
  ├── sports_discovery             (series×game_code market gather)
  ├── category classifier          (game lines / player / team / game props)
  ├── text UI + keybindings + autocomplete   (later slices)
  ├── order entry (later)          (reuse safety gates mindset)
  └── optional kx_orderbooks       (when live quotes needed)

kalshi_broadcast_word_trader.py (existing; maintain)
kx_orderbooks/                   (shared engine)
```

Shared library extraction only when duplication hurts (discovery helpers, auth, REST pagination).

---

## 7. Near-term goals

### Slice 0 — Planning (this pass)
- Attach repo, capture direction, backlog, discovery reality

### Slice 1 — Market lister (FIRST BUILD)
- Input: `kxnflgame-26sep13atlpit` (case-insensitive; allow URL or bare id)
- Resolve game code + league family
- Fetch associated markets across relevant series
- Print a clear text listing (ticker, title, series/event, status)
- Handle pagination + basic 429 backoff
- No orders, no curses UI yet

### Slice 2 — Classification
- Bucket into game lines / player props / team props / game props
- Stable sorting + counts per bucket

### Slice 3 — Keyboard browser
- Navigate buckets and markets with shortcuts
- Filter/autocomplete on player/team/line text

### Slice 4 — Quotes
- Subscribe selected markets via `kx_orderbooks` multiplex WS
- Show top-of-book in the text UI

### Slice 5 — Order actions
- Keyboard order entry with dry-run default and confirmations
- Start narrow (single market IOC) before fancy batch flows

---

## 8. Non-goals (for now)

- Replacing or rewriting the mention trader
- GUI/web dashboard
- Autonomous in-game botting / unattended live trading
- Speech-to-text
- Multi-league perfection on day one (NFL game path first is enough)
- PyPI release

---

## 9. Risks

| Risk | Mitigation |
|------|------------|
| Incomplete market set if series list is wrong | Start with known hit series + expandable registry; print series coverage stats |
| 429s during discovery | Backoff, concurrency limit, cache series list, optional `--series` filter |
| Category mislabels | Heuristic v1 from series ticker/title; owner-correctable mapping table |
| Monolith gravity | New file; extract shared utils only with clear specs |
| Safety regressions on mention path | Don’t edit mention hot path unless required; keep SAFETY_FREEZE |

---

## 10. Success metrics (early)

- Given `kxnflgame-26sep13atlpit`, lister returns moneyline **and** spreads/totals/props families, not just 2 markets
- Runtime acceptable for pre-game warm-up (target: tens of seconds max with backoff, improve later)
- Owner can scan the printout and say “yes, that’s the board”
- Later: keystroke path feels as deliberate as the broadcast trader

---

## 11. Open questions for owner

1. Confirm script name: `kalshi_sports_trader.py` vs `kalsh_sports_trader.py`
2. NFL-only for first vertical, or also NBA/MLB/etc. immediately?
3. Should “associated markets” include closed/settled, or open/active only? (default: open/active)
4. Preferred auth for discovery-only: public REST unauthenticated (like mention trader’s public market fetch) vs always signed SDK?
5. When we add orders: demo-first only for a while?
6. Any must-keep keybind philosophy from broadcast trader (Ctrl-Y/N side, Ctrl-B/S mode, etc.)?

---

## 12. Factory operating model

| Role | Agent |
|------|-------|
| Coordinator | `kalshi-multiplex-orderbook-foreman` |
| Spec | `...-spec` |
| Implement | `...-implement` |
| Verify | `...-verify` |
| Ship | `...-ship` |

No unsolicited cron/autonomy. Ship/publish only on explicit approval.
