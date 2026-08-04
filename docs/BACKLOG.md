# Backlog — kalshi-multiplex-orderbook

**Last updated:** 2026-08-03  
**Product focus:** `kalshi_broadcast_word_trader.py` first; `kx_orderbooks` as the fast multiplex engine behind it.  
**Local tree:** `~/kalshi-multiplex-orderbook`  
**GitHub:** https://github.com/rpgrimm/kalshi-multiplex-orderbook · `main` · tag `v49-import`

## GitHub tracking

| Issue | Title |
|------:|-------|
| #1 | P0: Secret & path scrub checklist |
| #2 | P0: Owner runbook (cmd_adv) |
| #3 | P0: Coupling audit trader ↔ kx_orderbooks |
| #4 | P0: Safety freeze regression checklist |
| #5 | P1: Performance baseline harness |
| #6 | P1: Order-path quote read optimization |
| #7 | P1: WS apply path optimization |
| #8 | P1: Faster word-match / autocomplete |
| #9 | P1: END NO burst scheduling |
| #10 | P1: WS freshness gates |
| #11 | P2: Ctrl-H in-session key help |
| #12 | P2: DISQUALIFY + Ctrl-Y/N mode coaching |
| #13 | P2: Modularize trader |
| #14 | P2: Regression tests + WS fixtures |
| #15 | P2: pyproject / dependency hygiene |
| #16 | P3: Transcript + book replay harness |

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
| P0-1 | **Canonical import plan from v49 tarball** | Empty GitHub; only copy is a tar | done | Imported 2026-08-03 to `rpgrimm/kalshi-multiplex-orderbook` @ `6058a9a` tag `v49-import`. Local tree: `~/kalshi-multiplex-orderbook`. |
| P0-2 | **Secret & path scrub checklist** | Prevent key material / absolute machine paths leaking | ready | GitHub #1. Env-only auth; `.gitignore` seeded; keep scanning. |
| P0-3 | **Safety freeze checklist** | Speed work must not break operator-critical guards | ready | Doc done; GitHub #4 for walkthrough + future tests. |
| P0-4 | **Owner runbook (demo → dry → live)** | Formal project needs a known-good launch path | ready | GitHub #2 |
| P0-5 | **Baseline coupling audit: trader ↔ kx_orderbooks** | Can’t optimize what may be duplicated | ready | GitHub #3 |
| P0-6 | **Local git init / branch strategy** | History + remote | done | `main` tracking origin; tag `v49-import` pushed. |

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
| P2-9 | **Ctrl-H in-session key help** | Operators forget binds mid-broadcast | done | Spec: `docs/specs/UX-001-trade-control-help-and-mode-coaching.md`. Notation like Ctrl-Y. Bind `\x08`; Backspace stays DEL `\x7f` only (today both are backspace). |
| P2-10 | **Mode coaching: side keys in DISQUALIFY** | Ctrl-D then Ctrl-Y felt like “BUY YES”; actually side-only | done | Same spec UX-001. Ctrl-Y/N while DISQUALIFY → message: need Ctrl-B BUY or Ctrl-S SELL first; stay in DISQUALIFY. Richer MODE entry blurb; startup hints Ctrl-H and “Y/N=side, B/S/D=mode”. |
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
| 2026-08-03 | Ctrl-H shows key help in display (not Ctrl-?, not plus) | P2-9 | done (ba3b5a2) |
| 2026-08-03 | After Ctrl-D, Ctrl-Y/N should coach: need BUY (Ctrl-B) or SELL (Ctrl-S) mode; generally more helpful mode messages | P2-10 | done (ba3b5a2) |

---

## Explicitly not starting yet

- Writing application code in this factory pass  
- Pushing to GitHub / opening public issues  
- Live prod trading changes  
- Broad rewrite of the trader  
