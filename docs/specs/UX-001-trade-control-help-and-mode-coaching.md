# UX-001 — In-session help (Ctrl-H) + DISQUALIFY side → BUY

**Status:** implemented  
**Trader VERSION:** `2026-08-03-v50.1-disqualify-side-to-buy`  
**Commits:** v50 Ctrl-H help; v50.1 DISQUALIFY+Ctrl-Y/N → BUY with message  
**Notation:** `Ctrl-H`, `Ctrl-Y`, `Ctrl-D` (hyphen style, not plus)

---

## Goals

- **G1.** `Ctrl-H` shows in-session key help + current MODE line.
- **G2.** From **DISQUALIFY**, `Ctrl-Y` / `Ctrl-N` switch to **BUY YES / BUY NO** and print a clear human-readable mode-change message.
- **G3.** Richer BUY/SELL/DISQUALIFY entry blurbs; startup explains two-axis controls.
- **G4.** No orders without Enter; END/exit confirms unchanged.

## Non-goals

- DISQUALIFY + side key → SELL (use Ctrl-S).
- Silent mode changes with no message.

---

## Behavior

### A. Help — Ctrl-H (`\x08`)

- Print key help panel; Backspace is DEL only (`\x7f`).
- Does not confirm END/exit.

### B. DISQUALIFY + Ctrl-Y / Ctrl-N (v50.1)

When action is `disqualify`:

1. `set_trade_control(action="buy", side=yes|no)`
2. Print:

```text
MODE CHANGED: left DISQUALIFY → now BUY YES (via Ctrl-Y).
  You were excluding markets; Ctrl-Y switched you into trading on the YES side.
  Type a market and press Enter to BUY. Ctrl-D returns to DISQUALIFY. Ctrl-S for SELL. Help: Ctrl-H
NOW: MODE: BUY YES | ...
```

3. Clear typeahead. No order until Enter.

### C. Side keys in BUY/SELL

Ctrl-Y/N only change side; status prefix `SIDE:`.

### D. Mode entry (Ctrl-B / Ctrl-S / Ctrl-D)

Plain-English coach blurbs + `MODE CHANGED` status line.

---

## Acceptance

1. Ctrl-H → help panel + current mode.
2. Backspace (DEL) still deletes typed chars.
3. Ctrl-D then Ctrl-Y → **BUY YES** + MODE CHANGED message; no order.
4. Ctrl-D then Ctrl-N → **BUY NO** + message; no order.
5. In BUY/SELL, Ctrl-Y/N only flip side.
6. Safety freeze: Enter still required for orders; END/exit two-step intact.

---

## Implementation

- `print_key_help`
- `switch_disqualify_side_to_buy`
- `print_mode_entry_coach`
- `input_worker` wiring for `\x08`, `\x19`, `\x0e`, mode keys
