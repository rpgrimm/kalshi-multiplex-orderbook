# Backlog — kalshi-multiplex-orderbook

**Last updated:** 2026-08-03  
**Product focus:** `kalshi_broadcast_word_trader.py` first; `kx_orderbooks` as the fast multiplex engine behind it.  
**Source of truth today:** `~/kalshi_multiplex_orderbooks_v49_exit_confirm.tar`  
**GitHub:** `rpgrimm/kalshi-multiplex-orderbook` (empty — no push without approval)

Priority legend:
- **P0** — Do first; unblocks formalization or protects live trading
- **P1** — Core performance / reliability for the main trader path
- **P2** — Important polish, structure, or secondary speedups
- **P3** — Later / optional

Status: `idea` · `ready` · `specced` · `in_progress` · `done` · `blocked`

---

## P0 — Formalize & protect

| ID | Item | Why | Status | Notes |
|----|------|-----|--------|-------|
| P0-1 | **Canonical import plan from v49 tarball** | Empty GitHub; only copy is a tar | ready | Layout proposal: package `kx_orderbooks/`, trader script(s), `cmd_adv`, `examples/`, `docs/`, `pyproject.toml`, `.gitignore`. Exclude `__pycache__`, secrets, venv, local state logs. |
| P0-2 | **Secret & path scrub checklist** | Prevent key material / absolute machine paths leaking | ready | Env-only auth (`KALSHI_*`); never commit PEM paths with keys; document required env vars only. |
| P0-3 | **Safety freeze checklist** | Speed work must not break operator-critical guards | ready | Dry-run default; `--demo`/`--prod` required; `--live` gate; heard-on-detect; Ctrl-E then Enter; typed END inert; Ctrl-C then Enter exit; price clamps; END YES/NO gates; disqualify honors. |
| P0-4 | **Owner runbook (demo → dry → live)** | Formal project needs a known-good launch path | ready | Document `cmd_adv` profiles (OTA/web-stream), rate defaults, trade-controls keybinds, log locations under `~/.local/state/...`. |
| P0-5 | **Baseline coupling audit: trader ↔ kx_orderbooks** | Can’t optimize what may be duplicated | ready | Map whether trader uses `SeriesOrderbookManager` cleanly or reimplements WS/book pieces; list double-maintenance risks. |
| P0-6 | **Local git init / branch strategy (no remote push)** | History before any GitHub write | ready | Main + short-lived perf/tweak branches; tag `v49-import` after clean import. |

---

## P1 — Make the hot path fast (measure first)

| ID | Item | Why | Status | Notes |
|----|------|-----|--------|-------|
| P1-1 | **Performance baseline harness** | “As fast as possible” needs numbers | ready | Microbenches + optional live dry-run timers: WS apply latency, get_best, keystroke→enqueue, enqueue→submit attempt, END batch first-submit, startup-to-ready. |
| P1-2 | **Order-path quote read optimization** | Pricing must see latest book with minimal lock time | idea | Prefer lock-free or copy-on-write snapshots for best bid/ask; avoid full book copies on every order. |
| P1-3 | **WS apply path optimization (`book`/`store`)** | Every delta hits this | idea | Reduce dict churn; tighter snapshot level parse; avoid extra Decimal work where cents ints suffice on hot path. |
| P1-4 | **Burst logging off the critical path** | Already partially deferred — verify no sync disk on YES/END bursts | idea | Confirm deferred buffers cover order + API logs under `cmd_adv` defaults; no fsync in workers. |
| P1-5 | **Word-match / autocomplete speed** | Large mention boards + every keystroke | idea | Prefix index / trie / compacted alias maps; avoid rescanning all markets per key. |
| P1-6 | **Order worker scheduling under END NO** | Many NO orders, rate-limited writes | idea | Preserve low-NO-ask-first priority; minimize lock hold while building payloads; parallel workers without stampeding limiter. |
| P1-7 | **WS freshness gate tuning** | Fresh enough without mandatory extra RTT | idea | When local book age < threshold, skip snapshot wait; measure stale-order vs latency tradeoff. |
| P1-8 | **Startup path: discovery + subscribe + first snapshots** | Dead time before typing matters | idea | Parallelize where safe; tighter ready condition; progress UX only if free. |

---

## P2 — Structure, correctness, small tweaks

| ID | Item | Why | Status | Notes |
|----|------|-----|--------|-------|
| P2-1 | **Modularize trader without behavior change** | 8k-line file slows safe optimization | idea | Split: cli/args, auth/hosts, markets/discovery glue, input/terminal, matching, orders, ws bridge, logging. Keep single entry script or thin launcher. |
| P2-2 | **Single source of truth for books** | Library is the multiplex engine | idea | Trader consumes `kx_orderbooks` only for books; delete duplicated protocol code if present. |
| P2-3 | **Regression tests for safety & pure helpers** | Lock in freeze checklist | idea | Unit tests: price_to_cents, END eligibility, alias normalize, confirm state machine, rate limiter math. No live API required. |
| P2-4 | **WS protocol fixture tests** | Book correctness under speed changes | idea | Golden snapshot/delta sequences → expected best quotes / crossed detection. |
| P2-5 | **README rewrite for v49 reality** | Current README is stacked version notes | idea | Sections: quickstart, cmd_adv, keybinds, safety, library API, perf notes. |
| P2-6 | **pyproject / deps hygiene** | Reproducible runs | idea | Declare trader extras (`kalshi_python_sync`, etc.); optional lockfile; document venv for `cmd_adv`. |
| P2-7 | **Owner tweak inbox (small UX/defaults)** | Explicit “little tweaks as needed” bucket | idea | Fill from owner: keybinds, default sizes/slippage, END thresholds, display density, colors, sort toggles. |
| P2-9 | **Ctrl-H in-session key help** | Operators forget binds mid-broadcast | ready | Spec: `docs/specs/UX-001-trade-control-help-and-mode-coaching.md`. Notation like Ctrl-Y. Bind `\x08`; Backspace stays DEL `\x7f` only (today both are backspace). |
| P2-10 | **Mode coaching: side keys in DISQUALIFY** | Ctrl-D then Ctrl-Y felt like “BUY YES”; actually side-only | ready | Same spec UX-001. Ctrl-Y/N while DISQUALIFY → message: need Ctrl-B BUY or Ctrl-S SELL first; stay in DISQUALIFY. Richer MODE entry blurb; startup hints Ctrl-H and “Y/N=side, B/S/D=mode”. |
| P2-8 | **Demo rate-limit & fill rehearsal script** | Confidence before prod perf claims | idea | Curated dry/live-demo checklist using existing `--demo-rate-limit-test` and trade-controls. |

---

## P3 — Later / optional

| ID | Item | Why | Status | Notes |
|----|------|-----|--------|-------|
| P3-1 | **Transcript + book replay harness** | Debug missed fills offline | idea | |
| P3-2 | **Metrics dashboard (local)** | Optional; terminal remains primary | idea | |
| P3-3 | **PyPI publish of `kx_orderbooks` alone** | Only if external reuse wanted | idea | |
| P3-4 | **Speech-to-text input mode** | Out of scope unless requested | idea | |
| P3-5 | **Multi-series / multi-event session** | Complexity; not required for formalize | idea | |
| P3-6 | **Non-Python rewrite of WS apply loop** | Only if baselines prove need | idea | |

---

## Suggested execution order (first slice)

1. **P0-3** Safety freeze checklist (doc)  
2. **P0-1 / P0-2 / P0-6** Clean import layout + local git (no push)  
3. **P0-5** Coupling audit  
4. **P0-4** Runbook  
5. **P1-1** Perf baselines  
6. Top 1–2 winners from P1-2…P1-8 based on data  
7. **P2-7** Owner tweaks as they appear  
8. GitHub push **only with explicit approval**

---

## Owner-directed tweak log

_Add items here as the owner names them; promote into P2-7 or P1 if performance-related._

| Date | Tweak | Priority | Status |
|------|-------|----------|--------|
| 2026-08-03 | Formalize project; trader-main; max speed on library+trader; small tweaks as needed | — | direction captured |
| 2026-08-03 | Ctrl-H shows key help in display (not Ctrl-?, not plus) | P2-9 | specced (UX-001) |
| 2026-08-03 | After Ctrl-D, Ctrl-Y/N should coach: need BUY (Ctrl-B) or SELL (Ctrl-S) mode; generally more helpful mode messages | P2-10 | specced (UX-001) |

---

## Explicitly not starting yet

- Writing application code in this factory pass  
- Pushing to GitHub / opening public issues  
- Live prod trading changes  
- Broad rewrite of the trader  
