# Product Plan — kalshi-multiplex-orderbook

**Status:** Initial plan (2026-08-03)  
**Owner direction:** Formalize the project; primary product is `kalshi_broadcast_word_trader.py`; keep `kx_orderbooks` and the trader **as fast as possible**; apply small tweaks as needed.  
**Constraints:** No code/publish in this planning pass. GitHub `rpgrimm/kalshi-multiplex-orderbook` exists but is empty. Canonical local source today: `~/kalshi_multiplex_orderbooks_v49_exit_confirm.tar` (v49-exit-confirm, 2026-07-07).

---

## 1. One-sentence product

A **latency-sensitive, terminal-first Kalshi broadcast/mention trader** that multiplexes live orderbooks for an entire series/event on one WebSocket and lets the operator type words as they hear them to buy YES, then confirm a gated BUY NO sweep on the rest.

---

## 2. Primary user & job-to-be-done

| | |
|---|---|
| **User** | Solo operator (owner) during live broadcasts / mention markets |
| **Job** | Hear a word → commit capital on the matching market **immediately** with correct book pricing, without fighting the UI or missing the rest of the board at END |
| **Environment** | Local terminal, prod or demo Kalshi API, Advanced-tier rate limits via `cmd_adv` |
| **Success feeling** | “I typed it, it armed/filled fast, books were fresh, nothing dangerous fired without confirmation.” |

---

## 3. Product shape (what we are formalizing)

### 3.1 Main product (P0 focus)

**`kalshi_broadcast_word_trader.py`** — the live operator tool.

Core loop:
1. Resolve series/event → load open mention markets.
2. Start **one** multiplexed orderbook WebSocket for all tickers.
3. Read keystrokes; match market words/aliases from the typed tail.
4. Queue **BUY YES** on heard markets (mark heard even if order fails — safety).
5. **Ctrl-E → Enter** confirms **BUY NO** on remaining markets under safety gates.
6. Optional manual trade controls (YES/NO/BUY/SELL/DISQUALIFY/size/refresh).
7. Dry-run by default; `--demo`/`--prod` required; `--live` for real orders.
8. Clean exit is confirmed (**Ctrl-C → Enter**).

**`cmd_adv`** — preferred launch wrapper (Advanced defaults, trade-controls always on, OTA vs web-stream slippage profiles).

### 3.2 Supporting library (critical dependency, not a separate product yet)

**`kx_orderbooks`** — multiplex orderbook engine used (or to be cleanly used) by the trader:

- Auth WS handshake
- Single-socket subscribe to many `market_tickers` with `use_yes_price=true`
- Snapshot/delta books, best quotes, dynamic add/delete/snapshot refresh
- Thread-safe store + callback/poll APIs

Formalization goal: library stays a **fast, tight dependency** of the trader — not a science project, not abandoned glue.

### 3.3 Non-products (for now)

Unless owner reopens scope:
- GUI / web dashboard
- Multi-user or hosted SaaS
- Speech-to-text automation (typing remains the input)
- Multi-exchange aggregation
- Fully autonomous market-making
- Public packaging/PyPI release process beyond making the monorepo healthy

---

## 4. Current state assessment

### Strengths
- Real, battle-evolved operator UX (v47–v49 safety: Ctrl-E confirm, typed END inert, exit confirm, disqualify, size controls).
- Correct architectural instinct: **one WS, many books**, YES-price scale.
- Serious production concerns already encoded: rate limits, 429 backoff, deferred logging during bursts, WS freshness gates, IOC vs ladder, positions/reduce-only sells.
- Clear dry-run vs live split and demo/prod endpoint selection.

### Risks / drag
- **Empty GitHub** vs ~8k-line trader + library only in a tarball — no history, no issues, no CI, easy to lose the canonical tree.
- **Monolith trader** (~8022 lines) mixes I/O, strategy, terminal UI, order protocol, and ops concerns → harder to optimize and test hot paths safely.
- **Library vs trader coupling** unclear at import time (package exists; trader is large enough it may duplicate or only partially lean on the package).
- **No automated tests** visible in the tarball.
- **Docs fragmented** (README stacks v47/v48/v49 notes).
- Performance work without baselines will thrash: need measurements before “make faster.”

### Performance-sensitive surfaces (hypotheses to verify)
1. WS message parse → book apply → best quote read on order path  
2. Order worker queue depth / lock contention under burst YES then END NO  
3. Keystroke matching / autocomplete over large market lists  
4. Logging and API stats during write bursts  
5. Snapshot freshness waits before submit  
6. REST fallbacks when WS quote is stale  
7. Python startup + market discovery time before first keystroke is useful  

---

## 5. Product principles

1. **Trader-first.** Library changes earn their keep by making the trader faster, safer, or simpler.
2. **Speed with safety.** Faster paths must not weaken confirmations, heard-marking, dry-run defaults, or price clamps.
3. **Measure, then cut.** Every performance claim needs a before/after on a named path (e.g. delta→best, keystroke→queue, queue→submit).
4. **Small tweaks over rewrites.** Prefer surgical wins inside the working v49 behavior.
5. **Demo before prod.** Performance and behavior validation on demo/`--live` demo where possible; prod changes explicit.
6. **No silent publish.** Repo import, pushes, releases only with owner approval.

---

## 6. Goals

### Near-term (formalize + baseline)
- Canonical tree in git (local first; GitHub when owner approves).
- Clear package layout: `kx_orderbooks` + trader + `cmd_adv` + examples.
- Documented runbook for demo/prod dry-run/live.
- Performance baseline harness for hot paths.
- Inventory coupling: trader → library call graph; eliminate accidental duplication where it costs latency or correctness.

### Mid-term (fast path)
- Reduce lock/queue/copy overhead on book updates and order pricing reads.
- Keep END/YES bursts within Advanced-tier limits without artificial slowness.
- Faster word match / autocomplete on large boards.
- Tighter WS freshness without extra RTTs when book is already good.
- Small UX/safety tweaks as owner requests (confirmations, defaults, display).

### Longer-term (only if needed)
- Split trader modules (input / orders / quotes / state) **without** behavior drift.
- Optional replay harness from transcripts + recorded books.
- Public-ready library docs — only if owner wants external use.

---

## 7. Non-goals (initial)

- Rewriting in another language unless measurement proves Python is the ceiling *and* owner wants that cost.
- Changing Kalshi market selection strategy beyond mention/broadcast workflow.
- Cloud deployment, always-on bots, or unattended live trading.
- Expanding to non-mention market types before the mention path is formalized and fast.

---

## 8. Architecture sketch (target, evolutionary)

```
cmd_adv / CLI
    │
    ▼
kalshi_broadcast_word_trader  (operator app)
    ├── terminal input + confirms
    ├── word/alias match
    ├── order queue workers + rate limits
    ├── positions / REST helpers
    └── quote access ──────────────► kx_orderbooks
                                        ├── discovery
                                        ├── MultiplexOrderbookWorker (1 WS)
                                        ├── OrderbookStore (apply snapshot/delta)
                                        └── BestQuote / views
```

**Performance boundary:**  
Anything on the path `WS frame → apply → best quote → order price → submit` is sacred. UI chrome and deferred logs stay off that path.

---

## 9. Operating model (this factory)

| Role | Agent | Responsibility |
|------|--------|----------------|
| Coordinator | `kalshi-multiplex-orderbook-factory` (Ledger) | Plan, backlog, owner interview, prioritization |
| Spec | `...-spec` | Turn backlog items into precise change specs |
| Implement | `...-implement` | Code changes in attached workspace/repo |
| Verify | `...-verify` | Tests, perf checks, regression of safety behavior |
| Ship | `...-ship` | Packaging, commits, PRs — only with approval |

Until a working tree is imported into a repo workspace, coordinator stays on **planning, specs, backlog**.

---

## 10. Success metrics

| Metric | Intent |
|--------|--------|
| Time to first useful keystroke | Discovery + WS subscribe + initial snapshots |
| WS delta → readable best quote | Book hot path |
| Heard-word → order enqueued | Input/match/queue path |
| Enqueued → submit attempt | Worker + rate limiter |
| END confirm → first NO submit | Burst path |
| Safety regressions | Zero: dry-run default, confirms, heard semantics, clamps |
| Operator trust | Owner willing to run `cmd_adv --prod --live` without fear of UX footguns |

Exact numeric targets TBD after first baseline run on owner hardware/network.

---

## 11. Open decisions (need owner input when relevant)

1. GitHub visibility: keep public empty repo vs private for trader secrets-adjacent ops docs?
2. Is v49 tarball the sole canonical source, or merge bits from older trees?
3. Preferred Python version / deploy host for live sessions?
4. Which tweaks are already on your mental list (display, defaults, keybinds, END gates, sizes)?
5. Acceptable risk for live prod perf experiments vs demo-only?

---

## 12. Immediate recommended sequence

1. **Import plan** — unpack v49 into a clean monorepo layout (no publish yet).  
2. **Safety freeze checklist** — behaviors that tests/specs must not break.  
3. **Perf baseline** — instrument or microbench the hot paths.  
4. **Fastest safe wins** — eliminate obvious copies/contention/log I/O on order path.  
5. **Small tweaks** — owner-directed UX/defaults, one at a time.  
6. **GitHub** — push only after owner review of tree + scrubbed secrets.

See `docs/BACKLOG.md` for prioritized items.
