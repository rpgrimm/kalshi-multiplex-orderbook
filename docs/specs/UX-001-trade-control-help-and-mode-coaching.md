# UX-001 — In-session help (Ctrl-H) + trade-control mode coaching

**Status:** implemented 2026-08-03 (trader VERSION `2026-08-03-v50-ctrl-h-help`)  
**Priority:** P2 tweak (operator clarity) — ship with next trader change slice  
**Source:** Owner request 2026-08-03  
**Baseline:** v49-exit-confirm `kalshi_broadcast_word_trader.py` + `cmd_adv` (trade-controls always on)

**Notation:** Same style as existing binds — `Ctrl-H`, `Ctrl-Y`, `Ctrl-D` (hyphen, not plus).  
Help chord is **Ctrl-H** (Control + letter H). Not `?`, not the plus key.

---

## Problem

1. There is **no in-session help** for keybinds. Operators must remember startup text or external docs.
2. Trade controls are **two-axis**:
   - **Action:** BUY (`Ctrl-B`) · SELL (`Ctrl-S`) · DISQUALIFY (`Ctrl-D`)
   - **Side:** YES (`Ctrl-Y`) · NO (`Ctrl-N`)
3. `Ctrl-Y` / `Ctrl-N` only change **side**. They do **not** leave DISQUALIFY (or switch BUY↔SELL).
4. Owner mental model: after `Ctrl-D`, pressing `Ctrl-Y` should mean “go to BUY YES.” Instead the UI stays in DISQUALIFY and only mutates an irrelevant side field — **confusing, under-explained**.

---

## Goals

- **G1.** `Ctrl-H` shows a concise on-screen help panel anytime during a session (trade-controls path; also useful if controls off for END/exit keys).
- **G2.** When action is **DISQUALIFY**, `Ctrl-Y` / `Ctrl-N` do **not** silently no-op/side-flip; they print a clear coaching message that BUY or SELL mode is required first.
- **G3.** Generally **more helpful** mode messaging: plain English, current mode restated, next keys suggested.
- **G4.** No change to order placement safety (Enter still required; END/exit confirms unchanged).

## Non-goals

- Auto-switching from DISQUALIFY → BUY on `Ctrl-Y` (owner asked for a **message**, not silent mode change). Revisit only if owner later wants “smart” switching.
- Full curses TUI rewrite.
- Changing default trading keybinds (B/S/D/Y/N/E/C/T/R).

---

## Current behavior (code facts)

| Key | Char | Effect today |
|-----|------|----------------|
| Ctrl-Y | `\x19` | `set_trade_control(side="yes")` only |
| Ctrl-N | `\x0e` | `set_trade_control(side="no")` only |
| Ctrl-B | `\x02` | `set_trade_control(action="buy")` |
| Ctrl-S | `\x13` | `set_trade_control(action="sell")` |
| Ctrl-D | `\x04` | `set_trade_control(action="disqualify")` |
| Ctrl-T | `\x14` | size edit for **current side** |
| Ctrl-R | `\x12` | refresh books/positions display |
| Ctrl-E | `\x05` | arm END batch |
| Ctrl-C | `\x03` | arm exit |
| Ctrl-H | `\x08` / `\b` | **Not help** — currently treated as **Backspace** together with DEL `\x7f` |
| Backspace | often `\x7f` | deletes one typed char |

Status line already roughly: `MODE: BUY YES | YES size: … | NO size: … | …` or `MODE: DISQUALIFY | …`.

---

## Desired behavior

### A. Help — `Ctrl-H`

**Trigger:** ASCII Ctrl-H = `\x08` (`\b`). Same family as other Ctrl-letter binds.

**Backspace conflict (must handle):**  
Today: `if ch in ("\x7f", "\b"):` → both are backspace.  
**Change:**
- **`Ctrl-H` (`\x08`)** → show help (not backspace).
- **Backspace** → **`DEL` only (`\x7f`)**, which is what most modern terminals send for the Backspace key.

Document in help and runbook: if Backspace stops working, the terminal is sending `^H` instead of DEL — fix terminal Backspace mapping, or we can add a follow-up bind later.

**On Ctrl-H:**
1. Clear typing buffer for readability (same as other mode keys) — optional but preferred so the panel is readable.
2. Do **not** confirm END or exit.
3. If a size-edit is in progress, cancel size-edit with reason `help` (or leave size-edit and only print help — prefer cancel size-edit for a clean panel).
4. Print the **help panel**, then reprint the current mode status line.
5. Optional: transcript event `help_shown` if transcript enabled.

**Help panel copy (v1):**

```text
═══ KEY HELP ═══
Modes (pick action + side):
  Ctrl-B  BUY mode          Ctrl-S  SELL mode
  Ctrl-D  DISQUALIFY mode   (excludes market from auto YES + Ctrl-E NO)
  Ctrl-Y  side YES          Ctrl-N  side NO
  Ctrl-T  edit size for the current side (digits, Enter)
  Ctrl-R  refresh books + positions

Trading:
  Type to filter/highlight  Enter  confirm highlighted action
  Ctrl-E then Enter         BUY NO on remaining qualified markets
  Typed END                 plain text (not a command)

Exit / cancel:
  Ctrl-C then Enter         exit    Esc  cancel pending confirm
  Esc / other key           cancel armed END or exit confirm

Help: Ctrl-H  (this panel)
Backspace: terminal DEL key (not Ctrl-H)

Current: MODE: <same as status line>
════════════════
```

Keep panel ≤ ~20 lines so it stays usable mid-broadcast.

### B. Side keys while in DISQUALIFY

When `trade_control_action == "disqualify"` and user presses **Ctrl-Y** or **Ctrl-N**:

1. **Do not** only flip side with no explanation (today’s silent confusion).
2. Print a coaching message, e.g.:

```text
MODE COACH: you are in DISQUALIFY (not trading).
  Ctrl-Y / Ctrl-N only select YES/NO side after you choose an order mode.
  Press Ctrl-B for BUY or Ctrl-S for SELL, then Ctrl-Y or Ctrl-N.
  Stay in DISQUALIFY: type a market and press Enter to exclude it.
  Help: Ctrl-H
```

3. Leave action as **disqualify** (no auto-switch) unless product later changes.
4. Still reprint status line so “still DISQUALIFY” is obvious.
5. Clear typeahead buffer (consistent with other mode keys).

### C. Broader “more helpful” messaging (same slice if cheap)

| Event | Messaging improvement |
|-------|------------------------|
| Enter DISQUALIFY via Ctrl-D | Explicit: “DISQUALIFY mode — no orders. Type market + Enter to exclude. Ctrl-B/Ctrl-S to trade again. Ctrl-H help.” |
| Enter SELL via Ctrl-S | “SELL mode — type owned market + Enter for reduce-only close on side YES/NO. Ctrl-Y/N pick side. Ctrl-H help.” |
| Enter BUY via Ctrl-B | “BUY mode — type market + Enter. Ctrl-Y/N pick side. Ctrl-H help.” |
| Ctrl-Y / Ctrl-N in BUY or SELL | Keep status update; prefix with `SIDE: YES` / `SIDE: NO` and full `MODE: BUY YES` etc. |
| Ctrl-T while DISQUALIFY | Allow editing sizes (they apply when back in BUY/SELL) + one-line explain. |
| Startup trade-controls blurb | Add: `Ctrl-H help` and “Ctrl-Y/N = side only; Ctrl-B/S/D = mode.” |

### D. Safety / non-regression

- No orders without Enter (unchanged).
- END / exit two-step unchanged.
- DISQUALIFY still places no orders.
- Help and coach messages are print-only.
- Backspace via DEL still edits the typeahead buffer.

---

## Acceptance checks

1. With `cmd_adv` / `--trade-controls`, **Ctrl-H** prints help including current mode; session keeps running.
2. **DEL/Backspace key** (sending `\x7f`) still deletes typed characters.
3. `Ctrl-D` then `Ctrl-Y` → coaching message; mode remains DISQUALIFY; no order.
4. `Ctrl-D` then `Ctrl-N` → same coaching pattern for NO.
5. `Ctrl-D` then `Ctrl-B` then `Ctrl-Y` → MODE BUY YES; no coach error.
6. `Ctrl-B` / `Ctrl-S` / `Ctrl-D` entry messages mention how to switch and `Ctrl-H`.
7. Dry-run and live paths identical for these UI-only behaviors.
8. Safety freeze items in `docs/SAFETY_FREEZE.md` still pass.

---

## Implementation sketch (for implement agent later)

- `input_worker`:
  - **Before** the backspace branch: if `ch == "\x08"` → `print_key_help(...)`; continue.
  - Backspace branch: only `ch == "\x7f"` (remove `"\b"` from the backspace set).
  - Ctrl-Y/N: if action == disqualify → `print_mode_coach_disqualify_side()`; else existing `set_trade_control(side=…)`.
- Add `print_key_help(state, args)` next to `print_trade_control_status`.
- Enrich mode-entry messages (B/S/D); startup blurb includes Ctrl-H.
- Manual test under real tty (cbreak); confirm Backspace key still works on owner terminal.

---

## Out of scope follow-ups (optional later)

- Config flag if owner wants Ctrl-Y from DISQUALIFY to jump to BUY YES.
- Alternate help key if a given terminal cannot separate Backspace from Ctrl-H.
- Pager/scroll for help on tiny terminals.
