# Backlog — kalshi-multiplex-orderbook (takeover)

**Last updated:** 2026-09-13
**Active focus:** `kalshi_sports_trader.py` first slice (list markets for a game)
**Repo:** https://github.com/rpgrimm/kalshi-multiplex-orderbook
**Local tree:** factory `product-repo` clone

Priority: **P0** now / **P1** next / **P2** structure / **P3** later
Status: `idea` · `ready` · `specced` · `in_progress` · `done` · `blocked` · `parked`

---

## Active track — Sports trader

| ID | Item | Why | Status | Notes |
|----|------|-----|--------|-------|
| S0-1 | Takeover plan + backlog refresh | New factory direction | done | docs updated 2026-09-13 |
| S0-2 | Confirm script filename spelling | Avoid churn | done | Owner confirmed `kalshi_sports_trader.py` |
| S1-1 | **Slice 1 spec: game → print all related markets** | First build | done | Implemented in script docstring + CLI |
| S1-2 | **Implement `kalshi_sports_trader.py` lister MVP** | Owner-testable | done | ~791 markets / 57 series on ATL@PIT probe |
| S1-3 | Game-code parser + URL tail accept | UX | done | |
| S1-4 | Candidate series registry (NFL game-scoped) | Completeness | done | Priority list + auto KXNFL filter |
| S1-5 | 429 backoff + concurrency limits on discovery | Reliability | done | Retry/backoff + inter-series pause |
| S1-6 | Coverage summary line | Trust | done | Coverage-by-series block |
| S2-1 | Category classifier v1 | Navigation prep | idea | game lines / player props / team props / game props |
| S2-2 | Grouped print / section headers | Readability | idea | |
| S3-1 | Keyboard browser (shortcuts) | Core UX | idea | Inspired by broadcast trader; iterate with owner |
| S3-2 | Autocomplete / filter | Speed in-game | idea | players, teams, numbers, series aliases |
| S4-1 | Wire `kx_orderbooks` for selected markets | Live TOB | idea | |
| S5-1 | Dry-run order entry on focused market | Trading | idea | Inherit demo/prod/live gates |
| S5-2 | Safety checklist for sports orders | Trust | idea | Separate from mention END semantics but same discipline |

### First execution order
1. S0-2 name confirm (can default if owner says “go”)
2. S1-1 short spec
3. S1-2…S1-6 implement + owner test on `kxnflgame-26sep13atlpit`
4. Only then S2/S3

---

## Parked — Mention trader / prior factory (still valid, not active)

Carry-forward from previous backlog; do not starve sports unless owner reprioritizes.

| ID | Item | Status | Notes |
|----|------|--------|-------|
| P0-2 | Secret & path scrub checklist | parked | GitHub #1 |
| P0-3 | Safety freeze regression walkthrough | parked | doc exists; tests later |
| P0-4 | Owner runbook (cmd_adv) | parked | GitHub #2 |
| P0-5 | Coupling audit trader ↔ kx_orderbooks | parked | GitHub #3 |
| P1-1 | Performance baseline harness | parked | |
| P1-2…P1-8 | Hot-path speed work | parked | after baselines |
| P2-1 | Modularize mention trader | parked | |
| P2-3/4 | Regression + WS fixture tests | parked | |
| P2-9/10 | Ctrl-H + DISQUALIFY coaching | done | keep frozen |
| P3-* | Replay / PyPI / STT | parked | |

---

## Owner tweak log

| Date | Tweak | Priority | Status |
|------|-------|----------|--------|
| 2026-09-13 | Take over repo; build sports trader; start with list-all-markets for game id | S1 | direction captured |
| 2026-09-13 | Keyboard-heavy text UI + autocomplete; categories game/player/team/game props | S3 | later |
| 2026-09-13 | Use broadcast trader as inspiration | — | noted |
| 2026-08-03 | Prior: Ctrl-H + DISQUALIFY mode coaching | — | done |

---

## Explicitly not starting without owner go

- Implementing S1 code (ready when you say go)
- Pushing commits / opening PRs
- Live order placement
- Editing mention-trader behavior

## Decisions log

| Date | Decision |
|------|----------|
| 2026-09-13 | Script name: `kalshi_sports_trader.py` |
| 2026-09-13 | NFL-first vertical OK |
| 2026-09-13 | Include all statuses by default (`--status all`) |
| 2026-09-13 | Public REST discovery for slice 1 |
| 2026-09-13 | Docs push approved |
