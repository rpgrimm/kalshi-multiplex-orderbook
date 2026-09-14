# kalshi-multiplex-orderbook

Type-as-you-hear **Kalshi mention/broadcast trader** backed by a **single-WebSocket multiplex orderbook** library.

| Piece | Role |
|-------|------|
| `kalshi_broadcast_word_trader.py` | Mention/broadcast trader — live operator tool |
| `kalshi_sports_trader.py` | Sports game trader (slice 1: list related markets) |
| `kx_orderbooks/` | Multiplex orderbook engine (one WS, many markets) |
| `cmd_adv` | Advanced-tier launcher (trade-controls always on) |
| `examples/` | Library watch / poll / callback demos |
| `docs/` | Product plan, backlog, safety freeze, UX specs |

**Baseline import:** v49-exit-confirm (2026-07-07).  
**Current trader:** v50.1-disqualify-side-to-buy (Ctrl-H help; DISQUALIFY+Ctrl-Y/N → BUY with message).

## Quick start

```bash
# Library + examples deps
python3 -m venv venv
source venv/bin/activate
pip install -e .
pip install kalshi_python_sync   # discovery / trader REST client

# Auth (prod example — never commit real values)
export KALSHI_PROD_API_KEY_ID='your-prod-key-id'
export KALSHI_PROD_PRIVATE_KEY_FILE="$HOME/path/to/prod_private_key.pem"
# Demo:
# export KALSHI_DEMO_API_KEY_ID='...'
# export KALSHI_DEMO_PRIVATE_KEY_FILE="$HOME/path/to/demo_private_key.pem"
```

### Watch a series (library)

```bash
python3 examples/watch_series_orderbooks.py KXTRUMPMENTIONB
```

### Sports trader (list / browse / watch markets for a game)

Same env gates as the broadcast trader: **`--demo` or `--prod` required**. Default is dry-run; `--live` is accepted for parity but this slice still does not place orders.

Catalog discovery uses public REST (no keys). Live books via `--browse` / `--watch` need env-matching API keys (`KALSHI_PROD_*` or `KALSHI_DEMO_*`).

```bash
./kalshi_sports_trader.py --prod kxnflgame-26sep13atlpit
./kalshi_sports_trader.py --prod KXNFLGAME-26SEP13ATLPIT --status all
./kalshi_sports_trader.py --prod kxnflgame-26sep13atlpit --browse
./kalshi_sports_trader.py --demo kxnflgame-26sep13atlpit --watch --watch-limit 40
# optional: limit which series are probed
./kalshi_sports_trader.py --prod kxnflgame-26sep13atlpit --series KXNFLGAME --series KXNFLSPREAD --series KXNFLTOTAL
```

Kalshi splits one game across many series that share the same game code
(`26SEP13ATLPIT` on `KXNFLSPREAD`, `KXNFLREC`, …). The sports lister probes those
related series and prints ticker / status / series / title.

### Broadcast trader

Dry-run requires `--demo` or `--prod`. Real orders need `--live`.

```bash
# Preferred launcher (Advanced defaults + trade-controls)
./cmd_adv <full-event-name> --demo
./cmd_adv <full-event-name> --prod --live --over-the-air
```

Or call the script directly:

```bash
./kalshi_broadcast_word_trader.py --prod --full-name kxnbamention-26jun05nyksas
```

## Trade controls (cmd_adv)

| Key | Meaning |
|-----|---------|
| Ctrl-B / Ctrl-S / Ctrl-D | Action: BUY / SELL / DISQUALIFY |
| Ctrl-Y / Ctrl-N | Side: YES / NO; from DISQUALIFY → BUY YES / BUY NO |
| Ctrl-T | Edit size for current side |
| Ctrl-R | Refresh books + positions |
| **Ctrl-H** | In-session key help |
| Ctrl-E then Enter | BUY NO on remaining qualified markets |
| Ctrl-C then Enter | Exit (Esc cancels) |
| Type + Enter | Confirm highlighted market action |

In **DISQUALIFY**, **Ctrl-Y / Ctrl-N** switch to **BUY YES / BUY NO** and print a clear mode-change message. Spec: `docs/specs/UX-001-trade-control-help-and-mode-coaching.md`.

## Safety

- Dry-run by default; `--live` required for real orders
- `--demo` or `--prod` required
- Heard markets marked immediately (even if order fails)
- Ctrl-E arms END; Enter confirms; typed `END` is plain text
- See `docs/SAFETY_FREEZE.md` for the full non-regression list

## Price convention (`kx_orderbooks`)

WebSocket subscribe uses `use_yes_price: true`. YES and NO levels are on the YES-price scale:

- best YES bid = max(yes levels)
- best YES ask = min(no levels)
- NO bid = 100 − yes_ask; NO ask = 100 − yes_bid

## Project docs

- [Product plan](docs/PRODUCT_PLAN.md)
- [Backlog](docs/BACKLOG.md)
- [Safety freeze](docs/SAFETY_FREEZE.md)

## License / API

Uses Kalshi’s Trade API. You need your own API keys. This repo does not ship credentials.
