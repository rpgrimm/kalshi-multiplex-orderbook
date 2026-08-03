# Safety Freeze Checklist — v49 behavior

These behaviors are **load-bearing** for live operator trust. Performance work and refactors must preserve them unless the owner explicitly changes the product rules.

Source baseline: `kalshi_broadcast_word_trader.py` VERSION `2026-07-07-v49-exit-confirm` + README/cmd_adv notes in the v49 tarball.

## Environment & order gates

- [ ] `--demo` or `--prod` is required (no implicit host).
- [ ] Default is **dry-run**; real orders only with `--live`.
- [ ] Auth comes from env / explicit key path flags — not hardcoded secrets.
- [ ] Limit prices respect max bid clamp (default 97¢ unless owner changes).

## Heard-word / YES path

- [ ] Detected market is marked **heard immediately**, even if the order fails.
- [ ] Duplicate detections for the same market are ignored.
- [ ] Heard markets are excluded from later END → BUY NO.

## END → BUY NO path

- [ ] **Ctrl-E** is the only END trigger.
- [ ] Typed `END` is ordinary text (no command behavior).
- [ ] Ctrl-E **arms** only; **Enter** confirms; Esc/other cancels.
- [ ] On confirm, eligibility and quotes are **re-evaluated**.
- [ ] BUY NO only for active, not-heard, not-disqualified markets passing gates:
  - YES signal below skip threshold (default skip when YES ≥ 97¢)
  - NO ask at/above floor (default min NO ask 4¢)
- [ ] Default queue order: lowest NO ask first (highest upside first).
- [ ] `--no-end-no` disables the feature when requested.

## Trade controls (`cmd_adv` always on)

- [ ] Typing alone does not submit BUY/SELL, disqualify, or place bids.
- [ ] Enter is required to confirm highlighted manual action.
- [ ] Ctrl-D disqualify excludes market from auto YES and future Ctrl-E NO for the run.
- [ ] SELL uses authoritative position + reduce_only IOC for that leg (not only in-process cache).
- [ ] Manual Y/N buys & sells stay simple IOC even if global mode is ioc-ladder; Ctrl-E keeps ladder mode when configured.

## Exit

- [ ] Ctrl-C **arms** exit; does not kill immediately.
- [ ] Enter exits cleanly; Esc cancels and trading continues.

## Books / pricing integrity

- [ ] Multiplex path keeps `use_yes_price=true` semantics.
- [ ] Best YES bid = max(yes levels); best YES ask = min(no levels) on YES scale.
- [ ] Order pricing prefers fresh WS quotes; stale handling must not silently use wildly old books without the existing freshness policy.

## Logging / rate limits

- [ ] 429 retries use backoff; non-429 auth/validation/order errors are not blindly retried as rate limits.
- [ ] Burst logging must not block order workers (deferred log behavior preserved or improved).

## Change control

Any intentional change to the above needs:
1. Owner acknowledgment  
2. Spec note in backlog  
3. Regression coverage (manual checklist minimum; automated test preferred)
