#!/usr/bin/env python3
# VERSION: 2026-08-03-v50.1-disqualify-side-to-buy
"""
kalshi_broadcast_word_trader.py

Type-as-you-hear broadcast trader for Kalshi word mention markets.

New workflow:

  1. The script loads the current mention markets.
  2. A multiplexed WebSocket keeps the latest orderbook for every market updated.
  3. An input thread reads single keystrokes immediately; no Enter required.
  4. As soon as the typed character stream ends with a market word, the script
     queues a BUY YES order for that word and marks that market as "heard".
     The order worker prices from the latest WebSocket ask + slippage.
  5. Press Ctrl-E, then Enter to confirm, before the script queues BUY NO orders only for
     active markets that were not already heard, whose
     current YES price is still below the END safety threshold, and whose current
     NO ask is still above the END NO floor. By default, END queues those NOs
     from lowest NO ask to highest NO ask so the highest-upside orders go first.

Dry run is the default. You must choose --demo or --prod. Add --live only when you really want to place orders.

Examples:

  # Dry run against a live Kalshi market list
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15

  # Live mode, 1 contract, refresh bids/asks every 500ms
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --live --count 1

  # Use a fixed hard bid price for all BUY orders
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --live --hard-bid 75

  # Use a dumped get_markets.py output file for testing/dry run
  ./kalshi_broadcast_word_trader.py --prod --file markets.txt

  # Save a listening transcript for later analysis. Disabled by default.
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --transcript --transcript-file listen.txt

  # Force immediate per-order file logging, if debugging.
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --no-defer-log-writes

  # Disable automatic END => BUY NO behavior
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --no-end-no

  # Safer END behavior: only buy remaining NOs when YES is below 97c
  # and NO ask is at least 4c (> 3c). END defaults to NO ask low->high priority.
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --end-no-skip-yes-at 97 --end-no-min-no-ask 4

  # Restore old END queue order if desired.
  ./kalshi_broadcast_word_trader.py --prod KXVANCEMENTION-26MAY15 --end-no-order market

  # Paste-friendly full event name from the browser
  ./kalshi_broadcast_word_trader.py --prod --full-name kxmlbmention-26may16nyynym

  # Equivalent older form: broad MLB mention series, but trade only one game/event subset
  ./kalshi_broadcast_word_trader.py --prod KXMLBMENTION --market-event 26MAY16NYYNYM

  # Equivalent explicit ticker-prefix filter
  ./kalshi_broadcast_word_trader.py --prod KXMLBMENTION --market-prefix KXMLBMENTION-26MAY16NYYNYM-

Controls while running:

  Type words as you hear them. No Enter is needed.
  Press Ctrl-E, then Enter to confirm before finishing the stream and queuing BUY NO
  on un-heard words. END queues remaining NOs by lowest NO ask first by default.
  Press < to reprint remaining open markets and toggle alpha vs YES-lowest-first sort.
  With --trade-controls: Ctrl-B/S/D select action (BUY/SELL/DISQUALIFY); Ctrl-Y/N
  select side only (YES/NO). Ctrl-T edits the selected-side order size. Choose one
  market with autocomplete + Enter. Ctrl-R refreshes/display bids, asks, and positions.
  Ctrl-H shows in-session key help. In DISQUALIFY, Ctrl-Y/N switch you to BUY YES/NO with a clear message.
  Press Ctrl-C to arm exit confirmation; Enter exits cleanly and Esc cancels.
  By default, transcript capture is disabled and order/API file logs are buffered
  in memory during order bursts, then flushed after the queue drains or at exit.

Important safety behavior:

  - A detected market is marked heard immediately, even if the order fails.
    That prevents END from later buying NO on a word you actually heard.
  - Duplicate detections for the same market are ignored.
  - --live is required for real orders; otherwise every order is logged as dry-run.
  - Limit prices are clamped to MAX_BID_CENTS, default 97.
  - END has extra safety gates: by default it skips BUY NO when the current
    YES signal is >= 97c, marks markets as finished once YES is >= 99c, and
    only buys NO when the current NO ask is at least 4c (> 3c). END NO orders
    are queued lowest NO ask first by default, so the highest-upside NOs get
    the first write slots.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import random
import re
import select
import sys
import termios
import threading
import time
import tty
import uuid
from collections import defaultdict, deque
from urllib import error as urlerror, request as urlrequest, parse as urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

try:
    from kalshi_python_sync import Configuration, KalshiClient
except ModuleNotFoundError as exc:
    Configuration = None
    KalshiClient = Any
    KALSHI_SYNC_IMPORT_ERROR = exc
else:
    KALSHI_SYNC_IMPORT_ERROR = None

try:
    from kx_orderbooks import SeriesOrderbookManager
    from kx_orderbooks.auth import load_private_key as load_ws_private_key
except ModuleNotFoundError as exc:
    SeriesOrderbookManager = None
    load_ws_private_key = None
    KX_ORDERBOOK_IMPORT_ERROR = exc
else:
    KX_ORDERBOOK_IMPORT_ERROR = None


PROD_REST_HOST = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_REST_HOST = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_REST_HOST_ALT = "https://demo-api.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
DEMO_WS_URL_ALT = "wss://demo-api.kalshi.co/trade-api/ws/v2"

BASIC_READ_LIMIT_PER_SECOND = 200
BASIC_WRITE_LIMIT_PER_SECOND = 100
DEFAULT_READ_RATE_LIMIT_PER_SECOND = 180
DEFAULT_WRITE_RATE_LIMIT_PER_SECOND = 5

# Preferred environment-specific auth variables.
PROD_API_KEY_ID_ENV = "KALSHI_PROD_API_KEY_ID"
PROD_PRIVATE_KEY_FILE_ENV = "KALSHI_PROD_PRIVATE_KEY_FILE"
DEMO_API_KEY_ID_ENV = "KALSHI_DEMO_API_KEY_ID"
DEMO_PRIVATE_KEY_FILE_ENV = "KALSHI_DEMO_PRIVATE_KEY_FILE"

# Backward-compatible prod auth variables used by older versions of this script.
LEGACY_PROD_API_KEY_ID_ENV = "KALSHI_API_KEY_ID"
LEGACY_PROD_PRIVATE_KEY_FILE_ENV = "KALSHI_PRIVATE_KEY_FILE"

# Kept as a backward-compatible prod-only fallback when no private-key env var is set.
DEFAULT_PRIVATE_KEY_FILE = "grimm.txt"
MAX_BID_CENTS = 97
DEFAULT_END_NO_SKIP_YES_AT_CENTS = 97
DEFAULT_AUTO_FINISH_YES_AT_CENTS = 99
DEFAULT_AUTO_FINISH_NO_AT_CENTS = 1
DEFAULT_END_NO_MIN_NO_ASK_CENTS = 4
DEFAULT_END_HOTKEY = "ctrl-e"
DEFAULT_SPIKE_WINDOW_SECONDS = 2.0
DEFAULT_SPIKE_MOVE_CENTS = 8
DEFAULT_SPIKE_COOLDOWN_SECONDS = 5.0
DEFAULT_SPIKE_MAX_SPREAD_CENTS = 15
DEFAULT_DISPLAY_WIDE_SPREAD_CENTS = 15


# -----------------------------------------------------------------------------
# Small formatting / conversion helpers
# -----------------------------------------------------------------------------


def utc_now() -> str:
    """Return an ISO timestamp suitable for logs."""
    return datetime.now(timezone.utc).isoformat()


def local_clock_millis() -> str:
    """Return local wall-clock time for comparing real-time console events.

    This intentionally uses the host's local timezone and millisecond precision so
    auto-finish lines can be compared directly with other tools running on the
    same machine, such as the YES spike watcher.
    """
    return datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]


class SlidingWindowRateLimiter:
    """
    Thread-safe one-second sliding-window limiter.

    This is deliberately stricter than a token bucket: it will not allow more
    than N calls in any trailing one-second window. That is friendlier for
    Kalshi's Basic tier style per-second caps than a bursty bucket.
    """

    def __init__(self, max_calls_per_second: float, *, name: str):
        self.max_calls_per_second = float(max_calls_per_second)
        self.name = name
        self.lock = threading.RLock()
        self.calls: deque[float] = deque()
        self.total_sleep_seconds = 0.0
        self.sleep_count = 0

    def enabled(self) -> bool:
        return self.max_calls_per_second > 0

    def acquire(self) -> float:
        """Block until a call is allowed; return seconds slept."""
        if not self.enabled():
            return 0.0

        slept = 0.0
        limit = max(1, int(self.max_calls_per_second))

        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 1.0:
                    self.calls.popleft()

                if len(self.calls) < limit:
                    self.calls.append(now)
                    if slept > 0:
                        self.total_sleep_seconds += slept
                        self.sleep_count += 1
                    return slept

                sleep_for = max(0.001, 1.0 - (now - self.calls[0]))

            time.sleep(sleep_for)
            slept += sleep_for


class ApiCallStats:
    """Small thread-safe API accounting helper for pass/fail summaries."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.next_id = 0
        self.by_kind: dict[str, dict[str, int]] = {
            "read": {"attempts": 0, "success": 0, "fail": 0, "rate_limited": 0, "backoffs": 0},
            "write": {"attempts": 0, "success": 0, "fail": 0, "rate_limited": 0, "backoffs": 0},
        }

    def new_call_id(self) -> int:
        """Return a per-process id that groups retries for one logical API call."""
        with self.lock:
            self.next_id += 1
            return self.next_id

    def record(self, kind: str, field: str, n: int = 1) -> None:
        with self.lock:
            bucket = self.by_kind.setdefault(
                kind,
                {"attempts": 0, "success": 0, "fail": 0, "rate_limited": 0, "backoffs": 0},
            )
            bucket[field] = bucket.get(field, 0) + n

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self.lock:
            return {kind: dict(values) for kind, values in self.by_kind.items()}

    def total_failures(self) -> int:
        snap = self.snapshot()
        return sum(values.get("fail", 0) for values in snap.values())


def setup_runtime_api_helpers(args) -> None:
    """Attach per-run limiters, counters, and optional buffered logging."""
    args._read_limiter = SlidingWindowRateLimiter(args.read_rate_limit, name="read")
    args._write_limiter = SlidingWindowRateLimiter(args.write_rate_limit, name="write")
    args._api_stats = ApiCallStats()
    args._deferred_log_buffer = DeferredLogBuffer() if getattr(args, "defer_log_writes", True) else None


def current_api_stats(args) -> ApiCallStats | None:
    return getattr(args, "_api_stats", None)



class DeferredLogBuffer:
    """Thread-safe in-memory log buffer for hot-path order bursts."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.lines_by_file: dict[str, list[str]] = defaultdict(list)
        self.total_buffered = 0
        self.total_flushed = 0
        self.flushes = 0

    def append(self, path: str, text: str) -> None:
        if not path:
            return
        if not text.endswith("\n"):
            text += "\n"
        with self.lock:
            self.lines_by_file[str(path)].append(text)
            self.total_buffered += 1

    def flush(self) -> dict[str, int]:
        with self.lock:
            pending = {path: list(lines) for path, lines in self.lines_by_file.items() if lines}
            self.lines_by_file.clear()

        flushed_by_file: dict[str, int] = {}
        for path, lines in pending.items():
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(lines)
            flushed_by_file[path] = len(lines)

        if flushed_by_file:
            with self.lock:
                n = sum(flushed_by_file.values())
                self.total_flushed += n
                self.flushes += 1
        return flushed_by_file

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            pending = sum(len(lines) for lines in self.lines_by_file.values())
            return {
                "pending": pending,
                "total_buffered": self.total_buffered,
                "total_flushed": self.total_flushed,
                "flushes": self.flushes,
            }


def deferred_log_buffer(args) -> DeferredLogBuffer | None:
    return getattr(args, "_deferred_log_buffer", None)


def render_api_log_line(event: dict) -> str:
    """Return one compact, human-readable API log line."""
    event = dict(event)
    event.setdefault("ts", utc_now())
    order = [
        "ts",
        "result",
        "kind",
        "call_id",
        "label",
        "ticker",
        "word",
        "order_submit_api",
        "execution_mode",
        "ladder_slice",
        "ladder_total_slices",
        "ladder_target_count",
        "ladder_remaining_before",
        "ladder_price_step_cents",
        "side",
        "count",
        "limit_cents",
        "v2_book_side",
        "v2_price",
        "ask_cents",
        "price_source",
        "action",
        "trigger",
        "client_order_id",
        "order_type",
        "time_in_force",
        "attempt",
        "previous_attempt",
        "next_attempt",
        "max_retries",
        "retries_used",
        "elapsed_ms",
        "http_status",
        "slept_ms",
        "backoff_s",
        "backoff_source",
        "retry_after_header",
        "will_retry",
        "error_type",
        "error",
    ]
    parts = []
    used = set()
    for key in order:
        if key in event and event[key] is not None:
            parts.append(f"{key}={event[key]}")
            used.add(key)

    for key in sorted(k for k in event.keys() if k not in used):
        if event[key] is not None:
            parts.append(f"{key}={event[key]}")

    return " | ".join(parts) + "\n"


def api_log_event(args, event: dict) -> None:
    """Record one compact API log line, buffered by default for hot-path speed."""
    log_file = getattr(args, "api_log_file", None)
    if not log_file:
        return

    line = render_api_log_line(event)
    buf = deferred_log_buffer(args) if getattr(args, "defer_log_writes", False) else None
    if buf is not None:
        buf.append(log_file, line)
        return

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


def flush_deferred_logs(args, *, reason: str | None = None) -> dict[str, int]:
    """Flush buffered API/order logs, if deferred logging is enabled."""
    buf = deferred_log_buffer(args)
    if buf is None:
        return {}
    flushed = buf.flush()
    if flushed:
        setattr(args, "_last_deferred_log_flush_reason", reason or "unspecified")
    return flushed

def retry_after_seconds_from_headers(headers: Any) -> float | None:
    """Parse Retry-After from a response/header object, when present."""
    if headers is None:
        return None

    candidates: list[Any] = []
    if isinstance(headers, dict):
        candidates.extend(
            headers.get(k)
            for k in ("Retry-After", "retry-after", "X-RateLimit-Reset", "x-ratelimit-reset")
        )
    else:
        for key in ("Retry-After", "retry-after", "X-RateLimit-Reset", "x-ratelimit-reset"):
            try:
                candidates.append(headers.get(key))
            except Exception:
                pass

    now = time.time()
    for raw in candidates:
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # Retry-After is usually a delay. Some APIs use epoch-ish reset seconds.
        if value > now - 60:
            return max(0.0, value - now)
        return max(0.0, value)

    return None


def is_rate_limit_error_from_details(details: dict) -> bool:
    """Return True for HTTP 429 / Too Many Requests style failures."""
    status_values = [
        details.get("http_status"),
        details.get("response_status"),
        details.get("status"),
        details.get("status_code"),
    ]
    if any(str(value) == "429" for value in status_values if value is not None):
        return True

    msg = " ".join(str(details.get(key, "")) for key in ("message", "reason", "body", "response_text"))
    return "too many requests" in msg.lower() or "rate limit" in msg.lower()


def retry_after_seconds_from_details(details: dict) -> float | None:
    for key in ("headers", "response_headers"):
        value = retry_after_seconds_from_headers(details.get(key))
        if value is not None:
            return value
    return None


def call_api_with_limits(args, *, kind: str, label: str, func, meta: dict | None = None) -> Any:
    """
    Rate-limit one API call and retry only HTTP 429 failures with backoff.

    Non-429 errors are deliberately not retried because they may represent a real
    order failure, validation problem, auth failure, or network ambiguity.
    """
    stats = current_api_stats(args)
    call_id = stats.new_call_id() if stats is not None else None
    limiter = getattr(args, f"_{kind}_limiter", None)
    meta = {k: v for k, v in (meta or {}).items() if v is not None}

    def with_meta(event: dict) -> dict:
        # Order metadata makes every PASS/BACKOFF/FAIL line self-contained.
        # Event fields win if a key ever collides.
        return {**meta, **event}

    max_retries = max(0, int(getattr(args, "max_429_retries", 0)))
    attempt = 0

    while True:
        attempt += 1
        slept = limiter.acquire() if limiter is not None else 0.0
        if stats is not None:
            stats.record(kind, "attempts")

        started = time.monotonic()
        try:
            result = func()
            if isinstance(result, dict):
                returned_status = result.get("http_status") or result.get("response_status")
                if str(returned_status) == "429":
                    raise RuntimeError("HTTP 429 Too Many Requests returned without SDK exception")

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if stats is not None:
                stats.record(kind, "success")
            api_log_event(
                args,
                with_meta({
                    "result": "PASS_AFTER_RETRY" if attempt > 1 else "PASS",
                    "kind": kind,
                    "call_id": call_id,
                    "label": label,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "retries_used": attempt - 1 if attempt > 1 else 0,
                    "elapsed_ms": elapsed_ms,
                    "slept_ms": int(slept * 1000),
                }),
            )
            return result
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            details = error_details(exc)
            is_429 = is_rate_limit_error_from_details(details)
            http_status = details.get("http_status") or details.get("response_status")
            error_text = str(exc).replace("\n", " ")[:240]
            error_type = details.get("type") or type(exc).__name__

            if is_429:
                if stats is not None:
                    stats.record(kind, "rate_limited")

                if attempt <= max_retries:
                    server_retry_after = retry_after_seconds_from_details(details)
                    retry_after_header = "yes" if server_retry_after is not None else "no"
                    if server_retry_after is not None:
                        retry_after = server_retry_after
                        backoff_source = "retry-after-header"
                    else:
                        base = max(0.01, float(getattr(args, "backoff_base_seconds", 0.5)))
                        retry_after = min(
                            float(getattr(args, "backoff_max_seconds", 10.0)),
                            base * (2 ** (attempt - 1)),
                        )
                        jitter = random.random() * min(0.25, retry_after)
                        retry_after += jitter
                        backoff_source = f"client-exponential+jitter(base={base:g}s)"

                    if stats is not None:
                        stats.record(kind, "backoffs")
                    api_log_event(
                        args,
                        with_meta({
                            "result": "BACKOFF_429",
                            "kind": kind,
                            "call_id": call_id,
                            "label": label,
                            "attempt": attempt,
                            "next_attempt": attempt + 1,
                            "max_retries": max_retries,
                            "elapsed_ms": elapsed_ms,
                            "http_status": http_status,
                            "slept_ms": int(slept * 1000),
                            "backoff_s": f"{retry_after:.3f}",
                            "backoff_source": backoff_source,
                            "retry_after_header": retry_after_header,
                            "will_retry": "yes",
                            "error_type": error_type,
                            "error": error_text,
                        }),
                    )
                    time.sleep(retry_after)
                    api_log_event(
                        args,
                        with_meta({
                            "result": "RETRYING_AFTER_BACKOFF",
                            "kind": kind,
                            "call_id": call_id,
                            "label": label,
                            "attempt": attempt + 1,
                            "previous_attempt": attempt,
                            "max_retries": max_retries,
                            "backoff_s": f"{retry_after:.3f}",
                        }),
                    )
                    continue

            if stats is not None:
                stats.record(kind, "fail")
            api_log_event(
                args,
                with_meta({
                    "result": "FAIL_429" if is_429 else "FAIL",
                    "kind": kind,
                    "call_id": call_id,
                    "label": label,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "elapsed_ms": elapsed_ms,
                    "http_status": http_status,
                    "slept_ms": int(slept * 1000),
                    "will_retry": "no",
                    "error_type": error_type,
                    "error": error_text,
                }),
            )
            raise

def api_summary_lines(args) -> list[str]:
    stats = current_api_stats(args)
    if stats is None:
        return []

    snap = stats.snapshot()
    read_limiter = getattr(args, "_read_limiter", None)
    write_limiter = getattr(args, "_write_limiter", None)
    lines = [
        "API call summary:",
        f"  env={args.kalshi_env} rest={args.api_host} ws={args.ws_url}",
        (
            "  rate limits: "
            f"reads={args.read_rate_limit:g}/s (Basic cap {BASIC_READ_LIMIT_PER_SECOND}/s), "
            f"writes={args.write_rate_limit:g}/s (Basic cap {BASIC_WRITE_LIMIT_PER_SECOND}/s)"
        ),
    ]
    for kind in ("read", "write"):
        bucket = snap.get(kind, {})
        limiter = read_limiter if kind == "read" else write_limiter
        limiter_sleeps = getattr(limiter, "sleep_count", 0)
        limiter_sleep_seconds = getattr(limiter, "total_sleep_seconds", 0.0)
        result = "PASS" if bucket.get("fail", 0) == 0 else "FAIL"
        lines.append(
            f"  {kind.upper():5} {result}: attempts={bucket.get('attempts', 0)} "
            f"success={bucket.get('success', 0)} fail={bucket.get('fail', 0)} "
            f"429={bucket.get('rate_limited', 0)} backoffs={bucket.get('backoffs', 0)} "
            f"limiter_waits={limiter_sleeps} limiter_slept={limiter_sleep_seconds:.3f}s"
        )
    freshness = ws_freshness_stats(args)
    if freshness is not None:
        fsnap = freshness.snapshot()
        lines.append(
            "  ws_snapshot_refresh: "
            f"proactive_batches={fsnap['proactive_batches']} "
            f"markets={fsnap['proactive_markets']} "
            f"order_requests={fsnap['order_refresh_requests']} "
            f"fresh={fsnap['order_refresh_fresh']} "
            f"timeouts={fsnap['order_refresh_timeouts']} "
            f"order_waited={fsnap['order_refresh_wait_seconds']:.3f}s"
        )
    if getattr(args, "api_log_file", None):
        lines.append(f"  log={args.api_log_file}")
    buf = deferred_log_buffer(args)
    if buf is not None:
        bsnap = buf.snapshot()
        lines.append(
            "  deferred_log_writes: "
            f"pending={bsnap['pending']} buffered={bsnap['total_buffered']} "
            f"flushed={bsnap['total_flushed']} flushes={bsnap['flushes']}"
        )
    return lines


def print_api_summary(args) -> None:
    for line in api_summary_lines(args):
        safe_print(line)


def money_to_cents(value: Any) -> int | None:
    """
    Convert Kalshi dollar strings like "0.4300" to integer cents, e.g. 43.

    ROUND_CEILING intentionally matches the previous script: if an ask comes
    back with a fractional cent representation, round up rather than underbid.
    """
    if value is None or value == "":
        return None

    dec = Decimal(str(value))
    return int((dec * Decimal("100")).to_integral_value(rounding=ROUND_CEILING))


def cents_to_dollars(cents: int | None) -> str:
    """Pretty-print integer cents."""
    if cents is None:
        return "--"
    return f"{int(cents)}¢"


ANSI_RESET = "\033[0m"
ANSI = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
    "grey": "\033[90m",
}


def use_color() -> bool:
    """Return True when ANSI color output should be emitted."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE") == "1" or os.environ.get("FORCE_COLOR") == "1":
        return True
    return sys.stdout.isatty()


def color_text(text: Any, *styles: str) -> str:
    """Wrap text in ANSI styles when stdout is a terminal."""
    rendered = str(text)
    if not use_color() or not styles:
        return rendered
    prefix = "".join(ANSI.get(style, "") for style in styles)
    if not prefix:
        return rendered
    return f"{prefix}{rendered}{ANSI_RESET}"


def clamp_price_cents(cents: int) -> int:
    """Kalshi contract prices are cents from 1 through MAX_BID_CENTS here."""
    return max(1, min(MAX_BID_CENTS, int(cents)))


def normalize_word(text: str) -> str:
    """
    Normalize market words and typed input into comparable phrase text.

    We preserve word boundaries instead of smashing everything together. That
    makes debugging sane and prevents false matches like typing "cart" matching
    a market word "art". Punctuation still does not matter.

    Examples:
      "Social Security" -> "social security"
      "Trump!"          -> "trump"
      "J.D. Vance"      -> "j d vance"
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()




def normalize_compact_word(text: str) -> str:
    """Normalize text by removing all non-alphanumeric separators."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


NUMBER_WORDS_0_TO_19 = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
}

NUMBER_WORDS_TENS = {
    20: "twenty",
    30: "thirty",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
}

WORD_TO_NUMBER_TOKEN = {
    word: str(num) for num, word in NUMBER_WORDS_0_TO_19.items()
}


def number_to_words(num: int) -> str | None:
    """Return a normalized English phrase for small integer tokens."""
    if 0 <= num <= 19:
        return NUMBER_WORDS_0_TO_19[num]

    if 20 <= num <= 99:
        tens = (num // 10) * 10
        ones = num % 10
        tens_word = NUMBER_WORDS_TENS[tens]
        if ones == 0:
            return tens_word
        return f"{tens_word} {NUMBER_WORDS_0_TO_19[ones]}"

    return None


def expand_numeric_aliases(norm: str) -> list[str]:
    """
    Add safe number aliases for market words that contain numeric tokens.

    This fixes markets displayed as ``6`` when you type ``six`` while
    listening, while still preserving the literal digit trigger ``6``. It also
    handles simple multi-token phrases like ``top 10`` -> ``top ten``.
    """
    if not norm:
        return []

    aliases: list[str] = []
    seen: set[str] = set()

    def add(alias: str) -> None:
        alias = normalize_word(alias)
        if alias and alias not in seen:
            aliases.append(alias)
            seen.add(alias)

    add(norm)

    tokens = norm.split()
    for i, token in enumerate(tokens):
        replacement: str | None = None

        if token.isdigit():
            replacement = number_to_words(int(token))
        elif token in WORD_TO_NUMBER_TOKEN:
            replacement = WORD_TO_NUMBER_TOKEN[token]

        if replacement:
            variant = list(tokens)
            variant[i] = replacement
            add(" ".join(variant))

    return aliases




def repeated_times_trigger_aliases(market: dict) -> list[tuple[str, int]]:
    """
    Return repeated-word triggers for markets like ``Trump (3+ times)``.

    A market displayed as ``Trump (3+ times)`` should not trigger from typing
    the literal phrase "trump three times". It should count each typed/heard
    occurrence of "trump" and trigger only when the count reaches 3.
    """
    aliases: list[tuple[str, int]] = []
    seen: set[str] = set()

    # Common Kalshi display: "Trump (3+ times)". Also tolerate a missing
    # closing parenthesis or singular "time" just in case a display varies.
    pattern = re.compile(
        r"^\s*(?P<phrase>.+?)\s*\(\s*(?P<count>\d+)\s*\+\s*times?\s*\)?\s*$",
        flags=re.IGNORECASE,
    )

    # Use all possible alias fields. Some API objects keep the first alias in
    # custom_strike["Word"] but put the full display phrase in yes_sub_title.
    for raw in market_alias_source_texts(market):
        m = pattern.match(str(raw).strip())
        if not m:
            continue

        required_count = int(m.group("count"))
        if required_count < 2:
            continue

        phrase = m.group("phrase").strip()
        if not phrase:
            continue

        # Preserve slash-separated synonyms in the repeated phrase too, e.g.
        # "AI/Artificial Intelligence (3+ times)" would count either alias.
        for part in re.split(r"[/\\]+", phrase):
            norm = normalize_word(part)
            for alias in expand_numeric_aliases(norm):
                dedupe_key = f"{alias}:{required_count}"
                if dedupe_key in seen:
                    continue
                aliases.append((alias, required_count))
                seen.add(dedupe_key)

    return aliases


def repeated_trigger_preview(market: dict) -> list[str]:
    """Human-readable repeated trigger labels used by --dump-words/preview."""
    return [
        f"{alias} x{required_count}"
        for alias, required_count in repeated_times_trigger_aliases(market)
    ]


def phrase_matches_tail(recent_norm: str, word_key: str) -> bool:
    """
    Return True when the normalized input ends with word_key on a word boundary.

    recent_norm is maintained as lowercase alphanumeric words separated by
    single spaces. Matching on a boundary avoids accidental suffix matches:
    "cart" should not match "art", but "modern art" should.
    """
    recent_norm = recent_norm.rstrip()
    if not recent_norm or not word_key:
        return False
    return recent_norm == word_key or recent_norm.endswith(" " + word_key)


def model_to_dict(obj: Any) -> Any:
    """
    Convert SDK/pydantic/OpenAPI-ish objects into plain Python values that can
    be pretty-printed and logged safely.
    """
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Decimal):
        return str(obj)

    if isinstance(obj, dict):
        return {str(k): model_to_dict(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [model_to_dict(v) for v in obj]

    if hasattr(obj, "model_dump"):
        return model_to_dict(obj.model_dump())

    if hasattr(obj, "to_dict"):
        return model_to_dict(obj.to_dict())

    if hasattr(obj, "dict"):
        return model_to_dict(obj.dict())

    try:
        return {
            k: model_to_dict(v)
            for k, v in vars(obj).items()
            if not k.startswith("_")
        }
    except TypeError:
        return str(obj)


# -----------------------------------------------------------------------------
# Kalshi client / market loading
# -----------------------------------------------------------------------------


def auth_env_candidates(kalshi_env: str) -> list[tuple[str, str]]:
    """Return auth env-var pairs in preferred order for demo or prod."""
    try:
        from kx_orderbooks.auth import auth_env_candidates as _cands
        return _cands(kalshi_env)
    except Exception:
        if kalshi_env == "demo":
            return [(DEMO_API_KEY_ID_ENV, DEMO_PRIVATE_KEY_FILE_ENV)]
        return [
            (PROD_API_KEY_ID_ENV, PROD_PRIVATE_KEY_FILE_ENV),
            (LEGACY_PROD_API_KEY_ID_ENV, LEGACY_PROD_PRIVATE_KEY_FILE_ENV),
        ]


def default_auth_env_names(kalshi_env: str) -> tuple[str, str]:
    """Return the preferred API-key/private-key-file env vars for demo or prod."""
    return auth_env_candidates(kalshi_env)[0]


def key_id_hint(api_key_id: str) -> str:
    """Return a non-secret hint for logs without printing the full key id."""
    try:
        from kx_orderbooks.auth import key_id_hint as _hint
        return _hint(api_key_id)
    except Exception:
        text = str(api_key_id or "")
        if len(text) <= 12:
            return "set"
        return f"{text[:8]}...{text[-4:]}"


def choose_auth_env_pair(args) -> tuple[str, str]:
    """Pick preferred env var names (used for help text / overrides)."""
    preferred_api_env, preferred_private_env = default_auth_env_names(args.kalshi_env)
    if getattr(args, "api_key_id_env", None) or getattr(args, "private_key_file_env", None):
        return (
            args.api_key_id_env or preferred_api_env,
            args.private_key_file_env or preferred_private_env,
        )
    return preferred_api_env, preferred_private_env


def resolve_auth_settings(args) -> None:
    """
    Resolve Kalshi auth once and store non-secret metadata on args.

    Sources (first hit wins), shared with sports trader via kx_orderbooks.auth:
      1) CLI --private-key-file / explicit env-var name overrides
      2) process environment (KALSHI_PROD_* / KALSHI_DEMO_* / legacy KALSHI_*)
      3) ~/.config/kalshi-multiplex-orderbook/{prod,demo}.env
      4) split files: {prod,demo}.api-key-id + {prod,demo}.private-key.pem
      5) legacy prod ./grimm.txt private-key fallback

    The API key value is never printed.
    """
    try:
        from kx_orderbooks.auth import resolve_kalshi_auth
    except Exception as exc:
        raise SystemExit(
            "kx_orderbooks.auth is required for credential resolution. "
            f"Install package deps (pip install -e .). Import error: {exc}"
        ) from exc

    try:
        auth = resolve_kalshi_auth(
            args.kalshi_env,
            private_key_file=getattr(args, "private_key_file", None),
            api_key_id_env=getattr(args, "api_key_id_env", None),
            private_key_file_env=getattr(args, "private_key_file_env", None),
            required=True,
        )
    except Exception as exc:
        raise SystemExit(str(exc)) from exc

    assert auth is not None
    args._auth_api_key_id = auth.api_key_id
    args._auth_api_key_hint = auth.api_key_hint
    args._auth_api_key_env = auth.api_key_source
    args._auth_private_key_file = auth.private_key_path
    args._auth_private_key_file_env = auth.private_key_source
    args._auth_config_dir = auth.config_dir


def print_auth_summary(args) -> None:
    """Print auth source names only; never print secret values."""
    if not getattr(args, "_auth_api_key_env", None):
        return
    cfg = getattr(args, "_auth_config_dir", None)
    cfg_bit = f" config_dir={cfg}" if cfg else ""
    safe_print(
        f"Auth: env={args.kalshi_env} key_source={args._auth_api_key_env} "
        f"key_hint={getattr(args, '_auth_api_key_hint', 'set')} "
        f"private_key_file={args._auth_private_key_file} "
        f"private_key_source={args._auth_private_key_file_env}{cfg_bit}"
    )


def load_pem_private_key_for_auth_check(private_key_file: str) -> Any:
    """Load a PEM private key for the lightweight REST auth check."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ModuleNotFoundError as exc:
        raise RuntimeError("cryptography is required for --auth-check") from exc

    with Path(private_key_file).expanduser().open("rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_private_key_text(private_key: Any, text: str) -> str:
    """Sign text with RSA-PSS/SHA256 and return base64."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def perform_rest_auth_check(args) -> bool:
    """
    Do a tiny signed REST call with the same resolved key/private-key pair.

    This catches the most common cause of WebSocket HTTP 401: using a prod key
    with a demo private key, a demo key against prod, or stale env vars.
    """
    method = "GET"
    path = "/trade-api/v2/portfolio/balance"
    url = args.api_host.rstrip("/") + "/portfolio/balance"
    timestamp = str(int(time.time() * 1000))

    private_key = load_pem_private_key_for_auth_check(args._auth_private_key_file)
    signature = sign_private_key_text(private_key, timestamp + method + path)
    headers = {
        "KALSHI-ACCESS-KEY": args._auth_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }
    req = urlrequest.Request(url, headers=headers, method=method)

    try:
        with urlrequest.urlopen(req, timeout=args.auth_check_timeout) as resp:
            raw = resp.read(4096).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
    except urlerror.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        safe_print(
            f"Auth check FAILED: HTTP {exc.code} against {args.kalshi_env} REST "
            f"using key_env={args._auth_api_key_env} key_hint={args._auth_api_key_hint} "
            f"private_key_source={args._auth_private_key_file_env}. Body: {raw[:500]}"
        )
        return False
    except Exception as exc:
        safe_print(
            f"Auth check FAILED: {type(exc).__name__}: {exc} "
            f"against {args.kalshi_env} REST using key_env={args._auth_api_key_env} "
            f"key_hint={args._auth_api_key_hint} private_key_source={args._auth_private_key_file_env}"
        )
        return False

    balance_bits = []
    if isinstance(body, dict):
        for key in ("balance", "available_balance", "portfolio_value"):
            if key in body:
                balance_bits.append(f"{key}={body[key]}")
    suffix = f" ({', '.join(balance_bits)})" if balance_bits else ""
    safe_print(
        f"Auth check OK: {args.kalshi_env} REST accepted key_env={args._auth_api_key_env} "
        f"key_hint={args._auth_api_key_hint}{suffix}"
    )
    return True

def make_client(api_key_id: str, private_key_file: str, *, rest_host: str) -> KalshiClient:
    """Create a Kalshi REST client from an already-resolved key id and PEM file."""
    if KALSHI_SYNC_IMPORT_ERROR is not None:
        raise SystemExit(
            "kalshi_python_sync is required to fetch markets or place orders. "
            "Activate the venv where it is installed, or install it before "
            "running live/API-backed mode. `-h` / `--help` works without it. "
            f"Original import error: {KALSHI_SYNC_IMPORT_ERROR}"
        ) from KALSHI_SYNC_IMPORT_ERROR

    config = Configuration(host=rest_host)

    with open(private_key_file, "r", encoding="utf-8") as f:
        private_key = f.read()

    config.api_key_id = api_key_id
    config.private_key_pem = private_key

    return KalshiClient(config)


def split_full_market_event_name(raw: str) -> tuple[str, str]:
    """
    Split a paste-friendly browser/event name into (series_ticker, event_code).

    Example:
      kxnbamention-26jun05nyksas -> (KXNBAMENTION, 26JUN05NYKSAS)

    The first hyphen separates the broad series from the event/game code. Extra
    trailing hyphens are ignored so copied prefixes such as
    KXNBAMENTION-26JUN05NYKSAS- still work.
    """
    text = str(raw or "").strip().strip('"').strip("'").upper().strip()
    text = re.sub(r"\s+", "", text).strip("-")
    if "-" not in text:
        raise ValueError(
            "expected SERIES-EVENT, for example KXNBAMENTION-26JUN05NYKSAS"
        )

    series, event = text.split("-", 1)
    event = event.strip("-")
    if not series or not event:
        raise ValueError(
            "expected SERIES-EVENT, for example KXNBAMENTION-26JUN05NYKSAS"
        )
    if "-" in event:
        raise ValueError(
            "expected just SERIES-EVENT for --full-name, not a full market ticker; "
            "example: KXNBAMENTION-26JUN05NYKSAS"
        )

    return series, event


def market_is_open_for_trading(market: dict) -> bool:
    """Return True for currently tradeable markets across SDK/raw shapes."""
    status = str(market.get("status") or "").lower()
    # kalshi_python_sync historically exposed active, while the REST docs use open.
    return status in ("active", "open")


def event_ticker_from_args(ticker: str, args) -> str | None:
    """Return the exact event ticker when the CLI supplied --market-event/--full-name."""
    event = getattr(args, "market_event", None) if args is not None else None
    if not event:
        return None
    event = str(event).strip().strip('"').strip("'").upper().strip("-")
    if not event:
        return None
    ticker = str(ticker or "").strip().strip("-").upper()
    if ticker and not event.startswith(ticker + "-"):
        return f"{ticker}-{event}"
    return event


def public_json_get(args, path: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict:
    """Unauthenticated public Kalshi market-data GET with JSON response."""
    base = str(getattr(args, "api_host", None) or PROD_REST_HOST).rstrip("/")
    query = urlparse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = base + path
    if query:
        url += "?" + query

    req = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Kalshi public GET returned non-JSON for {url}: {raw[:200]!r}") from exc


def fetch_markets_rest_paginated(args, *, series_ticker: str | None = None, event_ticker: str | None = None) -> list[dict]:
    """Fetch markets via REST using limit/cursor and optional event_ticker filter."""
    params_base: dict[str, Any] = {"limit": 1000}
    if event_ticker:
        params_base["event_ticker"] = event_ticker
    elif series_ticker:
        params_base["series_ticker"] = series_ticker

    markets: list[dict] = []
    cursor = ""
    pages = 0
    seen_cursors: set[str] = set()
    while True:
        pages += 1
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor

        label_bits = []
        if event_ticker:
            label_bits.append(f"event={event_ticker}")
        if series_ticker and not event_ticker:
            label_bits.append(f"series={series_ticker}")
        label_bits.append(f"page={pages}")
        label = "get_markets_rest:" + ",".join(label_bits)

        data = call_api_with_limits(
            args,
            kind="read",
            label=label,
            func=lambda params=params: public_json_get(args, "/markets", params=params),
        )

        page_markets = data.get("markets") or []
        if not isinstance(page_markets, list):
            raise RuntimeError(f"Unexpected /markets response shape: {data!r}")
        markets.extend(page_markets)

        cursor = str(data.get("cursor") or "")
        if not cursor:
            break
        if cursor in seen_cursors:
            raise RuntimeError("Kalshi /markets pagination cursor repeated; refusing loop")
        seen_cursors.add(cursor)
        # Defensive ceiling so a broad series cannot spin forever if an API bug repeats cursors.
        if pages >= 20:
            raise RuntimeError("Stopped after 20 /markets pages; refusing to continue pagination loop")

    args._market_fetch_method = "rest_event" if event_ticker else "rest_series_paginated"
    args._market_fetch_pages = pages
    args._market_fetch_total = len(markets)

    return [m for m in markets if market_is_open_for_trading(m)]


def fetch_active_markets(client: KalshiClient, ticker: str, args=None) -> list[dict]:
    """Fetch open/active markets for a Kalshi series or exact event ticker."""
    event_ticker = event_ticker_from_args(ticker, args)

    # Important for broad mention series: fetch the exact event directly when we
    # know it. Otherwise the broad series first page can be unrelated markets.
    if args is not None:
        try:
            if event_ticker:
                return fetch_markets_rest_paginated(args, event_ticker=event_ticker)
            return fetch_markets_rest_paginated(args, series_ticker=ticker)
        except Exception as exc:
            args._market_fetch_rest_error = repr(exc)
            # Fall through to the SDK path for compatibility with older installs.

    call = lambda: client.get_markets(series_ticker=ticker)
    if args is not None:
        resp = call_api_with_limits(args, kind="read", label=f"get_markets:{ticker}", func=call)
        args._market_fetch_method = "sdk_series_first_page"
        args._market_fetch_pages = 1
    else:
        resp = call()

    markets: list[dict] = []
    for market in resp.markets:
        d = model_to_dict(market)
        if market_is_open_for_trading(d):
            markets.append(d)

    if args is not None:
        args._market_fetch_total = len(markets)

    return markets


def normalized_market_filter_prefixes(args) -> list[str]:
    """
    Return uppercase market ticker prefixes used to narrow a broad series.

    This is useful for broad mention series such as KXMLBMENTION, where Kalshi
    may return many individual game/event markets and you only want one subset,
    for example all tickers beginning with:

      KXMLBMENTION-26MAY16NYYNYM-

    Supported forms:
      --full-name KXMLBMENTION-26MAY16NYYNYM
        -> ticker=KXMLBMENTION, prefix=KXMLBMENTION-26MAY16NYYNYM-

      --market-event 26MAY16NYYNYM
        -> <positional ticker>-26MAY16NYYNYM-

      --market-event KXMLBMENTION-26MAY16NYYNYM
        -> KXMLBMENTION-26MAY16NYYNYM-

      --market-prefix KXMLBMENTION-26MAY16NYYNYM-
        -> exact prefix as provided, uppercased
    """
    prefixes: list[str] = []

    for raw_prefix in getattr(args, "market_prefix", []) or []:
        prefix = str(raw_prefix).strip().strip('"').strip("'").upper()
        if prefix:
            prefixes.append(prefix)

    event = getattr(args, "market_event", None)
    if event:
        event = str(event).strip().strip('"').strip("'").upper().strip("-")
        ticker = (getattr(args, "ticker", None) or "").strip().strip("-").upper()

        if ticker and not event.startswith(ticker + "-"):
            prefix = f"{ticker}-{event}-"
        else:
            prefix = f"{event}-"

        prefixes.append(prefix)

    # Preserve CLI order while removing duplicates.
    seen: set[str] = set()
    unique_prefixes: list[str] = []
    for prefix in prefixes:
        if prefix not in seen:
            unique_prefixes.append(prefix)
            seen.add(prefix)

    return unique_prefixes


def filter_markets_by_ticker_prefix(markets: list[dict], args) -> list[dict]:
    """Filter loaded markets to one or more market ticker prefixes."""
    prefixes = normalized_market_filter_prefixes(args)
    args._market_filter_prefixes = prefixes

    if not prefixes:
        args._market_filter_status = "none"
        return markets

    filtered = [
        market
        for market in markets
        if any(str(market.get("ticker", "")).upper().startswith(prefix) for prefix in prefixes)
    ]

    args._market_filter_status = (
        f"prefixes={','.join(prefixes)} kept={len(filtered)}/{len(markets)}"
    )

    if not filtered:
        available_preview = ", ".join(
            sorted(str(m.get("ticker", "")) for m in markets if m.get("ticker"))[:20]
        )
        raise SystemExit(
            "Market filter matched 0 active markets.\n"
            f"Filter prefix(es): {', '.join(prefixes)}\n"
            f"Loaded active markets: {len(markets)}\n"
            f"First loaded tickers: {available_preview or '(none)'}"
        )

    return filtered


def extract_json_objects(text: str) -> list[dict]:
    """
    Parse the dump format your old helper used:

      "TICKER"
      { ...market json... }
      "TICKER"
      { ...market json... }

    This scans and extracts each top-level JSON object, ignoring ticker lines.
    """
    objects: list[dict] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start : i + 1]
                try:
                    objects.append(json.loads(chunk))
                except json.JSONDecodeError as e:
                    raise SystemExit(f"Failed to parse JSON object: {e}") from e
                start = None

    return objects


def load_markets_from_file(path: str) -> list[dict]:
    """Load active markets from a saved dump file."""
    text = Path(path).read_text(encoding="utf-8")
    markets = extract_json_objects(text)
    return [m for m in markets if market_is_open_for_trading(m)]


def market_word(market: dict) -> str:
    """Return the human word/phrase for a mention market."""
    custom = market.get("custom_strike") or {}

    # Prefer the slash-bearing display field when present. Kalshi often stores
    # a compact custom strike like "AI" while the UI/subtitle says
    # "AI / Artificial Intelligence". Showing the display phrase makes logs and
    # END skip messages line up with the actual one market being traded.
    for value in (market.get("yes_sub_title"), market.get("subtitle")):
        if value and re.search(r"[/\\]", str(value)):
            return str(value)

    return (
        custom.get("Word")
        or market.get("yes_sub_title")
        or market.get("subtitle")
        or market.get("ticker")
        or "UNKNOWN"
    )


def market_label(market: dict) -> str:
    """Return a stable display label like 'Trump [KX...]'."""
    name = str(market_word(market))
    ticker = str(market.get("ticker") or "").strip()
    return f"{name} [{ticker}]" if ticker and ticker != name else name


def market_alias_source_texts(market: dict) -> list[str]:
    """Return all market text fields that may contain trigger aliases.

    Some Kalshi objects put only the first alias in ``custom_strike["Word"]``
    while the display field contains the full slash-separated market name, e.g.
    ``yes_sub_title == "AI / Artificial Intelligence"``.  We collect all of
    those fields and later map every alias back to the same market key/ticker.
    """
    custom = market.get("custom_strike") or {}
    candidates = [
        custom.get("Word"),
        market.get("yes_sub_title"),
        market.get("subtitle"),
    ]

    texts: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        texts.append(text)
        seen.add(text)

    if not texts:
        ticker = str(market.get("ticker") or "UNKNOWN").strip()
        texts.append(ticker or "UNKNOWN")

    return texts


def market_trigger_aliases(market: dict) -> list[str]:
    """
    Return every typed phrase that should trigger this market.

    Kalshi mention markets often encode synonyms in one display field, for
    example:

      ``spy/spying``
      ``deal/settle``
      ``invest/invested/investment``
      ``AI/Artificial Intelligence``

    Every alias returned here still maps to one canonical market key/ticker.
    So typing either ``AI`` or ``Artificial Intelligence`` marks the same market
    heard, and END will not buy NO for the alias you did not type.
    """
    # Markets like "Trump (3+ times)" are handled by the repeated-word
    # counter. Do not index their normalized literal phrase as
    # "trump 3 times" / "trump three times".
    if repeated_times_trigger_aliases(market):
        return []

    aliases: list[str] = []
    seen: set[str] = set()

    # Slash is the important separator for these markets. Preserve spaces
    # inside each side of the slash so phrases still work naturally. Numeric
    # expansion lets a displayed market word like "6" trigger from either
    # typing "6" or "six".
    for raw in market_alias_source_texts(market):
        for part in re.split(r"[/\\]+", raw):
            norm = normalize_word(part)
            for alias in expand_numeric_aliases(norm):
                if alias in seen:
                    continue
                aliases.append(alias)
                seen.add(alias)

    # Defensive fallback: if the fields were weird and splitting found nothing,
    # index the normalized display string.
    if not aliases:
        norm = normalize_word(market_word(market))
        for alias in expand_numeric_aliases(norm):
            if alias not in seen:
                aliases.append(alias)
                seen.add(alias)

    return aliases


def market_key(market: dict) -> str:
    """Stable key used to avoid duplicate orders."""
    return market.get("ticker") or market_word(market)


def side_ask_cents(market: dict, side: str) -> int | None:
    """Return the ask for YES or NO as integer cents."""
    side = side.lower()
    if side == "yes":
        return money_to_cents(market.get("yes_ask_dollars"))
    if side == "no":
        return money_to_cents(market.get("no_ask_dollars"))
    raise ValueError(f"Bad side: {side}")


def side_bid_cents(market: dict, side: str) -> int | None:
    """Return the bid for YES or NO as integer cents."""
    side = side.lower()
    if side == "yes":
        return money_to_cents(market.get("yes_bid_dollars"))
    if side == "no":
        return money_to_cents(market.get("no_bid_dollars"))
    raise ValueError(f"Bad side: {side}")



class WebSocketFreshnessStats:
    """Thread-safe diagnostics for proactive and order-path WS snapshot refreshes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proactive_batches = 0
        self.proactive_markets = 0
        self.order_refresh_requests = 0
        self.order_refresh_fresh = 0
        self.order_refresh_timeouts = 0
        self.order_refresh_wait_seconds = 0.0

    def record_proactive(self, market_count: int) -> None:
        with self._lock:
            self.proactive_batches += 1
            self.proactive_markets += int(market_count)

    def record_order_refresh(self, *, fresh: bool, waited_seconds: float) -> None:
        with self._lock:
            self.order_refresh_requests += 1
            self.order_refresh_wait_seconds += max(0.0, float(waited_seconds))
            if fresh:
                self.order_refresh_fresh += 1
            else:
                self.order_refresh_timeouts += 1

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "proactive_batches": self.proactive_batches,
                "proactive_markets": self.proactive_markets,
                "order_refresh_requests": self.order_refresh_requests,
                "order_refresh_fresh": self.order_refresh_fresh,
                "order_refresh_timeouts": self.order_refresh_timeouts,
                "order_refresh_wait_seconds": self.order_refresh_wait_seconds,
            }


def ws_freshness_stats(args) -> WebSocketFreshnessStats | None:
    return getattr(args, "_ws_freshness_stats", None)


def _ws_view_age_ms(view, now: float | None = None) -> int | None:
    if view is None or not getattr(view, "last_local_ts", 0):
        return None
    if now is None:
        now = time.time()
    return max(0, int((now - view.last_local_ts) * 1000))


def _ws_quote_from_view(view, *, refresh_meta: dict | None = None) -> dict | None:
    if view is None or not view.ready:
        return None

    b = view.best
    quote = {
        "price_source": "ws_orderbook",
        "ws_seq": view.seq,
        "ws_age_ms": _ws_view_age_ms(view),
        "ws_last_exchange_ts_ms": view.last_exchange_ts_ms,
        "yes_bid_cents": b.yes_bid_cents,
        "yes_bid_qty": b.yes_bid_qty,
        "yes_ask_cents": b.yes_ask_cents,
        "yes_ask_qty": b.yes_ask_qty,
        "no_bid_cents": b.no_bid_cents,
        # With Kalshi use_yes_price=True, the top NO bid is the same level
        # as the top YES ask, and the top NO ask is the same level as the
        # top YES bid. Keep quantities explicit for display/logging.
        "no_bid_qty": b.yes_ask_qty,
        "no_ask_cents": b.no_ask_cents,
        "no_ask_qty": b.yes_bid_qty,
        "spread_cents": b.spread_cents,
        "mid_yes_cents": b.mid_yes_cents,
    }
    if refresh_meta:
        quote.update(refresh_meta)
    return quote


def request_ws_snapshots(args, market_tickers: list[str], *, source: str) -> bool:
    """Ask the existing WS subscription to resend snapshots without reconnecting."""
    if not market_tickers or not getattr(args, "ws_orderbook", False):
        return False
    manager = getattr(args, "_orderbook_manager", None)
    if manager is None:
        return False
    try:
        queued = manager.request_snapshots(market_tickers)
        if queued is False:
            return False
    except Exception:
        # This is a best-effort cache repair. The current quote / normal reconnect
        # path remains usable even if a snapshot request cannot be queued.
        logging.getLogger(__name__).debug(
            "Unable to queue WS snapshot refresh (%s) for %s market(s)",
            source,
            len(market_tickers),
            exc_info=True,
        )
        return False
    return True


def refresh_ws_quote_for_order(args, ticker: str, side: str) -> dict | None:
    """Return a WS quote, actively refreshing a stale/missing subscribed cache first.

    Normal streaming deltas are still the fast path. Snapshot repair happens only
    when the cached ticker is older than --ws-snapshot-max-age-ms (or not ready).
    It does not skip the order: if the refresh misses the short wait window, the
    caller receives the existing WS quote and can continue under current behavior.
    """
    if not getattr(args, "ws_orderbook", False):
        return None

    manager = getattr(args, "_orderbook_manager", None)
    if manager is None:
        return None

    side = side.lower()
    max_age_ms = int(getattr(args, "ws_snapshot_max_age_ms", 0) or 0)
    wait_ms = int(getattr(args, "ws_fresh_order_wait_ms", 0) or 0)
    view_before = manager.get_view(ticker)
    quote_before = _ws_quote_from_view(view_before)

    quote_is_usable = (
        quote_before is not None
        and quote_before.get(f"{side}_ask_cents") is not None
    )
    quote_age_ms = quote_before.get("ws_age_ms") if quote_before else None
    needs_refresh = (
        not quote_is_usable
        or (max_age_ms > 0 and (quote_age_ms is None or quote_age_ms > max_age_ms))
    )

    if not needs_refresh:
        return quote_before

    before_local_ts = getattr(view_before, "last_local_ts", 0.0) if view_before is not None else 0.0
    requested = request_ws_snapshots(args, [ticker], source="order_path")
    if not requested:
        if quote_before is not None:
            quote_before.update({
                "ws_snapshot_refresh": "request_unavailable",
                "ws_snapshot_refresh_wait_ms": 0,
            })
        return quote_before

    started = time.monotonic()
    deadline = started + max(0, wait_ms) / 1000.0
    refreshed_quote = None
    fresh = False

    while True:
        view_after = manager.get_view(ticker)
        refreshed_quote = _ws_quote_from_view(view_after)
        if (
            refreshed_quote is not None
            and getattr(view_after, "last_local_ts", 0.0) > before_local_ts
            and refreshed_quote.get(f"{side}_ask_cents") is not None
        ):
            fresh = True
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.003)

    waited_seconds = time.monotonic() - started
    stats = ws_freshness_stats(args)
    if stats is not None:
        stats.record_order_refresh(fresh=fresh, waited_seconds=waited_seconds)

    if fresh and refreshed_quote is not None:
        refreshed_quote.update({
            "ws_snapshot_refresh": "fresh_snapshot",
            "ws_snapshot_refresh_wait_ms": int(waited_seconds * 1000),
        })
        return refreshed_quote

    fallback_quote = refreshed_quote or quote_before
    if fallback_quote is not None:
        fallback_quote.update({
            "ws_snapshot_refresh": "timed_out_using_cached_quote",
            "ws_snapshot_refresh_wait_ms": int(waited_seconds * 1000),
        })
    return fallback_quote


def ws_snapshot_freshness_worker(*, args, state, manager, market_tickers: list[str]) -> None:
    """Keep quiet subscribed books fresh by periodically requesting WS snapshots.

    Kalshi's orderbook_delta channel sends an initial snapshot then deltas. For a
    quiet market that can leave the local timestamp old even though the socket is
    healthy. The get_snapshot action returns a new snapshot without changing the
    subscription, so this worker refreshes only books older than the configured
    max age. It is intentionally silent in normal operation.
    """
    max_age_ms = int(getattr(args, "ws_snapshot_max_age_ms", 0) or 0)
    if max_age_ms <= 0:
        return

    # Scan more frequently than the target age, but avoid a busy loop.
    scan_seconds = max(0.05, min(0.25, max_age_ms / 2000.0))
    request_cooldown_seconds = max(0.05, max_age_ms / 1000.0)
    last_requested: dict[str, float] = {}

    while not state.stop_event.wait(scan_seconds):
        if not getattr(manager, "connected", False) or not getattr(manager, "subscribed", False):
            continue

        now = time.time()
        stale_tickers: list[str] = []
        for ticker in market_tickers:
            view = manager.get_view(ticker)
            age_ms = _ws_view_age_ms(view, now)
            stale = view is None or not view.ready or age_ms is None or age_ms > max_age_ms
            if not stale:
                continue
            if now - last_requested.get(ticker, 0.0) < request_cooldown_seconds:
                continue
            stale_tickers.append(ticker)

        if not stale_tickers:
            continue

        if request_ws_snapshots(args, stale_tickers, source="background_freshness"):
            for ticker in stale_tickers:
                last_requested[ticker] = now
            stats = ws_freshness_stats(args)
            if stats is not None:
                stats.record_proactive(len(stale_tickers))


def ws_quote_for_market(args, ticker: str) -> dict | None:
    """Return the latest cached WebSocket top-of-book quote for ticker, if ready."""
    if not getattr(args, "ws_orderbook", False):
        return None

    manager = getattr(args, "_orderbook_manager", None)
    if manager is None:
        return None

    return _ws_quote_from_view(manager.get_view(ticker))


def rest_quote_for_market(market: dict) -> dict:
    """Return the older REST/SDK market snapshot quote fields."""
    return {
        "price_source": "rest_snapshot",
        "ws_seq": None,
        "ws_age_ms": None,
        "ws_last_exchange_ts_ms": None,
        "ws_snapshot_refresh": None,
        "ws_snapshot_refresh_wait_ms": None,
        "yes_bid_cents": side_bid_cents(market, "yes"),
        "yes_bid_qty": None,
        "yes_ask_cents": side_ask_cents(market, "yes"),
        "yes_ask_qty": None,
        "no_bid_cents": side_bid_cents(market, "no"),
        "no_bid_qty": None,
        "no_ask_cents": side_ask_cents(market, "no"),
        "no_ask_qty": None,
        "spread_cents": None,
        "mid_yes_cents": None,
    }


def latest_quote_for_order(args, market: dict, side: str) -> dict:
    """
    Prefer the live WebSocket quote, falling back to the REST market snapshot.

    For BUY YES, base price is latest YES ask.
    For BUY NO, base price is latest NO ask.
    """
    ticker = market["ticker"]
    side = side.lower()
    if side not in ("yes", "no"):
        raise ValueError(f"Bad side: {side}")

    quote = refresh_ws_quote_for_order(args, ticker, side)
    if quote is not None and quote.get(f"{side}_ask_cents") is not None:
        return quote

    if getattr(args, "ws_required", False) and getattr(args, "ws_orderbook", False):
        raise RuntimeError(
            f"WebSocket orderbook for {ticker} is not ready or has no {side.upper()} ask"
        )

    return rest_quote_for_market(market)


def latest_quote_for_market(args, market: dict) -> dict:
    """
    Return the freshest available quote context for filtering/risk checks.

    Unlike latest_quote_for_order(), this does not require a particular side's
    ask to be present. It is meant for safety decisions such as END filtering.
    """
    ticker = market["ticker"]
    quote = ws_quote_for_market(args, ticker)
    if quote is not None:
        return quote
    return rest_quote_for_market(market)


def quote_yes_guard_cents(quote: dict) -> int | None:
    """
    Return the YES price signal used by END safety logic.

    Prefer YES bid and mid because they indicate where the market is actually
    valuing YES. Fall back to YES ask only when bid/mid are unavailable, so one
    stray expensive ask does not unnecessarily mark a market finished.
    """
    primary_candidates = [
        quote.get("yes_bid_cents"),
        quote.get("mid_yes_cents"),
    ]
    primary_candidates = [
        int(v) for v in primary_candidates if isinstance(v, int)
    ]

    if primary_candidates:
        return max(primary_candidates)

    ask = quote.get("yes_ask_cents")
    if isinstance(ask, int):
        return ask

    return None


def quote_yes_probability_cents(quote: dict) -> int | float | None:
    """Return the best display estimate of the current YES probability."""
    mid = quote.get("mid_yes_cents")
    if isinstance(mid, (int, float)):
        return mid

    # Without a WebSocket midpoint, the ask is the clearest visible price for
    # tiny finished-NO markets: YES ask 1c should print as YES=1c, not 0c.
    ask = quote.get("yes_ask_cents")
    if isinstance(ask, (int, float)):
        return ask

    bid = quote.get("yes_bid_cents")
    if isinstance(bid, (int, float)):
        return bid
    return None


def quote_yes_low_finish_cents(quote: dict) -> int | None:
    """Return a conservative low-YES signal for detecting finished NO markets."""
    candidates = [
        quote.get("yes_ask_cents"),
        quote.get("mid_yes_cents"),
    ]
    candidates = [int(v) for v in candidates if isinstance(v, int)]
    if candidates:
        return min(candidates)

    bid = quote.get("yes_bid_cents")
    if isinstance(bid, int):
        return bid

    return None


def quote_prob_summary(
    quote: dict,
    yes_cents: int | float | None = None,
    *,
    include_spread: bool = False,
    args=None,
) -> str:
    """Compact display of the current YES/NO probabilities."""
    if yes_cents is None:
        yes_cents = quote_yes_probability_cents(quote)

    no_prob = None if yes_cents is None else max(0, min(100, 100 - float(yes_cents)))
    text = f"YES={format_cents_value(yes_cents)} NO≈{format_cents_value(no_prob)}"
    if include_spread:
        book = quote_dual_book_summary(quote, args=args)
        if book:
            text += " | " + book
    return text


def format_cents_value(value: int | float | None) -> str:
    """Pretty-print cents while preserving half-cent midpoints."""
    if value is None:
        return "--"
    try:
        f = float(value)
    except Exception:
        return "--"
    if f.is_integer():
        return f"{int(f)}¢"
    return f"{f:.1f}¢"


def format_quote_qty(value: object) -> str:
    """Pretty-print a top-of-book quantity without noisy trailing zeros."""
    if value is None or value == "":
        return ""
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def quote_side_qty(quote: dict, side: str, position: str) -> object:
    """Return top-of-book quantity for side=YES/NO and position=bid/ask."""
    return quote.get(f"{side.lower()}_{position.lower()}_qty")


def quote_side_spread_cents(quote: dict, side: str) -> int | float | None:
    """Return bid/ask spread for YES or NO from a quote context."""
    side = side.lower()
    bid = quote.get(f"{side}_bid_cents")
    ask = quote.get(f"{side}_ask_cents")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        return float(ask) - float(bid)
    return None


def quote_has_wide_spread(
    quote: dict,
    *,
    threshold_cents: int | float,
    side: str | None = None,
) -> bool:
    """Return True when either side, or one requested side, has a wide spread."""
    sides = [side.lower()] if side else ["yes", "no"]
    for s in sides:
        spread = quote_side_spread_cents(quote, s)
        if spread is not None and spread > threshold_cents:
            return True
    return False


def format_book_price(cents: int | float | None, qty: object = None) -> str:
    """Pretty top-of-book price plus optional quantity, e.g. 44¢x1."""
    text = format_cents_value(cents)
    qty_text = format_quote_qty(qty)
    if qty_text:
        text += f"x{qty_text}"
    return text


def quote_side_book_summary(quote: dict, side: str, *, args=None) -> str:
    """Return a compact YES or NO bid/ask/spread summary."""
    side = side.lower()
    label = side.upper()
    bid = quote.get(f"{side}_bid_cents")
    ask = quote.get(f"{side}_ask_cents")
    bid_qty = quote_side_qty(quote, side, "bid")
    ask_qty = quote_side_qty(quote, side, "ask")
    spread = quote_side_spread_cents(quote, side)
    wide_threshold = int(getattr(args, "display_wide_spread_cents", DEFAULT_DISPLAY_WIDE_SPREAD_CENTS))

    label_style = "green" if side == "yes" else "red"
    label_text = color_text(label, label_style, "bold")
    spread_text = f"spr={format_cents_value(spread)}"
    if spread is not None and spread > wide_threshold:
        spread_text = color_text(spread_text + " WIDE", "red", "bold")

    return (
        f"{label_text} "
        f"bid={format_book_price(bid, bid_qty)} "
        f"ask={format_book_price(ask, ask_qty)} "
        f"{spread_text}"
    )


def quote_dual_book_summary(quote: dict, *, args=None) -> str:
    """Show YES and NO books separately, with wide spreads highlighted."""
    if not quote:
        return ""
    return (
        quote_side_book_summary(quote, "yes", args=args)
        + " | "
        + quote_side_book_summary(quote, "no", args=args)
    )


def quote_top_of_book_summary(quote: dict, *, args=None) -> str:
    """Show YES and NO bid/ask, size, spread, and midpoint for alerts."""
    parts = [quote_dual_book_summary(quote, args=args)]
    mid = quote.get("mid_yes_cents")
    if isinstance(mid, (int, float)):
        parts.append(f"midYES≈{format_cents_value(mid)}")
    return " | ".join(part for part in parts if part)


def quote_spread_cents(quote: dict) -> int | None:
    """Return integer YES bid/ask spread if available."""
    spread = quote_side_spread_cents(quote, "yes")
    if spread is not None:
        return int(round(spread))
    spread = quote.get("spread_cents")
    if isinstance(spread, (int, float)):
        return int(round(spread))
    return None


def should_treat_spike_as_wide_spread(args, quote: dict) -> tuple[bool, int | None, int]:
    """Return whether a spike alert is based on a too-wide YES or NO book."""
    max_spread = int(getattr(args, "spike_max_spread_cents", DEFAULT_SPIKE_MAX_SPREAD_CENTS))
    spreads = [
        quote_side_spread_cents(quote, "yes"),
        quote_side_spread_cents(quote, "no"),
    ]
    present = [int(round(s)) for s in spreads if s is not None]
    widest = max(present) if present else None
    return widest is not None and widest > max_spread, widest, max_spread


def should_queue_end_no_order(args, market: dict) -> tuple[bool, str, dict, int | None]:
    """
    Decide whether END is allowed to queue BUY NO for this market.

    Returns:
      (should_queue, reason, quote, yes_guard_cents)

    Default policy:
      - if quote is unavailable, skip rather than spray a blind NO order
      - if YES >= --auto-finish-yes-at, mark/skip as finished
      - if YES >= --end-no-skip-yes-at, skip as too dangerous for END NO
      - if NO ask is below --end-no-min-no-ask, skip because the remaining
        NO is too cheap/too late to chase
      - otherwise this is a remaining NO candidate
    """
    quote = latest_quote_for_market(args, market)
    yes_guard = quote_yes_guard_cents(quote)
    no_ask = quote_side_ask_cents(quote, "no")

    if yes_guard is None:
        if args.end_no_require_yes_quote:
            return False, "no_yes_quote", quote, None

        # If the user explicitly disabled the YES-quote requirement, still do
        # the NO ask floor below so END cannot buy 1c/2c/3c garbage NOs.

    if no_ask is None and args.end_no_min_no_ask_cents is not None:
        return False, "no_no_ask_quote", quote, yes_guard

    if (
        args.end_no_min_no_ask_cents is not None
        and no_ask is not None
        and no_ask < args.end_no_min_no_ask_cents
    ):
        return False, "no_ask_too_low_for_end_no", quote, yes_guard

    if (
        yes_guard is not None
        and args.auto_finish_yes_at_cents is not None
        and yes_guard >= args.auto_finish_yes_at_cents
    ):
        return False, "finished_yes_high", quote, yes_guard

    if (
        yes_guard is not None
        and args.end_no_skip_yes_at_cents is not None
        and yes_guard >= args.end_no_skip_yes_at_cents
    ):
        return False, "yes_too_high_for_end_no", quote, yes_guard

    if yes_guard is None:
        return True, "ok_no_yes_quote", quote, None

    return True, "ok", quote, yes_guard


def end_no_queue_order_heading(order_mode: str) -> str:
    """Human-readable description of END NO queue priority."""
    if order_mode == "no-ask-low":
        return "NO ask low->high"
    if order_mode == "no-ask-high":
        return "NO ask high->low"
    if order_mode == "yes-low":
        return "YES low->high"
    if order_mode == "alpha":
        return "alpha"
    return "market order"


def end_no_queue_sort_key(candidate: dict, order_mode: str) -> tuple:
    """Sort END NO candidates before queueing orders.

    Default is lowest NO ask first. For BUY NO, lower ask means cheaper entry
    and therefore bigger upside if the contract resolves to 100c.
    """
    market = candidate["market"]
    label = market_word(market).lower()
    original_index = candidate.get("index", 0)
    quote = candidate.get("quote") or {}
    no_ask = quote_side_ask_cents(quote, "no")
    yes_guard = candidate.get("yes_guard")

    def price_or_last(value, missing_value=101.0) -> float:
        if value is None:
            return float(missing_value)
        try:
            return float(value)
        except Exception:
            return float(missing_value)

    if order_mode == "no-ask-low":
        return (price_or_last(no_ask), label, original_index)
    if order_mode == "no-ask-high":
        return (-price_or_last(no_ask, missing_value=-1.0), label, original_index)
    if order_mode == "yes-low":
        return (price_or_last(yes_guard), label, original_index)
    if order_mode == "alpha":
        return (label, original_index)

    # Old behavior: preserve the loaded market order.
    return (original_index,)


def quote_side_ask_cents(quote: dict, side: str) -> int | None:
    """Return YES/NO ask from a quote context."""
    return quote.get(f"{side.lower()}_ask_cents")


def quote_side_bid_cents(quote: dict, side: str) -> int | None:
    """Return YES/NO bid from a quote context."""
    return quote.get(f"{side.lower()}_bid_cents")


def load_current_markets(client: KalshiClient | None, args) -> list[dict]:
    """Load markets from --file or from Kalshi, then apply optional subset filters."""
    if args.file:
        markets = load_markets_from_file(args.file)
    else:
        if client is None:
            raise RuntimeError("Cannot fetch markets without Kalshi client")
        markets = fetch_active_markets(client, args.ticker, args=args)

    return filter_markets_by_ticker_prefix(markets, args)


# -----------------------------------------------------------------------------
# Order payload and submit helpers
# -----------------------------------------------------------------------------


def make_order_payload(
    *,
    ticker: str,
    side: str,
    count: int,
    order_type: str,
    slippage_cents: int,
    ask_cents: int | None,
    time_in_force: str,
    limit_base_cents: int | None = None,
) -> dict:
    """
    Build the legacy /portfolio/orders payload shape.

    Limit mode bids base + slippage, where base defaults to ask. Hard-bid mode
    passes limit_base_cents explicitly and slippage 0.
    """
    payload = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": count,
        "type": order_type,
        "client_order_id": str(uuid.uuid4()),
    }

    base_cents = ask_cents if limit_base_cents is None else limit_base_cents

    if order_type == "limit":
        if base_cents is None:
            raise ValueError("Cannot place limit order without a base price")

        price = clamp_price_cents(base_cents + slippage_cents)

        if side == "yes":
            payload["yes_price"] = price
        else:
            payload["no_price"] = price

        payload["time_in_force"] = time_in_force

    elif order_type == "market":
        if ask_cents is not None:
            payload["buy_max_cost"] = clamp_price_cents(ask_cents + slippage_cents) * count
    else:
        raise ValueError(f"Unsupported order type: {order_type}")

    return payload


def order_payload_limit_price(payload: dict, side: str) -> int | None:
    """Return the YES/NO limit price from a legacy payload when present."""
    if side == "yes":
        return payload.get("yes_price")
    if side == "no":
        return payload.get("no_price")
    return None



def make_sell_order_payload(
    *,
    ticker: str,
    side: str,
    count: int | Decimal,
    order_type: str,
    slippage_cents: int,
    bid_cents: int | None,
    time_in_force: str,
) -> dict:
    """Build a legacy-shaped SELL payload priced from the selected side's bid.

    A SELL must cross the current bid, so slippage moves the limit *down* from
    the bid.  The V2 conversion below represents both YES and NO exits in the
    single YES-side book and marks the resulting order ``reduce_only``.
    """
    if order_type != "limit":
        raise ValueError("SELL position trade controls require --order-type limit")
    if bid_cents is None:
        raise ValueError("Cannot SELL without a current bid")

    side = str(side or "").lower()
    if side not in {"yes", "no"}:
        raise ValueError(f"Unsupported sell side: {side!r}")

    price = clamp_price_cents(int(bid_cents) - int(slippage_cents))
    payload = {
        "ticker": ticker,
        "action": "sell",
        "side": side,
        "count": count,
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
        "time_in_force": time_in_force,
    }
    if side == "yes":
        payload["yes_price"] = price
    else:
        payload["no_price"] = price
    return payload


def parse_positive_int_csv(value: str, *, name: str, allow_zero: bool = False) -> list[int]:
    """Parse comma-separated integer flags such as 50,30,20."""
    items: list[int] = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must contain only integers, got {raw!r}") from exc
        if allow_zero:
            if n < 0:
                raise ValueError(f"{name} values must be >= 0")
        elif n <= 0:
            raise ValueError(f"{name} values must be > 0")
        items.append(n)
    if not items:
        raise ValueError(f"{name} must contain at least one integer")
    return items


def allocate_ladder_counts(total_count: int, weights: list[int]) -> list[int]:
    """Allocate target contracts across positive slice weights without exceeding total_count."""
    total_count = int(total_count)
    if total_count <= 0:
        return []
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("ladder slice weights must sum to a positive number")

    raw: list[tuple[int, Decimal, Decimal]] = []
    allocated = 0
    for idx, weight in enumerate(weights):
        exact = Decimal(total_count) * Decimal(weight) / Decimal(total_weight)
        floor_count = int(exact)
        frac = exact - Decimal(floor_count)
        raw.append((idx, Decimal(floor_count), frac))
        allocated += floor_count

    counts = [int(floor_count) for _, floor_count, _ in raw]
    remainder = total_count - allocated
    # Give leftover contracts to the largest fractional slices first, preserving
    # earlier slices on ties so a 50,30,20 ladder stays front-loaded.
    for idx, _, _ in sorted(raw, key=lambda item: (-item[2], item[0]))[:remainder]:
        counts[idx] += 1
    return counts


def decimal_count_value(value: Any) -> Decimal | None:
    """Best-effort parser for V2 contract counts such as '1.00'."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def first_decimal_field(mapping: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key in mapping:
            parsed = decimal_count_value(mapping.get(key))
            if parsed is not None:
                return parsed
    return None


def find_first_mapping_with_any_key(value: Any, keys: tuple[str, ...], depth: int = 0) -> dict | None:
    """Search a nested JSON-ish object for a dict containing one of keys."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        if any(key in value for key in keys):
            return value
        for child in value.values():
            found = find_first_mapping_with_any_key(child, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_first_mapping_with_any_key(child, keys, depth + 1)
            if found is not None:
                return found
    return None


def extract_filled_count_from_submit_info(submit_info: dict, requested_count: int) -> tuple[int | None, str]:
    """Return (filled_count, source) from a V2/legacy submit response when knowable.

    If the API response shape changes or omits fill information, callers should
    stop the ladder rather than submit more slices and risk exceeding target size.
    """
    response = submit_info.get("response") if isinstance(submit_info, dict) else None
    keys = (
        "fill_count",
        "filled_count",
        "filled_size",
        "filled_quantity",
        "matched_count",
        "executed_count",
        "executed_quantity",
    )
    order = find_first_mapping_with_any_key(response, keys + ("remaining_count", "remaining_quantity", "count"))
    if not isinstance(order, dict):
        return None, "fill_count_unavailable"

    direct = first_decimal_field(order, keys)
    if direct is not None:
        return max(0, min(requested_count, int(direct))), "response_fill_count"

    remaining = first_decimal_field(order, ("remaining_count", "remaining_quantity", "remaining_size"))
    initial = first_decimal_field(order, ("initial_count", "original_count", "count", "quantity"))
    if remaining is not None:
        if initial is None:
            initial = Decimal(requested_count)
        filled = initial - remaining
        return max(0, min(requested_count, int(filled))), "response_remaining_count"

    return None, "fill_count_unavailable"


def effective_count_for_side(args, side: str | None) -> int:
    """Return the order size for this side, honoring --count-yes/--count-no overrides."""
    side_key = str(side or "").lower()
    if side_key == "yes" and getattr(args, "count_yes", None) is not None:
        return int(args.count_yes)
    if side_key == "no" and getattr(args, "count_no", None) is not None:
        return int(args.count_no)
    return int(args.count)


def effective_slippage_for_side(args, side: str | None) -> int:
    """Return simple-mode slippage for this side, honoring side-specific overrides."""
    side_key = str(side or "").lower()
    if side_key == "yes" and getattr(args, "slippage_yes_cents", None) is not None:
        return int(args.slippage_yes_cents)
    if side_key == "no" and getattr(args, "slippage_no_cents", None) is not None:
        return int(args.slippage_no_cents)
    return int(args.slippage_cents)


def slippage_summary_for_args(args) -> str:
    """Human-readable simple-mode slippage summary."""
    if getattr(args, "hard_bid_cents", None) is not None:
        return f"hard-bid={int(args.hard_bid_cents)}c (slippage ignored)"
    yes_slippage = effective_slippage_for_side(args, "yes")
    no_slippage = effective_slippage_for_side(args, "no")
    if yes_slippage == no_slippage == int(args.slippage_cents):
        return f"ask+{int(args.slippage_cents)}c"
    return f"fallback=+{int(args.slippage_cents)}c, YES=+{yes_slippage}c, NO=+{no_slippage}c"


def count_summary_for_args(args) -> str:
    """Human-readable order size summary."""
    yes_count = effective_count_for_side(args, "yes")
    no_count = effective_count_for_side(args, "no")
    if yes_count == no_count == int(args.count):
        return f"count={int(args.count)}"
    return f"default={int(args.count)}, YES={yes_count}, NO={no_count}"


def ladder_plan_for_args(args, side: str | None = None, target_count: int | None = None) -> list[tuple[int, int]]:
    """Return [(slice_count, price_step_cents), ...] for the current target count/side."""
    count_target = int(target_count if target_count is not None else effective_count_for_side(args, side))
    counts = allocate_ladder_counts(count_target, args.ladder_slice_weights)
    return [
        (count, step)
        for count, step in zip(counts, args.ladder_price_steps_cents)
        if count > 0
    ]


def fixed_contract_count(count: int | float | str) -> str:
    """Return V2 fixed-point contract quantity, e.g. 1 -> '1.00'."""
    return f"{Decimal(str(count)).quantize(Decimal('0.01'))}"


def fixed_dollar_price_from_cents(cents: int | float | str) -> str:
    """Return V2 fixed-point dollar price, e.g. 56 -> '0.5600'."""
    value = Decimal(str(cents)) / Decimal("100")
    return f"{value.quantize(Decimal('0.0001'))}"


def make_event_order_v2_payload(legacy_payload: dict) -> dict:
    """
    Convert our BUY YES/BUY NO legacy payload into Kalshi's V2 event-order shape.

    V2 is a single YES-side book:
      * BUY YES  -> side='bid', price=yes_price
      * BUY NO   -> side='ask', price=1 - no_price  (sell YES at that price)

    Example: buying NO at 1c becomes asking/selling YES at 99c.
    """
    order_type = legacy_payload.get("type")
    if order_type != "limit":
        raise ValueError(
            "V2 event-order submit currently supports limit orders only; "
            "use --order-submit-api legacy for market orders."
        )

    action = str(legacy_payload.get("action", "")).lower()
    old_side = str(legacy_payload.get("side", "")).lower()
    if action not in {"buy", "sell"} or old_side not in {"yes", "no"}:
        raise ValueError(f"Unsupported legacy order shape for V2 conversion: action={action!r} side={old_side!r}")

    # V2 quotes a single YES-side book.  BUY/SELL NO are the economically
    # equivalent opposite operations in that book.  SELLs are reduce_only so
    # an intended close cannot reverse into a new position.
    if action == "buy" and old_side == "yes":
        old_price = legacy_payload.get("yes_price")
        if old_price is None:
            raise ValueError("BUY YES limit order missing yes_price")
        v2_side = "bid"
        v2_price_cents = int(old_price)
    elif action == "buy" and old_side == "no":
        old_price = legacy_payload.get("no_price")
        if old_price is None:
            raise ValueError("BUY NO limit order missing no_price")
        v2_side = "ask"
        v2_price_cents = 100 - int(old_price)
    elif action == "sell" and old_side == "yes":
        old_price = legacy_payload.get("yes_price")
        if old_price is None:
            raise ValueError("SELL YES limit order missing yes_price")
        v2_side = "ask"
        v2_price_cents = int(old_price)
    else:  # SELL NO: buy YES at the complementary price to close NO exposure.
        old_price = legacy_payload.get("no_price")
        if old_price is None:
            raise ValueError("SELL NO limit order missing no_price")
        v2_side = "bid"
        v2_price_cents = 100 - int(old_price)

    # Kalshi prices are bounded away from 0/100 for event contracts. Our legacy
    # payloads are already clamped to 1..97, but clamp again after NO conversion.
    v2_price_cents = max(1, min(99, int(v2_price_cents)))

    tif = legacy_payload.get("time_in_force") or "immediate_or_cancel"
    if tif == "GTT":
        tif = "good_till_canceled"

    return {
        "ticker": legacy_payload["ticker"],
        "client_order_id": legacy_payload.get("client_order_id") or str(uuid.uuid4()),
        "side": v2_side,
        "count": fixed_contract_count(legacy_payload["count"]),
        "price": fixed_dollar_price_from_cents(v2_price_cents),
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": action == "sell",
    }


def signed_json_request(
    args,
    *,
    method: str,
    path: str,
    body: dict | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict:
    """Make a raw signed Kalshi REST request and return normalized response info.

    Kalshi signs the URL path without query parameters, while query parameters
    are appended only to the request URL.
    """
    if not getattr(args, "_auth_api_key_id", None) or not getattr(args, "_auth_private_key_file", None):
        raise RuntimeError("Resolved auth settings are required before signed REST requests")

    signed_path = str(path).split("?", 1)[0]
    url = args.api_host.rstrip("/") + signed_path.removeprefix("/trade-api/v2")
    query = urlparse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    if query:
        url += "?" + query
    timestamp = str(int(time.time() * 1000))
    private_key = getattr(args, "_raw_rest_private_key", None)
    if private_key is None:
        private_key = load_pem_private_key_for_auth_check(args._auth_private_key_file)
        args._raw_rest_private_key = private_key
    signature = sign_private_key_text(private_key, timestamp + method.upper() + signed_path)
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "KALSHI-ACCESS-KEY": args._auth_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urlrequest.Request(url, data=data, headers=headers, method=method.upper())

    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                response_body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                response_body = {"raw": raw}
            return {
                "method": f"raw.{method.upper()} {path}",
                "http_status": getattr(resp, "status", None) or getattr(resp, "code", None),
                "headers": headers_to_dict(getattr(resp, "headers", None)),
                "response": response_body,
            }
    except urlerror.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        try:
            exc.body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            exc.body = raw
        raise



def fetch_positions_for_loaded_markets(args, markets: list[dict]) -> dict[str, Decimal]:
    """Fetch net YES positions for the loaded markets from Kalshi.

    Positive ``position_fp`` means a YES position; negative means a NO
    position.  We use the exact event filter whenever the command supplied a
    full event name, otherwise fetch once and retain only currently loaded
    tickers.
    """
    known = {str(m.get("ticker") or "").upper() for m in markets if m.get("ticker")}
    if not known:
        return {}

    event_ticker = event_ticker_from_args(getattr(args, "ticker", ""), args)
    params: dict[str, Any] = {"limit": 1000, "count_filter": "position"}
    if event_ticker:
        params["event_ticker"] = event_ticker

    info = call_api_with_limits(
        args,
        kind="read",
        label=f"get_positions:{event_ticker or 'loaded_markets'}",
        meta={"event_ticker": event_ticker, "markets": len(known)},
        func=lambda: signed_json_request(
            args,
            method="GET",
            path="/trade-api/v2/portfolio/positions",
            params=params,
            timeout=float(getattr(args, "order_submit_timeout", 10.0)),
        ),
    )
    response = info.get("response") if isinstance(info, dict) else None
    rows = response.get("market_positions") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected /portfolio/positions response: {response!r}")

    result: dict[str, Decimal] = {ticker: Decimal("0") for ticker in known}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if ticker not in known:
            continue
        position = decimal_count_value(row.get("position_fp"))
        if position is None:
            position = decimal_count_value(row.get("position"))
        if position is not None:
            result[ticker] = position
    return result


def fetch_position_for_market(args, market: dict) -> Decimal:
    """Fetch one market's authoritative signed YES/NO position."""
    ticker = str(market.get("ticker") or "").upper()
    if not ticker:
        raise RuntimeError("Cannot fetch a position without a market ticker")
    positions = fetch_positions_for_loaded_markets(args, [market])
    return positions.get(ticker, Decimal("0"))


def format_net_position(position: Decimal | int | float | None) -> str:
    """Display net event position as YES, NO, or flat."""
    parsed = decimal_count_value(position)
    if parsed is None or parsed == 0:
        return "flat"
    qty = abs(parsed)
    qty_text = f"{qty.normalize():f}" if qty != qty.to_integral() else str(int(qty))
    return f"YES {qty_text}" if parsed > 0 else f"NO {qty_text}"


def submit_order_v2_events(args, payload: dict) -> dict:
    """Submit one order via POST /portfolio/events/orders using V2 shape."""
    v2_payload = make_event_order_v2_payload(payload)
    info = signed_json_request(
        args,
        method="POST",
        path="/trade-api/v2/portfolio/events/orders",
        body=v2_payload,
        timeout=float(getattr(args, "order_submit_timeout", 10.0)),
    )
    info["call_style"] = "raw_signed_json"
    info["submit_api"] = "v2_events"
    info["v2_payload"] = v2_payload
    return info


def headers_to_dict(headers: Any) -> Any:
    """Normalize HTTP-ish headers."""
    if headers is None:
        return None

    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}

    try:
        return {str(k): str(v) for k, v in dict(headers).items()}
    except Exception:
        return str(headers)


def response_status_code(obj: Any) -> int | None:
    """Best-effort extraction of an HTTP-ish status/return code."""
    if obj is None:
        return None

    for attr in ("status_code", "status", "code"):
        value = getattr(obj, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    response = getattr(obj, "response", None)
    if response is not None and response is not obj:
        value = response_status_code(response)
        if value is not None:
            return value

    return None


def normalize_submit_response(raw: Any, method_name: str) -> dict:
    """
    Normalize SDK return shapes.

    OpenAPI-style *_with_http_info methods commonly return:
      (data, status_code, headers)
    """
    data = raw
    status_code = None
    headers = None

    if isinstance(raw, tuple):
        if len(raw) >= 3:
            data, status_code, headers = raw[0], raw[1], raw[2]
        elif len(raw) == 2:
            data, status_code = raw[0], raw[1]
        elif len(raw) == 1:
            data = raw[0]
    else:
        status_code = response_status_code(raw)
        if hasattr(raw, "headers"):
            headers = getattr(raw, "headers")
        if hasattr(raw, "data"):
            data = getattr(raw, "data")

    if isinstance(status_code, str) and status_code.isdigit():
        status_code = int(status_code)

    return {
        "method": method_name,
        "http_status": status_code,
        "headers": headers_to_dict(headers),
        "response": model_to_dict(data),
    }


def is_argument_shape_error(exc: Exception) -> bool:
    """True only for local SDK/signature failures where no order was sent."""
    if isinstance(exc, TypeError):
        return True

    msg = str(exc)
    return (
        "Missing required argument" in msg
        or "Unexpected keyword argument" in msg
        or ("create_order_request" in msg and "validation errors" in msg)
    )


def call_order_method(method: Any, method_name: str, payload: dict) -> dict:
    """
    Call Kalshi create_order across SDK versions without double-submitting.

    Retries are only for local Python call-shape errors. Real API/order failures
    are not retried here because that could duplicate a live order.
    """
    attempts = [
        ("create_order_request_kw", lambda: method(create_order_request=payload)),
        ("positional_request", lambda: method(payload)),
        ("expanded_kwargs", lambda: method(**payload)),
    ]

    last_shape_error: Exception | None = None

    for call_style, call in attempts:
        try:
            raw = call()
            info = normalize_submit_response(raw, method_name)
            info["call_style"] = call_style
            return info
        except Exception as exc:
            if is_argument_shape_error(exc):
                last_shape_error = exc
                continue
            raise

    if last_shape_error is not None:
        raise last_shape_error

    raise RuntimeError(f"Could not call {method_name}")


def submit_order(client: KalshiClient, payload: dict) -> dict:
    """Submit an order and return normalized method/status/response info."""
    if hasattr(client, "create_order_with_http_info"):
        return call_order_method(
            client.create_order_with_http_info,
            "client.create_order_with_http_info",
            payload,
        )

    if hasattr(client, "create_order"):
        return call_order_method(client.create_order, "client.create_order", payload)

    if hasattr(client, "orders"):
        orders = client.orders

        if hasattr(orders, "create_order_with_http_info"):
            return call_order_method(
                orders.create_order_with_http_info,
                "client.orders.create_order_with_http_info",
                payload,
            )

        if hasattr(orders, "create_order"):
            return call_order_method(
                orders.create_order,
                "client.orders.create_order",
                payload,
            )

    raise RuntimeError(
        "Could not find a supported order method on KalshiClient. "
        "Inspect dir(client) and update submit_order()."
    )


def submit_order_selected(args, client: KalshiClient, payload: dict) -> dict:
    """Submit using the selected order submit API."""
    submit_api = getattr(args, "order_submit_api", "v2")
    if submit_api == "v2":
        return submit_order_v2_events(args, payload)
    if submit_api == "legacy":
        info = submit_order(client, payload)
        if isinstance(info, dict):
            info["submit_api"] = "legacy_portfolio_orders"
        return info
    raise ValueError(f"Unsupported --order-submit-api: {submit_api}")


def pretty_json(value: Any) -> str:
    """Stable readable JSON for log blocks."""
    return json.dumps(value, default=str, indent=2, sort_keys=True)


def error_details(exc: Exception) -> dict:
    """Capture useful structured details from SDK/API exceptions."""
    details = {
        "type": type(exc).__name__,
        "message": str(exc),
        "http_status": response_status_code(exc),
    }

    for attr in ("body", "reason", "status", "status_code", "headers"):
        if hasattr(exc, attr):
            details[attr] = model_to_dict(getattr(exc, attr))

    response = getattr(exc, "response", None)
    if response is not None:
        details["response_status"] = response_status_code(response)
        if hasattr(response, "text"):
            details["response_text"] = getattr(response, "text")
        elif hasattr(response, "data"):
            details["response_data"] = model_to_dict(getattr(response, "data"))
        if hasattr(response, "headers"):
            details["response_headers"] = headers_to_dict(getattr(response, "headers"))

    return details


# -----------------------------------------------------------------------------
# Logging, transcript capture, and order execution
# -----------------------------------------------------------------------------


class TranscriptRecorder:
    """
    Save what you type while listening, plus structured events.

    The .txt file is for human review.
    The .jsonl file is for later analysis/replay with scripts.
    """

    def __init__(self, transcript_file: str | None):
        self.lock = threading.RLock()
        self.transcript_file = transcript_file
        self.jsonl_file: str | None = None
        self._txt_f = None
        self._jsonl_f = None

        if not transcript_file:
            return

        path = Path(transcript_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        self.transcript_file = str(path)
        self.jsonl_file = str(path.with_suffix(path.suffix + ".jsonl"))

        self._txt_f = open(self.transcript_file, "a", encoding="utf-8")
        self._jsonl_f = open(self.jsonl_file, "a", encoding="utf-8")

    def enabled(self) -> bool:
        """Return True when transcript output files are open."""
        return self._txt_f is not None and self._jsonl_f is not None

    def write_header(self, args, markets: list[dict]) -> None:
        """Write a human-readable session header and a JSONL session_start."""
        if not self.enabled():
            return

        self.record_event(
            "session_start",
            {
                "ticker": getattr(args, "ticker", None),
                "kalshi_env": getattr(args, "kalshi_env", None),
                "api_host": getattr(args, "api_host", None),
                "ws_url": getattr(args, "ws_url", None),
                "market_event": getattr(args, "market_event", None),
                "market_prefix": getattr(args, "market_prefix", None),
                "live": getattr(args, "live", False),
                "count": getattr(args, "count", None),
                "mention_side": getattr(args, "mention_side", None),
                "n_markets": len(markets),
            },
        )

        with self.lock:
            self._txt_f.write("\n" + "=" * 88 + "\n")
            self._txt_f.write(f"SESSION START {utc_now()}\n")
            self._txt_f.write(
                f"ticker={getattr(args, 'ticker', None)} "
                f"env={getattr(args, 'kalshi_env', None)} "
                f"market_event={getattr(args, 'market_event', None)} "
                f"live={getattr(args, 'live', False)}\n"
            )
            self._txt_f.write("=" * 88 + "\n\n")
            self._txt_f.flush()

    def record_char(self, ch: str) -> None:
        """
        Record one raw typed character.

        Backspace is represented visibly rather than rewriting history. That is
        better for after-action analysis because mistakes and corrections remain
        visible.
        """
        if not self.enabled():
            return

        if ch in ("\x7f", "\b"):
            text = "<BACKSPACE>"
        elif ch in ("\r", "\n"):
            text = "\n"
        elif ch == "\t":
            text = "\t"
        elif ch.isprintable():
            text = ch
        else:
            text = f"<CTRL-{ord(ch):02x}>"

        with self.lock:
            self._txt_f.write(text)
            self._txt_f.flush()

        self.record_event("char", {"ch": text})

    def record_event(self, event_type: str, data: dict | None = None) -> None:
        """Append one structured JSONL event."""
        if not self.enabled():
            return

        event = {
            "ts": utc_now(),
            "type": event_type,
            "data": data or {},
        }

        with self.lock:
            self._jsonl_f.write(json.dumps(event, default=str, sort_keys=True) + "\n")
            self._jsonl_f.flush()

    def close(self) -> None:
        """Flush and close transcript files."""
        if not self.enabled():
            return

        self.record_event("session_end", {})

        with self.lock:
            self._txt_f.write(f"\n\nSESSION END {utc_now()}\n")
            self._txt_f.flush()
            self._jsonl_f.flush()
            self._txt_f.close()
            self._jsonl_f.close()
            self._txt_f = None
            self._jsonl_f = None


def transcript_recorder(args) -> TranscriptRecorder | None:
    """Return the active transcript recorder, if one exists."""
    return getattr(args, "_transcript_recorder", None)


def record_transcript_event(args, event_type: str, data: dict | None = None) -> None:
    """Best-effort helper used by worker functions."""
    recorder = transcript_recorder(args)
    if recorder is not None:
        recorder.record_event(event_type, data or {})



def render_order_log_block(event: dict) -> str:
    """Render the readable multi-line order log block without touching disk."""
    event = dict(event)
    event["ts"] = utc_now()

    title_bits = [
        event.get("ts"),
        str(event.get("mode", "")),
        str(event.get("action", "")),
        str(event.get("result", "")),
    ]
    title = " | ".join(bit for bit in title_bits if bit)

    summary_keys = [
        "env",
        "trigger",
        "typed_tail",
        "trade_action",
        "word",
        "ticker",
        "side",
        "count",
        "order_type",
        "time_in_force",
        "price_base",
        "price_source",
        "base_price_cents",
        "limit_price_cents",
        "hard_bid_cents",
        "slippage_cents",
        "bid_cents",
        "ask_cents",
        "yes_bid_cents",
        "yes_ask_cents",
        "no_bid_cents",
        "no_bid_qty",
        "no_ask_cents",
        "no_ask_qty",
        "spread_cents",
        "ws_seq",
        "ws_age_ms",
        "ws_last_exchange_ts_ms",
        "ws_snapshot_refresh",
        "ws_snapshot_refresh_wait_ms",
        "order_submit_api",
        "execution_mode",
        "ladder_slice",
        "ladder_total_slices",
        "ladder_target_count",
        "ladder_remaining_before",
        "ladder_price_step_cents",
        "submit_method",
        "submit_call_style",
        "http_status",
    ]

    lines: list[str] = []
    lines.append("\n" + "=" * 88 + "\n")
    lines.append(f"{title}\n")
    lines.append("-" * 88 + "\n")

    for key in summary_keys:
        if key in event and event[key] is not None:
            lines.append(f"{key:>20}: {event[key]}\n")

    if "payload" in event and event["payload"] is not None:
        lines.append("\npayload:\n")
        lines.append(pretty_json(event["payload"]) + "\n")

    if "v2_payload" in event and event["v2_payload"] is not None:
        lines.append("\nv2_payload:\n")
        lines.append(pretty_json(event["v2_payload"]) + "\n")

    if "response" in event and event["response"] is not None:
        lines.append("\nresponse:\n")
        lines.append(pretty_json(event["response"]) + "\n")

    if "headers" in event and event["headers"] is not None:
        lines.append("\nheaders:\n")
        lines.append(pretty_json(event["headers"]) + "\n")

    if "error" in event and event["error"] is not None:
        lines.append("\nerror:\n")
        if isinstance(event["error"], dict):
            lines.append(pretty_json(event["error"]) + "\n")
        else:
            lines.append(str(event["error"]) + "\n")

    lines.append("=" * 88 + "\n")
    return "".join(lines)


def log_order_event(target, event: dict) -> None:
    """
    Record a readable multi-line order log block.

    Pass args to enable hot-path buffered logging. Passing a string path keeps the
    old immediate-write behavior for compatibility.
    """
    args = target if hasattr(target, "log_file") else None
    log_file = getattr(args, "log_file", None) if args is not None else target
    if not log_file:
        return

    block = render_order_log_block(event)
    buf = deferred_log_buffer(args) if args is not None and getattr(args, "defer_log_writes", False) else None
    if buf is not None:
        buf.append(log_file, block)
        return

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(block)

def place_single_order_for_market(
    *,
    client: KalshiClient | None,
    args,
    market: dict,
    side: str,
    action: str,
    trigger: str,
    typed_tail: str | None = None,
    trade_action: str = "buy",
    count_override: int | Decimal | None = None,
    slippage_override_cents: int | None = None,
    time_in_force_override: str | None = None,
    ladder_slice_index: int | None = None,
    ladder_total_slices: int | None = None,
    ladder_target_count: int | None = None,
    ladder_remaining_before: int | None = None,
) -> tuple[bool, str, dict]:
    """
    Build, log, and optionally submit one BUY order or one ladder slice.

    In dry-run mode, ok=True means the would-be order was logged.
    In live mode, ok=True means the submit call did not raise.
    """
    ticker = market["ticker"]
    word = market_word(market)
    trade_action = str(trade_action or "buy").lower()
    if trade_action not in {"buy", "sell"}:
        raise ValueError(f"Unsupported trade action: {trade_action!r}")
    order_count = effective_count_for_side(args, side) if count_override is None else count_override
    order_count_decimal = decimal_count_value(order_count)
    if order_count_decimal is None or order_count_decimal <= 0:
        raise ValueError(f"Order count must be positive, got {order_count!r}")
    order_count = int(order_count_decimal) if order_count_decimal == order_count_decimal.to_integral() else order_count_decimal
    order_time_in_force = time_in_force_override or args.time_in_force
    ladder_enabled = ladder_slice_index is not None
    ladder_prefix = (
        f" ladder_slice={ladder_slice_index}/{ladder_total_slices}"
        if ladder_enabled
        else ""
    )

    try:
        quote = latest_quote_for_order(args, market, side)
        ask_cents = quote_side_ask_cents(quote, side)
        bid_cents = quote_side_bid_cents(quote, side)

        if trade_action == "buy":
            if args.hard_bid_cents is not None:
                price_base = "hard_bid"
                effective_slippage_cents = 0
                limit_base_cents = args.hard_bid_cents
            else:
                price_base = "ask"
                effective_slippage_cents = (
                    effective_slippage_for_side(args, side)
                    if slippage_override_cents is None
                    else slippage_override_cents
                )
                limit_base_cents = ask_cents

            payload = make_order_payload(
                ticker=ticker,
                side=side,
                count=order_count,
                order_type=args.order_type,
                slippage_cents=effective_slippage_cents,
                ask_cents=ask_cents,
                time_in_force=order_time_in_force,
                limit_base_cents=limit_base_cents,
            )
        else:
            if args.hard_bid_cents is not None:
                raise ValueError("--hard-bid cannot be used with SELL-position trade controls")
            price_base = "bid"
            effective_slippage_cents = (
                effective_slippage_for_side(args, side)
                if slippage_override_cents is None
                else slippage_override_cents
            )
            limit_base_cents = bid_cents
            payload = make_sell_order_payload(
                ticker=ticker,
                side=side,
                count=order_count,
                order_type=args.order_type,
                slippage_cents=effective_slippage_cents,
                bid_cents=bid_cents,
                time_in_force=order_time_in_force,
            )

        limit_price_cents = order_payload_limit_price(payload, side)
        v2_payload_preview = None
        try:
            if args.order_submit_api == "v2" and args.order_type == "limit":
                v2_payload_preview = make_event_order_v2_payload(payload)
        except Exception:
            v2_payload_preview = None

        base_log = {
            "mode": "live" if args.live else "dry_run",
            "env": args.kalshi_env,
            "action": action,
            "trade_action": trade_action,
            "trigger": trigger,
            "typed_tail": typed_tail,
            "word": word,
            "ticker": ticker,
            "side": side,
            "count": order_count,
            "order_type": args.order_type,
            "time_in_force": order_time_in_force if args.order_type == "limit" else None,
            "price_base": price_base,
            "base_price_cents": limit_base_cents,
            "limit_price_cents": limit_price_cents,
            "hard_bid_cents": args.hard_bid_cents,
            "slippage_cents": effective_slippage_cents,
            "price_source": quote.get("price_source"),
            "bid_cents": bid_cents,
            "ask_cents": ask_cents,
            "yes_bid_cents": quote.get("yes_bid_cents"),
            "yes_ask_cents": quote.get("yes_ask_cents"),
            "no_bid_cents": quote.get("no_bid_cents"),
            "no_bid_qty": quote.get("no_bid_qty"),
            "no_ask_cents": quote.get("no_ask_cents"),
            "no_ask_qty": quote.get("no_ask_qty"),
            "spread_cents": quote.get("spread_cents"),
            "mid_yes_cents": quote.get("mid_yes_cents"),
            "ws_seq": quote.get("ws_seq"),
            "ws_age_ms": quote.get("ws_age_ms"),
            "ws_last_exchange_ts_ms": quote.get("ws_last_exchange_ts_ms"),
            "ws_snapshot_refresh": quote.get("ws_snapshot_refresh"),
            "ws_snapshot_refresh_wait_ms": quote.get("ws_snapshot_refresh_wait_ms"),
            "payload": payload,
            "v2_payload": v2_payload_preview,
            "order_submit_api": args.order_submit_api,
            "execution_mode": getattr(args, "execution_mode", "simple"),
            "ladder_slice": ladder_slice_index,
            "ladder_total_slices": ladder_total_slices,
            "ladder_target_count": ladder_target_count,
            "ladder_remaining_before": ladder_remaining_before,
            "ladder_price_step_cents": slippage_override_cents,
        }

        record_transcript_event(
            args,
            "order_built",
            {
                "mode": "live" if args.live else "dry_run",
                "action": action,
                "trade_action": trade_action,
                "trigger": trigger,
                "typed_tail": typed_tail,
                "word": word,
                "ticker": ticker,
                "side": side,
                "count": order_count,
                "price_source": quote.get("price_source"),
                "bid_cents": bid_cents,
                "ask_cents": ask_cents,
                "limit_price_cents": limit_price_cents,
                "payload": payload,
                "execution_mode": getattr(args, "execution_mode", "simple"),
                "ladder_slice": ladder_slice_index,
                "ladder_total_slices": ladder_total_slices,
                "ladder_target_count": ladder_target_count,
                "ladder_remaining_before": ladder_remaining_before,
                "ladder_price_step_cents": slippage_override_cents,
            },
        )

        if not args.live:
            log_order_event(
                args,
                {
                    **base_log,
                    "result": "dry_run",
                    "response": None,
                    "error": None,
                },
            )
            return True, (
                f"DRY RUN{ladder_prefix} {action}: {word} | {ticker} {trade_action.upper()} {side.upper()} "
                f"count={order_count} limit={cents_to_dollars(limit_price_cents)} "
                f"source={quote.get('price_source')} ask={cents_to_dollars(ask_cents)} api={args.order_submit_api}"
            ), {
                "requested_count": order_count,
                "filled_count": order_count,
                "fill_count_source": "dry_run_assumed",
                "limit_price_cents": limit_price_cents,
                "ask_cents": ask_cents,
            }

        if client is None:
            raise RuntimeError("Live mode needs a Kalshi client")

        try:
            submit_info = call_api_with_limits(
                args,
                kind="write",
                label=f"create_order:{ticker}",
                meta={
                    "ticker": ticker,
                    "word": word,
                    "order_submit_api": args.order_submit_api,
                    "side": side.upper(),
                    "count": order_count,
                    "limit_cents": limit_price_cents,
                    "v2_book_side": (v2_payload_preview or {}).get("side"),
                    "v2_price": (v2_payload_preview or {}).get("price"),
                    "ask_cents": ask_cents,
                    "price_source": quote.get("price_source"),
                    "action": action,
                    "trade_action": trade_action,
                    "trigger": trigger,
                    "client_order_id": payload.get("client_order_id"),
                    "order_type": args.order_type,
                    "time_in_force": order_time_in_force if args.order_type == "limit" else None,
                    "hard_bid_cents": args.hard_bid_cents,
                    "slippage_cents": effective_slippage_cents,
                    "yes_bid_cents": quote.get("yes_bid_cents"),
                    "yes_ask_cents": quote.get("yes_ask_cents"),
                    "no_bid_cents": quote.get("no_bid_cents"),
                    "no_ask_cents": quote.get("no_ask_cents"),
                    "spread_cents": quote.get("spread_cents"),
                    "ws_age_ms": quote.get("ws_age_ms"),
                    "ws_snapshot_refresh": quote.get("ws_snapshot_refresh"),
                    "ws_snapshot_refresh_wait_ms": quote.get("ws_snapshot_refresh_wait_ms"),
                    "execution_mode": getattr(args, "execution_mode", "simple"),
                    "ladder_slice": ladder_slice_index,
                    "ladder_total_slices": ladder_total_slices,
                    "ladder_target_count": ladder_target_count,
                    "ladder_remaining_before": ladder_remaining_before,
                    "ladder_price_step_cents": slippage_override_cents,
                },
                func=lambda: submit_order_selected(args, client, payload),
            )
            log_order_event(
                args,
                {
                    **base_log,
                    "result": "sent",
                    "submit_method": submit_info.get("method"),
                    "submit_call_style": submit_info.get("call_style"),
                    "order_submit_api": submit_info.get("submit_api") or args.order_submit_api,
                    "http_status": submit_info.get("http_status"),
                    "headers": submit_info.get("headers"),
                    "response": submit_info.get("response"),
                    "v2_payload": submit_info.get("v2_payload"),
                    "error": None,
                },
            )

            http_status = submit_info.get("http_status")
            record_transcript_event(
                args,
                "order_sent",
                {
                    "action": action,
                    "trade_action": trade_action,
                    "word": word,
                    "ticker": ticker,
                    "side": side,
                    "limit_price_cents": limit_price_cents,
                    "http_status": http_status,
                    "submit_method": submit_info.get("method"),
                    "submit_call_style": submit_info.get("call_style"),
                    "order_submit_api": submit_info.get("submit_api") or args.order_submit_api,
                    "response": submit_info.get("response"),
                },
            )

            filled_count, fill_count_source = extract_filled_count_from_submit_info(submit_info, order_count)
            http_text = f" HTTP={http_status}" if http_status is not None else ""
            fill_text = "" if filled_count is None else f" filled={filled_count}/{order_count}"
            return True, (
                f"LIVE SENT{http_text}{ladder_prefix} {action}: {word} | {ticker} {trade_action.upper()} {side.upper()} "
                f"count={order_count} limit={cents_to_dollars(limit_price_cents)} "
                f"source={quote.get('price_source')} ask={cents_to_dollars(ask_cents)} "
                f"api={submit_info.get('submit_api') or args.order_submit_api}{fill_text}"
            ), {
                "submit_info": submit_info,
                "requested_count": order_count,
                "filled_count": filled_count,
                "fill_count_source": fill_count_source,
                "limit_price_cents": limit_price_cents,
                "ask_cents": ask_cents,
            }

        except Exception as order_error:
            details = error_details(order_error)
            record_transcript_event(
                args,
                "order_error",
                {
                    "action": action,
                    "trade_action": trade_action,
                    "word": word,
                    "ticker": ticker,
                    "side": side,
                    "limit_price_cents": limit_price_cents,
                    "error": details,
                },
            )
            log_order_event(
                args,
                {
                    **base_log,
                    "result": "error",
                    "http_status": details.get("http_status") or details.get("response_status"),
                    "response": None,
                    "error": details,
                },
            )
            return False, f"ORDER ERROR{ladder_prefix} {action}: {word} | {ticker}: {order_error}", {"error": details, "requested_count": order_count}

    except Exception as build_error:
        record_transcript_event(
            args,
            "order_build_error",
            {
                "action": action,
                "ticker": ticker,
                "word": word,
                "side": side,
                "error": error_details(build_error),
            },
        )
        log_order_event(
            args,
            {
                "mode": "live" if args.live else "dry_run",
                "env": args.kalshi_env,
                "action": f"{action}_build_order_failed",
                "trigger": trigger,
                "typed_tail": typed_tail,
                "ticker": ticker,
                "word": word,
                "side": side,
                "count": order_count,
                "hard_bid_cents": args.hard_bid_cents,
                "error": error_details(build_error),
            },
        )
        return False, f"ORDER BUILD ERROR{ladder_prefix} {action}: {word} | {ticker}: {build_error}", {"error": error_details(build_error), "requested_count": order_count}


def place_ioc_ladder_for_market(
    *,
    client: KalshiClient | None,
    args,
    market: dict,
    side: str,
    action: str,
    trigger: str,
    typed_tail: str | None = None,
) -> tuple[bool, str]:
    """Submit one intended order as sequential IOC slices, stopping at target count."""
    ticker = market["ticker"]
    word = market_word(market)
    target = int(effective_count_for_side(args, side))
    plan = ladder_plan_for_args(args, side=side, target_count=target)
    if not plan:
        return False, f"LADDER ERROR {action}: {word} | {ticker}: empty ladder plan"

    remaining = target
    total_filled = 0
    statuses: list[str] = []
    total_slices = len(plan)
    safe_print(
        f"LADDER PLAN: {word} [{ticker}] BUY {side.upper()} target={target} "
        f"slices=" + ",".join(f"{count}@+{step}c" for count, step in plan)
    )

    for idx, (planned_count, price_step) in enumerate(plan, start=1):
        if remaining <= 0:
            break
        slice_count = min(int(planned_count), remaining)
        if slice_count <= 0:
            continue

        slice_trigger = (
            f"{trigger}; IOC ladder slice {idx}/{total_slices} "
            f"target={target} remaining_before={remaining} step=+{price_step}c"
        )
        ok, status, details = place_single_order_for_market(
            client=client,
            args=args,
            market=market,
            side=side,
            action=f"{action}_ladder",
            trigger=slice_trigger,
            typed_tail=typed_tail,
            count_override=slice_count,
            slippage_override_cents=0 if args.hard_bid_cents is not None else int(price_step),
            time_in_force_override=args.ladder_time_in_force,
            ladder_slice_index=idx,
            ladder_total_slices=total_slices,
            ladder_target_count=target,
            ladder_remaining_before=remaining,
        )
        statuses.append(status)
        if not ok:
            return False, (
                f"LADDER FAIL {action}: {word} | {ticker} "
                f"filled={total_filled}/{target}; failed_slice={idx}/{total_slices}; {status}"
            )

        filled_count = details.get("filled_count")
        fill_source = details.get("fill_count_source")
        if filled_count is None:
            # Safety: do not submit another slice if we cannot prove how much the
            # previous IOC slice filled. This avoids accidentally exceeding target.
            return True, (
                f"LADDER STOP UNKNOWN FILL {action}: {word} | {ticker} "
                f"target={target} submitted_slice={idx}/{total_slices} count={slice_count}; "
                f"fill_source={fill_source}; stopped to avoid overfill"
            )

        filled_count = max(0, min(slice_count, int(filled_count)))
        total_filled += filled_count
        remaining = max(0, target - total_filled)
        safe_print(
            f"LADDER SLICE RESULT: {word} [{ticker}] slice={idx}/{total_slices} "
            f"requested={slice_count} filled={filled_count} total_filled={total_filled}/{target} "
            f"remaining={remaining} source={fill_source}"
        )

    ok = total_filled > 0 or not args.live
    return ok, (
        f"LADDER DONE {action}: {word} | {ticker} BUY {side.upper()} "
        f"filled={total_filled}/{target} slices_attempted={len(statuses)}/{total_slices} "
        f"tif={args.ladder_time_in_force}"
    )


def place_order_for_market(
    *,
    client: KalshiClient | None,
    args,
    market: dict,
    side: str,
    action: str,
    trigger: str,
    typed_tail: str | None = None,
    trade_action: str = "buy",
    count_override: int | Decimal | None = None,
) -> tuple[bool, str, dict]:
    """Dispatch to the selected execution mode and return fill details."""
    # Manual trade-control actions always use one immediate-or-cancel order so
    # their fill count can update the on-screen position immediately.  The
    # global execution mode still applies to automatic paths such as Ctrl-E.
    is_manual_trade_control = str(action).startswith("manual_")
    mode = "simple" if is_manual_trade_control else getattr(args, "execution_mode", "simple")
    if mode == "simple":
        ok, status, details = place_single_order_for_market(
            client=client,
            args=args,
            market=market,
            side=side,
            action=action,
            trigger=trigger,
            typed_tail=typed_tail,
            trade_action=trade_action,
            count_override=count_override,
        )
        return ok, status, details
    if mode == "ioc-ladder":
        ok, status = place_ioc_ladder_for_market(
            client=client,
            args=args,
            market=market,
            side=side,
            action=action,
            trigger=trigger,
            typed_tail=typed_tail,
        )
        return ok, status, {}
    return False, f"ORDER ERROR {action}: unsupported --execution-mode {mode!r}", {}


# -----------------------------------------------------------------------------
# Shared threaded state
# -----------------------------------------------------------------------------


@dataclass
class OrderTask:
    """A queued order generated by input detection, END, or trade controls."""

    action: str
    market_key: str
    market_snapshot: dict
    side: str
    trigger: str
    typed_tail: str | None = None
    trade_action: str = "buy"
    sell_all: bool = False
    suppress_duplicate_guard: bool = False


@dataclass
class AutocompleteSuggestion:
    """One selectable terminal autocomplete candidate."""

    alias: str
    market_key: str
    market_word: str
    ticker: str
    repeated: bool = False
    compact_match: bool = False
    yes_prob_cents: int | None = None
    # In SELL mode this is the net YES position displayed beside the candidate.
    # Positive means YES held; negative means NO held.
    net_position: Decimal = Decimal("0")


class SharedBroadcastState:
    """
    Thread-safe state shared by input, refresh, order, and status threads.

    Core rule:
      - state.lock protects market snapshots, word index, heard sets, counters,
        and status text.
      - network/order calls happen outside state.lock so input stays responsive.
    """

    def __init__(self, markets: list[dict], args):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.ended_event = threading.Event()
        self.refresh_now_event = threading.Event()
        self.order_queue: queue.Queue[OrderTask] = queue.Queue()
        # Number of order-worker tasks currently past queue.get() and still
        # waiting on build/submit/response handling. This lets deferred logging
        # flush only after the whole order burst is truly idle, not merely when
        # the queue has been drained by workers.
        self.active_order_tasks = 0

        self.markets: list[dict] = []
        self.market_by_key: dict[str, dict] = {}
        self.word_to_keys: dict[str, list[str]] = {}
        self.word_keys_by_length: list[str] = []
        self.compact_word_to_keys: dict[str, list[str]] = {}
        self.compact_word_keys_by_length: list[str] = []

        self.repeated_word_to_keys: dict[str, list[str]] = {}
        self.repeated_word_keys_by_length: list[str] = []
        self.compact_repeated_word_to_keys: dict[str, list[str]] = {}
        self.compact_repeated_word_keys_by_length: list[str] = []
        self.repeated_required_by_key: dict[str, int] = {}
        self.repeated_count_by_key: dict[str, int] = {}
        self.repeated_last_pos_by_key: dict[str, int] = {}

        self.heard_market_keys: set[str] = set()
        self.queued_market_keys: set[str] = set()
        self.end_queued_market_keys: set[str] = set()
        # Explicitly removed by Ctrl-D / DISQUALIFY mode. A disqualified market
        # never receives an automatic heard-word order or a later END=>NO order.
        self.disqualified_market_keys: set[str] = set()
        self.finished_market_keys: set[str] = set()

        self.spike_samples_by_key: dict[str, list[tuple[float, int]]] = {}
        self.last_spike_print_ts_by_key: dict[str, float] = {}
        self.spikes_detected = 0

        # Interactive display mode for the < key.
        # "alpha" keeps the familiar word order; "yes_low" surfaces low-YES/high-upside markets.
        self.market_view_sort_mode = "alpha"

        self.last_refresh_text = "never"
        self.refresh_in_progress = False
        self.status = "starting"

        self.typed_chars = 0
        self.detected_yes = 0
        self.duplicate_ignored = 0
        self.end_no_queued = 0
        self.end_no_skipped_disqualified = 0
        self.end_no_skipped_high_yes = 0
        self.end_no_skipped_no_quote = 0
        self.end_no_skipped_low_no_ask = 0
        self.end_no_marked_finished = 0
        self.auto_finished_yes = 0
        self.auto_finished_no = 0
        self.orders_ok = 0
        self.orders_error = 0
        self.last_order_status = "none"

        # Optional manual trade-control state.  Positions are net YES units:
        # positive = YES held; negative = NO held.  They are seeded from the
        # Positions API when trade controls start/refresh, and updated from IOC
        # response fill_count after each manual order.
        self.trade_control_action = "buy"
        self.trade_control_side = "yes"
        self.positions_by_ticker: dict[str, Decimal] = {}
        self.positions_refresh_text = "not loaded"
        self.positions_last_error = ""

        # Runtime-adjustable manual order sizes. Ctrl-T edits the currently
        # selected YES/NO side and updates args.count_yes/count_no so all
        # existing order-building paths keep honoring effective_count_for_side().
        self.trade_control_yes_count = int(effective_count_for_side(args, "yes"))
        self.trade_control_no_count = int(effective_count_for_side(args, "no"))

        # Ctrl-E is intentionally two-step: the first trigger only
        # arms a batch; Enter is required before any END=>NO orders are queued.
        self.end_confirmation_pending = False
        self.end_confirmation_source_label = ""
        self.end_confirmation_typed_tail = ""

        # Ctrl-C is intercepted in terminal input and made explicit: the first
        # keypress arms a clean-exit confirmation; only Enter actually stops
        # the process. Esc (or another key) cancels and leaves trading running.
        self.exit_confirmation_pending = False

        self.install_markets(markets, args=args, refresh_status="initial load")

    def install_markets(self, markets: list[dict], args, refresh_status: str) -> None:
        """Install a fresh market snapshot and rebuild the mention-word index."""
        with self.lock:
            self.markets = list(markets)
            self.market_by_key = {market_key(m): m for m in self.markets}

            word_to_keys: dict[str, list[str]] = {}
            compact_word_to_keys: dict[str, list[str]] = {}
            repeated_word_to_keys: dict[str, list[str]] = {}
            compact_repeated_word_to_keys: dict[str, list[str]] = {}
            repeated_required_by_key: dict[str, int] = {}
            skipped_short = 0

            for market in self.markets:
                key = market_key(market)
                indexed_any_alias = False

                repeat_aliases = repeated_times_trigger_aliases(market)
                if repeat_aliases:
                    for repeat_alias, required_count in repeat_aliases:
                        compact_repeat = normalize_compact_word(repeat_alias)
                        if len(compact_repeat) < args.min_word_len and not compact_repeat.isdigit():
                            skipped_short += 1
                            continue

                        indexed_any_alias = True
                        repeated_word_to_keys.setdefault(repeat_alias, []).append(key)
                        repeated_required_by_key[key] = required_count

                        if " " in repeat_alias and compact_repeat:
                            compact_repeated_word_to_keys.setdefault(compact_repeat, []).append(key)

                    if not indexed_any_alias:
                        skipped_short += 1
                    continue

                for norm in market_trigger_aliases(market):
                    compact_norm = normalize_compact_word(norm)

                    # Do not drop one-character numeric markets like "6".
                    # They are legitimate mention words, and numeric expansion
                    # also adds aliases such as "six".
                    if len(compact_norm) < args.min_word_len and not compact_norm.isdigit():
                        skipped_short += 1
                        continue

                    indexed_any_alias = True
                    word_to_keys.setdefault(norm, []).append(key)

                    # Also index a compact alias only for multi-token phrases.
                    # This lets "J D Vance" match "JDVance" and
                    # "Artificial Intelligence" match "ArtificialIntelligence",
                    # without reintroducing single-word suffix false positives
                    # like "cart" matching "art".
                    if " " in norm and compact_norm:
                        compact_word_to_keys.setdefault(compact_norm, []).append(key)

                if not indexed_any_alias:
                    skipped_short += 1

            self.word_to_keys = word_to_keys
            self.word_keys_by_length = sorted(word_to_keys.keys(), key=len, reverse=True)
            self.compact_word_to_keys = compact_word_to_keys
            self.compact_word_keys_by_length = sorted(compact_word_to_keys.keys(), key=len, reverse=True)
            self.repeated_word_to_keys = repeated_word_to_keys
            self.repeated_word_keys_by_length = sorted(repeated_word_to_keys.keys(), key=len, reverse=True)
            self.compact_repeated_word_to_keys = compact_repeated_word_to_keys
            self.compact_repeated_word_keys_by_length = sorted(compact_repeated_word_to_keys.keys(), key=len, reverse=True)
            self.repeated_required_by_key = repeated_required_by_key
            self.last_refresh_text = datetime.now().strftime("%H:%M:%S")
            trigger_alias_count = len(self.word_to_keys) + len(self.repeated_word_to_keys)
            self.status = (
                f"{refresh_status}: {len(self.markets)} active markets, "
                f"{trigger_alias_count} trigger aliases"
                + (f", skipped_short={skipped_short}" if skipped_short else "")
            )

    def snapshot_for_status(self) -> dict:
        """Return a consistent read-only snapshot for status printing."""
        with self.lock:
            return {
                "n_markets": len(self.markets),
                "n_words": len(self.word_to_keys) + len(self.repeated_word_to_keys),
                "heard": len(self.heard_market_keys),
                "finished": len(self.finished_market_keys),
                "queued": self.order_queue.qsize(),
                "active_order_tasks": self.active_order_tasks,
                "detected_yes": self.detected_yes,
                "duplicate_ignored": self.duplicate_ignored,
                "end_no_queued": self.end_no_queued,
                "end_no_skipped_disqualified": self.end_no_skipped_disqualified,
                "disqualified": len(self.disqualified_market_keys),
                "end_no_skipped_high_yes": self.end_no_skipped_high_yes,
                "end_no_skipped_no_quote": self.end_no_skipped_no_quote,
                "end_no_skipped_low_no_ask": self.end_no_skipped_low_no_ask,
                "end_no_marked_finished": self.end_no_marked_finished,
                "auto_finished_yes": self.auto_finished_yes,
                "auto_finished_no": self.auto_finished_no,
                "spikes_detected": self.spikes_detected,
                "orders_ok": self.orders_ok,
                "orders_error": self.orders_error,
                "status": self.status,
                "last_order_status": self.last_order_status,
                "last_refresh_text": self.last_refresh_text,
                "refresh_in_progress": self.refresh_in_progress,
                "ended": self.ended_event.is_set(),
                "end_confirmation_pending": self.end_confirmation_pending,
                "exit_confirmation_pending": self.exit_confirmation_pending,
                "trade_control_action": self.trade_control_action,
                "trade_control_side": self.trade_control_side,
                "trade_control_yes_count": self.trade_control_yes_count,
                "trade_control_no_count": self.trade_control_no_count,
                "positions_refresh_text": self.positions_refresh_text,
                "positions_last_error": self.positions_last_error,
            }

    def latest_market(self, key: str, fallback: dict) -> dict:
        """Return the newest snapshot for a market key, falling back to task data."""
        with self.lock:
            return self.market_by_key.get(key, fallback)

    def market_for_ticker(self, ticker: str) -> tuple[str, dict] | tuple[None, None]:
        """Return (market_key, market) for a ticker from the current snapshot."""
        target = str(ticker or "").upper()
        if not target:
            return None, None
        with self.lock:
            for key, market in self.market_by_key.items():
                if str(market.get("ticker") or "").upper() == target:
                    return key, market
        return None, None

    def trade_control_snapshot(self) -> tuple[str, str]:
        """Return selected manual action/side from Ctrl-B/Ctrl-S/Ctrl-D and Ctrl-Y/Ctrl-N."""
        with self.lock:
            return self.trade_control_action, self.trade_control_side

    def set_trade_control(self, *, action: str | None = None, side: str | None = None) -> tuple[str, str]:
        """Update and return the manual trade-control selection."""
        with self.lock:
            if action is not None:
                action = str(action).lower()
                if action not in {"buy", "sell", "disqualify"}:
                    raise ValueError(f"unsupported trade control action: {action!r}")
                self.trade_control_action = action
            if side is not None:
                side = str(side).lower()
                if side not in {"yes", "no"}:
                    raise ValueError(f"unsupported trade control side: {side!r}")
                self.trade_control_side = side
            selection = (
                "DISQUALIFY"
                if self.trade_control_action == "disqualify"
                else f"{self.trade_control_action.upper()} {self.trade_control_side.upper()}"
            )
            self.status = f"mode changed: {selection}"
            return self.trade_control_action, self.trade_control_side

    def trade_control_counts_snapshot(self) -> tuple[int, int]:
        """Return runtime-adjustable YES/NO order sizes."""
        with self.lock:
            return int(self.trade_control_yes_count), int(self.trade_control_no_count)

    def set_trade_control_count(self, side: str, count: int, args=None) -> tuple[int, int]:
        """Update runtime YES/NO order size and mirror it onto argparse args."""
        side = str(side or "").lower()
        count = int(count)
        if side not in {"yes", "no"}:
            raise ValueError(f"unsupported trade control count side: {side!r}")
        if count <= 0:
            raise ValueError("order size must be positive")
        with self.lock:
            if side == "yes":
                self.trade_control_yes_count = count
                if args is not None:
                    args.count_yes = count
            else:
                self.trade_control_no_count = count
                if args is not None:
                    args.count_no = count
            self.status = f"{side.upper()} order size changed to {count}"
            return int(self.trade_control_yes_count), int(self.trade_control_no_count)

    def arm_end_confirmation(self, *, source_label: str, typed_tail: str) -> tuple[bool, str]:
        """Arm a Ctrl-E batch without placing or queueing any orders."""
        with self.lock:
            if self.ended_event.is_set():
                return False, "END already handled"
            if self.end_confirmation_pending:
                return False, "END confirmation already pending"
            self.end_confirmation_pending = True
            self.end_confirmation_source_label = source_label
            self.end_confirmation_typed_tail = typed_tail
            self.status = "END confirmation pending: press Enter to submit qualified BUY NO batch"
            return True, self.status

    def cancel_end_confirmation(self, *, reason: str) -> bool:
        """Cancel an armed END batch. Returns True only when one was pending."""
        with self.lock:
            if not self.end_confirmation_pending:
                return False
            self.end_confirmation_pending = False
            self.end_confirmation_source_label = ""
            self.end_confirmation_typed_tail = ""
            self.status = f"END confirmation cancelled ({reason})"
            return True

    def take_end_confirmation(self) -> tuple[str, str] | None:
        """Consume a pending END confirmation for Enter-driven submission."""
        with self.lock:
            if not self.end_confirmation_pending:
                return None
            source_label = self.end_confirmation_source_label
            typed_tail = self.end_confirmation_typed_tail
            self.end_confirmation_pending = False
            self.end_confirmation_source_label = ""
            self.end_confirmation_typed_tail = ""
            self.status = "END confirmed; evaluating qualified BUY NO batch"
            return source_label, typed_tail

    def arm_exit_confirmation(self) -> tuple[bool, str]:
        """Arm Ctrl-C exit confirmation without stopping the trader."""
        with self.lock:
            if self.exit_confirmation_pending:
                return False, "exit confirmation already pending"
            self.exit_confirmation_pending = True
            self.status = "exit confirmation pending: press Enter to exit or Esc to continue"
            return True, self.status

    def cancel_exit_confirmation(self, *, reason: str) -> bool:
        """Cancel a pending Ctrl-C exit confirmation."""
        with self.lock:
            if not self.exit_confirmation_pending:
                return False
            self.exit_confirmation_pending = False
            self.status = f"exit confirmation cancelled ({reason})"
            return True

    def take_exit_confirmation(self) -> bool:
        """Consume a pending exit confirmation. Returns True only when armed."""
        with self.lock:
            if not self.exit_confirmation_pending:
                return False
            self.exit_confirmation_pending = False
            self.status = "exit confirmed"
            return True

    def disqualify_market(self, key: str) -> tuple[bool, dict | None]:
        """Mark one market excluded from automatic buys and future Ctrl-E NO orders."""
        with self.lock:
            market = self.market_by_key.get(key)
            if market is None:
                return False, None
            if key in self.disqualified_market_keys:
                return False, market
            self.disqualified_market_keys.add(key)
            self.status = f"Disqualified: {market_word(market)}"
            return True, market

    def replace_positions(self, positions: dict[str, Decimal], *, note: str) -> None:
        """Replace cached positions with an authoritative Positions API snapshot."""
        with self.lock:
            known = {str(m.get("ticker") or "").upper() for m in self.markets if m.get("ticker")}
            self.positions_by_ticker = {
                ticker: decimal_count_value(positions.get(ticker)) or Decimal("0")
                for ticker in known
            }
            self.positions_refresh_text = note
            self.positions_last_error = ""

    def merge_positions(self, positions: dict[str, Decimal], *, note: str) -> None:
        """Merge one or more authoritative position values into the local cache."""
        with self.lock:
            for ticker, position in positions.items():
                key = str(ticker or "").upper()
                if key:
                    self.positions_by_ticker[key] = decimal_count_value(position) or Decimal("0")
            self.positions_refresh_text = note
            self.positions_last_error = ""

    def position_for_ticker(self, ticker: str) -> Decimal:
        with self.lock:
            return self.positions_by_ticker.get(str(ticker or "").upper(), Decimal("0"))

    def note_position_refresh_error(self, text: str) -> None:
        with self.lock:
            self.positions_last_error = text

    def apply_manual_fill(self, *, ticker: str, side: str, trade_action: str, filled_count: int | Decimal | None) -> Decimal:
        """Apply one known immediate IOC fill to the local net-position display."""
        filled = decimal_count_value(filled_count)
        if filled is None or filled <= 0:
            return self.position_for_ticker(ticker)
        side = str(side).lower()
        trade_action = str(trade_action).lower()
        if side not in {"yes", "no"} or trade_action not in {"buy", "sell"}:
            return self.position_for_ticker(ticker)

        # BUY YES / SELL NO increase net YES.  BUY NO / SELL YES decrease it.
        sign = 1 if (trade_action == "buy" and side == "yes") or (trade_action == "sell" and side == "no") else -1
        key = str(ticker or "").upper()
        with self.lock:
            value = self.positions_by_ticker.get(key, Decimal("0")) + Decimal(sign) * filled
            self.positions_by_ticker[key] = value
            self.positions_refresh_text = "updated from immediate IOC fills"
            return value

    def set_status(self, status: str) -> None:
        """Set human-readable status."""
        with self.lock:
            self.status = status

    def mark_order_task_started(self) -> None:
        """Mark that an order worker is now in the hot submit/response path."""
        with self.lock:
            self.active_order_tasks += 1

    def mark_order_task_finished_and_idle(self) -> bool:
        """Return True when no queued or active order tasks remain.

        This is stricter than Queue.empty(): with multiple workers the queue can
        be empty while another worker is still waiting on an API response. We
        only want deferred log flushes after the response is handled and the
        whole order side is idle.
        """
        with self.lock:
            if self.active_order_tasks > 0:
                self.active_order_tasks -= 1
            return self.active_order_tasks == 0 and self.order_queue.empty()

    def order_side_idle(self) -> bool:
        """Return True if no order tasks are queued or being processed."""
        with self.lock:
            return self.active_order_tasks == 0 and self.order_queue.empty()

    def record_order_result(self, ok: bool, status: str) -> None:
        """Update aggregate order counters."""
        with self.lock:
            if ok:
                self.orders_ok += 1
            else:
                self.orders_error += 1
            self.last_order_status = status
            self.status = status


# -----------------------------------------------------------------------------
# Worker threads
# -----------------------------------------------------------------------------


PRINT_LOCK = threading.RLock()


def safe_print(message: str = "") -> None:
    """Serialize console prints from multiple threads."""
    with PRINT_LOCK:
        print(message, flush=True)


def market_refresh_worker(
    *,
    refresh_client: KalshiClient | None,
    args,
    state: SharedBroadcastState,
) -> None:
    """
    Dedicated market refresh loop.

    This thread updates all current bid/ask snapshots. If a network request takes
    longer than --refresh-seconds, refreshes naturally run at API speed rather
    than piling up overlapping calls.
    """
    while not state.stop_event.is_set():
        requested = False
        if args.refresh_seconds > 0:
            requested = state.refresh_now_event.wait(args.refresh_seconds)
        else:
            requested = state.refresh_now_event.wait(0.1)
            if not requested:
                continue

        state.refresh_now_event.clear()
        if state.stop_event.is_set():
            break

        try:
            with state.lock:
                state.refresh_in_progress = True

            markets = load_current_markets(refresh_client, args)
            state.install_markets(
                markets,
                args=args,
                refresh_status="manual refresh" if requested else "auto refresh",
            )

        except Exception as exc:
            prefix = "Manual refresh failed" if requested else "Auto refresh failed"
            state.set_status(f"{prefix}: {exc}")

        finally:
            with state.lock:
                state.refresh_in_progress = False


def queue_order_task(state: SharedBroadcastState, task: OrderTask) -> None:
    """Queue one task; manual controls intentionally permit repeated selections."""
    if not task.suppress_duplicate_guard:
        state.queued_market_keys.add(task.market_key)
    state.order_queue.put(task)


def trade_control_heading(state: SharedBroadcastState) -> str:
    """Return a compact description of the selected manual action and side."""
    action, side = state.trade_control_snapshot()
    if action == "disqualify":
        return "DISQUALIFY"
    return f"{action.upper()} {side.upper()}"


def trade_control_status_line(state: SharedBroadcastState, args) -> str:
    """Human-readable current mode and runtime order-size summary."""
    action, side = state.trade_control_snapshot()
    yes_count, no_count = state.trade_control_counts_snapshot()
    if action == "disqualify":
        mode = "DISQUALIFY"
        detail = "select a market and press Enter to exclude it; no order will be placed"
    elif action == "sell":
        mode = f"SELL {side.upper()}"
        detail = f"select one owned {side.upper()} market and press Enter to sell that position"
    else:
        active_count = yes_count if side == "yes" else no_count
        mode = f"BUY {side.upper()}"
        detail = f"next confirmed buy uses {active_count} contracts"
    return f"MODE: {mode} | YES size: {yes_count} | NO size: {no_count} | {detail}"


def print_trade_control_status(state: SharedBroadcastState, args, *, prefix: str = "TRADE CONTROL") -> None:
    """Print the current trade-control mode in a conspicuous, plain-English line."""
    safe_print("\n" + color_text(prefix, "magenta", "bold") + ": " + trade_control_status_line(state, args))


def print_key_help(state: SharedBroadcastState, args) -> None:
    """Print the in-session key help panel (Ctrl-H), then the current mode line."""
    lines = [
        "═══ KEY HELP ═══",
        "Modes (pick action + side):",
        "  Ctrl-B  BUY mode          Ctrl-S  SELL mode",
        "  Ctrl-D  DISQUALIFY mode   (excludes market from auto YES + Ctrl-E NO)",
        "  Ctrl-Y  side YES          Ctrl-N  side NO",
        "           (from DISQUALIFY: switches to BUY YES / BUY NO)",
        "  Ctrl-T  edit size for the current side (digits, Enter)",
        "  Ctrl-R  refresh books + positions",
        "",
        "Trading:",
        "  Type to filter/highlight  Enter  confirm highlighted action",
        "  Ctrl-E then Enter         BUY NO on remaining qualified markets",
        "  Typed END                 plain text (not a command)",
        "",
        "Exit / cancel:",
        "  Ctrl-C then Enter         exit    Esc  cancel pending confirm",
        "  Esc / other key           cancel armed END or exit confirm",
        "",
        "Help: Ctrl-H  (this panel)",
        "Backspace: terminal DEL key (not Ctrl-H)",
        "",
        "Current: " + trade_control_status_line(state, args),
        "════════════════",
    ]
    safe_print("\n" + "\n".join(lines))
    if getattr(args, "trade_controls", False):
        print_trade_control_status(state, args, prefix="CURRENT MODE")
    record_transcript_event(args, "help_shown", {"hotkey": "Ctrl-H"})


def switch_disqualify_side_to_buy(state: SharedBroadcastState, args, *, side_key: str) -> None:
    """From DISQUALIFY, Ctrl-Y/N jump to BUY on that side with a plain-English notice.

    Operators often press Ctrl-Y after Ctrl-D expecting BUY YES. Honor that intent:
    leave DISQUALIFY, enter BUY, set the requested side, and say what changed.
    """
    side_key = "yes" if str(side_key).lower() == "yes" else "no"
    side_label = side_key.upper()
    hotkey = "Ctrl-Y" if side_key == "yes" else "Ctrl-N"
    state.set_trade_control(action="buy", side=side_key)
    safe_print(
        "\n"
        + color_text("MODE CHANGED", "green", "bold")
        + f": left DISQUALIFY → now BUY {side_label} (via {hotkey})."
    )
    safe_print(
        f"  You were excluding markets; {hotkey} switched you into trading on the {side_label} side."
    )
    safe_print("  Type a market and press Enter to BUY. Ctrl-D returns to DISQUALIFY. Ctrl-S for SELL. Help: Ctrl-H")
    print_trade_control_status(state, args, prefix="NOW")
    record_transcript_event(
        args,
        "disqualify_side_switched_to_buy",
        {"side_key": side_key, "hotkey": hotkey, "new_action": "buy"},
    )


def print_mode_entry_coach(state: SharedBroadcastState, args, *, action: str) -> None:
    """Print a plain-English blurb when entering BUY, SELL, or DISQUALIFY."""
    action = str(action or "").lower()
    if action == "disqualify":
        safe_print(
            color_text("MODE COACH", "cyan", "bold")
            + ": DISQUALIFY mode — no orders. Type a market + Enter to exclude it from auto YES and Ctrl-E NO."
        )
        safe_print("  Press Ctrl-B for BUY or Ctrl-S for SELL to trade again. Help: Ctrl-H")
    elif action == "sell":
        _action, side = state.trade_control_snapshot()
        safe_print(
            color_text("MODE COACH", "cyan", "bold")
            + f": SELL mode — type an owned market + Enter for reduce-only close on side {side.upper()}."
        )
        safe_print("  Ctrl-Y/N pick side. Help: Ctrl-H")
    else:
        _action, side = state.trade_control_snapshot()
        safe_print(
            color_text("MODE COACH", "cyan", "bold")
            + f": BUY mode — type a market + Enter to buy {side.upper()}."
        )
        safe_print("  Ctrl-Y/N pick side only; Ctrl-B/S/D change action. Help: Ctrl-H")
    print_trade_control_status(state, args, prefix="MODE CHANGED")


def print_trade_control_board(state: SharedBroadcastState, args, *, heading: str = "TRADE CONTROL BOOK") -> None:
    """Print selected mode, cached positions, and current WS best books."""
    safe_print("\n" + color_text(heading, "yellow", "bold") + ": " + trade_control_status_line(state, args))
    with state.lock:
        markets = list(state.markets)
        positions_note = state.positions_refresh_text
        positions_error = state.positions_last_error
    for market in sorted(markets, key=lambda m: market_word(m).lower()):
        ticker = str(market.get("ticker") or "")
        try:
            quote = latest_quote_for_market(args, market)
            book = quote_dual_book_summary(quote, args=args)
            age = quote.get("ws_age_ms")
            age_text = f" ws_age={age}ms" if age is not None else ""
        except Exception as exc:
            book = f"book unavailable: {exc}"
            age_text = ""
        pos = state.position_for_ticker(ticker)
        with state.lock:
            disqualified = market_key(market) in state.disqualified_market_keys
        disqualified_text = " | DISQUALIFIED" if disqualified else ""
        safe_print(
            f"  {market_word(market)} [{ticker}] | {book} | position={format_net_position(pos)}{age_text}{disqualified_text}"
        )
    suffix = f" | positions={positions_note}"
    if positions_error:
        suffix += f" | last position refresh error={positions_error}"
    safe_print(
        "  "
        + color_text("Controls", "cyan", "bold")
        + ": Ctrl-B/S/D action, Ctrl-Y/N side, Ctrl-T size, Ctrl-R refresh, Ctrl-H help"
        + suffix
    )
    safe_print("  " + color_text("Current", "cyan", "bold") + ": " + trade_control_status_line(state, args))


def refresh_trade_control_display(state: SharedBroadcastState, args, *, source: str) -> None:
    """Request fresh WS snapshots, refresh positions, then render the board."""
    manager = getattr(args, "_orderbook_manager", None)
    tickers = list(getattr(args, "_ws_market_tickers", []) or [])
    if manager is not None and tickers:
        request_ws_snapshots(args, tickers, source=source)
        wait_ms = max(0, int(getattr(args, "trade_control_refresh_wait_ms", 120)))
        if wait_ms:
            time.sleep(wait_ms / 1000.0)

    try:
        with state.lock:
            markets = list(state.markets)
        positions = fetch_positions_for_loaded_markets(args, markets)
        state.replace_positions(positions, note=datetime.now().strftime("%H:%M:%S") + f" via {source}")
    except Exception as exc:
        state.note_position_refresh_error(str(exc))
        safe_print(color_text(f"POSITION REFRESH ERROR: {exc}", "red", "bold"))

    print_trade_control_board(state, args, heading="TRADE CONTROL REFRESH")


def handle_trade_control_word(
    *,
    state: SharedBroadcastState,
    args,
    word_key: str,
    typed_tail: str,
    compact_match: bool = False,
) -> None:
    """Queue a manual BUY task or legacy typed SELL-position task for matching markets."""
    with state.lock:
        mapping = state.compact_word_to_keys if compact_match else state.word_to_keys
        market_keys = list(mapping.get(word_key, []))
        action, side = state.trade_control_action, state.trade_control_side
        if action == "disqualify":
            return
        queued_words: list[str] = []
        for key in market_keys:
            if key in state.disqualified_market_keys:
                continue
            market = state.market_by_key.get(key)
            if market is None:
                continue
            queue_order_task(
                state,
                OrderTask(
                    action="manual_buy" if action == "buy" else "manual_sell_position",
                    market_key=key,
                    market_snapshot=market,
                    side=side,
                    trigger=(f"trade controls typed compact word {word_key}" if compact_match else f"trade controls typed word {word_key}"),
                    typed_tail=typed_tail,
                    trade_action=action,
                    sell_all=(action == "sell"),
                    suppress_duplicate_guard=True,
                ),
            )
            # A manual BUY confirms that this market was intentionally selected,
            # so Ctrl-E will not later add an unintended END=>NO order.
            if action == "buy":
                state.heard_market_keys.add(key)
            queued_words.append(market_word(market))

        if queued_words:
            summary = f"Queued manual {action.upper()}" + (" position" if action == "sell" else "") + f" {side.upper()}: " + ", ".join(queued_words)
            state.status = summary
            safe_print("\n" + color_text("MANUAL", "magenta", "bold") + ": " + summary)


def handle_trade_control_repeated_word(
    *,
    state: SharedBroadcastState,
    args,
    word_key: str,
    typed_tail: str,
    compact_match: bool = False,
) -> None:
    """Repeated-word variant of manual controls; queues once threshold is met."""
    with state.lock:
        mapping = state.compact_repeated_word_to_keys if compact_match else state.repeated_word_to_keys
        market_keys = list(mapping.get(word_key, []))
        action, side = state.trade_control_action, state.trade_control_side
        if action == "disqualify":
            return
        typed_pos = state.typed_chars
        queued_words: list[str] = []
        count_messages: list[str] = []

        for key in market_keys:
            if state.repeated_last_pos_by_key.get(key) == typed_pos:
                continue
            state.repeated_last_pos_by_key[key] = typed_pos
            if key in state.disqualified_market_keys:
                continue
            market = state.market_by_key.get(key)
            if market is None:
                continue
            required = state.repeated_required_by_key.get(key, 1)
            current = state.repeated_count_by_key.get(key, 0) + 1
            state.repeated_count_by_key[key] = current
            if current < required:
                count_messages.append(f"{market_word(market)} {current}/{required}")
                continue
            queue_order_task(
                state,
                OrderTask(
                    action="manual_buy" if action == "buy" else "manual_sell_position",
                    market_key=key,
                    market_snapshot=market,
                    side=side,
                    trigger=f"trade controls repeated word {word_key} {current}/{required}",
                    typed_tail=typed_tail,
                    trade_action=action,
                    sell_all=(action == "sell"),
                    suppress_duplicate_guard=True,
                ),
            )
            if action == "buy":
                state.heard_market_keys.add(key)
            queued_words.append(market_word(market))

        if queued_words:
            safe_print("\n" + color_text("MANUAL repeated", "magenta", "bold") + ": " + f"queued {action.upper()} {side.upper()} for " + ", ".join(queued_words))
        elif count_messages:
            safe_print("\n" + color_text("COUNT", "yellow", "bold") + ": " + ", ".join(count_messages))


def handle_detected_word(
    *,
    state: SharedBroadcastState,
    args,
    word_key: str,
    typed_tail: str,
    compact_match: bool = False,
) -> None:
    """
    Convert a typed-word match into one or more ordinary mention-market tasks.

    With --trade-controls, the current Ctrl-selected manual action/side replaces
    normal duplicate-suppressed mention handling.
    """
    if getattr(args, "trade_controls", False):
        handle_trade_control_word(
            state=state,
            args=args,
            word_key=word_key,
            typed_tail=typed_tail,
            compact_match=compact_match,
        )
        return
    with state.lock:
        if state.ended_event.is_set() and not args.allow_orders_after_end:
            state.status = f"Ignored word after END: {word_key}"
            return

        if compact_match:
            market_keys = state.compact_word_to_keys.get(word_key, [])
        else:
            market_keys = state.word_to_keys.get(word_key, [])

        queued_words: list[str] = []

        for key in market_keys:
            if key in state.disqualified_market_keys:
                continue
            if key in state.heard_market_keys or key in state.queued_market_keys:
                state.duplicate_ignored += 1
                continue

            market = state.market_by_key.get(key)
            if market is None:
                continue

            # Mark heard immediately. If the order fails, the word was still
            # heard, so END should not later buy NO for it.
            state.heard_market_keys.add(key)
            state.detected_yes += 1
            queued_words.append(market_word(market))

            record_transcript_event(
                args,
                "detected_word",
                {
                    "word_key": word_key,
                    "market_word": market_word(market),
                    "ticker": market.get("ticker"),
                    "side": args.mention_side,
                    "typed_tail": typed_tail,
                    "compact_match": compact_match,
                },
            )

            queue_order_task(
                state,
                OrderTask(
                    action="heard_word",
                    market_key=key,
                    market_snapshot=market,
                    side=args.mention_side,
                    trigger=(
                        f"typed compact word {word_key}"
                        if compact_match
                        else f"typed word {word_key}"
                    ),
                    typed_tail=typed_tail,
                ),
            )

        if queued_words:
            side_text = args.mention_side.upper()
            state.status = "Queued BUY %s for heard: %s" % (
                side_text,
                ", ".join(queued_words),
            )
            safe_print(
                "\n"
                + color_text("DETECTED", "green", "bold")
                + ": "
                + color_text(", ".join(queued_words), "bold", "cyan")
                + " -> "
                + color_text(f"queued BUY {side_text}", "green", "bold")
            )
            print_remaining_actionable_markets(state, args)




def handle_repeated_word(
    *,
    state: SharedBroadcastState,
    args,
    word_key: str,
    typed_tail: str,
    compact_match: bool = False,
) -> None:
    """
    Count a repeated-word market, such as ``Trump (3+ times)``.

    The market is queued only when the typed/heard base word reaches the
    required count. Before that, we only update the counter and transcript.
    """
    if getattr(args, "trade_controls", False):
        handle_trade_control_repeated_word(
            state=state,
            args=args,
            word_key=word_key,
            typed_tail=typed_tail,
            compact_match=compact_match,
        )
        return
    with state.lock:
        if state.ended_event.is_set() and not args.allow_orders_after_end:
            state.status = f"Ignored repeated word after END: {word_key}"
            return

        if compact_match:
            market_keys = state.compact_repeated_word_to_keys.get(word_key, [])
        else:
            market_keys = state.repeated_word_to_keys.get(word_key, [])

        queued_words: list[str] = []
        count_messages: list[str] = []
        typed_pos = state.typed_chars

        for key in market_keys:
            if key in state.disqualified_market_keys:
                continue
            if key in state.heard_market_keys or key in state.queued_market_keys:
                state.duplicate_ignored += 1
                continue

            # Prevent double-counting the same typed character if both phrase
            # and compact aliases happen to match on one input position.
            if state.repeated_last_pos_by_key.get(key) == typed_pos:
                continue
            state.repeated_last_pos_by_key[key] = typed_pos

            market = state.market_by_key.get(key)
            if market is None:
                continue

            required_count = state.repeated_required_by_key.get(key, 1)
            current_count = state.repeated_count_by_key.get(key, 0) + 1
            state.repeated_count_by_key[key] = current_count

            record_transcript_event(
                args,
                "repeated_word_count",
                {
                    "word_key": word_key,
                    "market_word": market_word(market),
                    "ticker": market.get("ticker"),
                    "side": args.mention_side,
                    "typed_tail": typed_tail,
                    "compact_match": compact_match,
                    "count": current_count,
                    "required_count": required_count,
                },
            )

            if current_count < required_count:
                count_messages.append(
                    f"{market_word(market)} {current_count}/{required_count}"
                )
                continue

            # Mark heard only when the repeated-word condition is satisfied.
            state.heard_market_keys.add(key)
            state.detected_yes += 1
            queued_words.append(market_word(market))

            record_transcript_event(
                args,
                "detected_repeated_word",
                {
                    "word_key": word_key,
                    "market_word": market_word(market),
                    "ticker": market.get("ticker"),
                    "side": args.mention_side,
                    "typed_tail": typed_tail,
                    "compact_match": compact_match,
                    "count": current_count,
                    "required_count": required_count,
                },
            )

            queue_order_task(
                state,
                OrderTask(
                    action="heard_repeated_word",
                    market_key=key,
                    market_snapshot=market,
                    side=args.mention_side,
                    trigger=(
                        f"typed compact word {word_key} "
                        f"{current_count}/{required_count} times"
                        if compact_match
                        else f"typed word {word_key} {current_count}/{required_count} times"
                    ),
                    typed_tail=typed_tail,
                ),
            )

        if queued_words:
            side_text = args.mention_side.upper()
            state.status = "Queued BUY %s for repeated heard: %s" % (
                side_text,
                ", ".join(queued_words),
            )
            safe_print(
                "\n"
                + color_text("DETECTED repeated", "green", "bold")
                + ": "
                + color_text(", ".join(queued_words), "bold", "cyan")
                + " -> "
                + color_text(f"queued BUY {side_text}", "green", "bold")
            )
            print_remaining_actionable_markets(state, args)
        elif count_messages:
            state.status = "Counted repeated trigger: " + ", ".join(count_messages)
            safe_print("\n" + color_text("COUNT", "yellow", "bold") + ": " + ", ".join(count_messages))


def format_auto_finish_item(market: dict, quote: dict, yes_guard: int | None, side: str) -> str:
    """Format one auto-finished market with color and probability."""
    label = color_text(market_label(market), "bold", "cyan")
    tickerish = f"{label} {quote_prob_summary(quote, yes_guard)}"
    if side == "yes":
        return tickerish.replace("YES=", color_text("YES=", "green", "bold"), 1)
    return tickerish.replace("NO≈", color_text("NO≈", "red", "bold"), 1)


def market_view_sort_heading(sort_mode: str) -> str:
    """Human-readable name for an open-market view sort mode."""
    if sort_mode == "yes_low":
        return "YES low->high"
    return "alpha"


def next_market_view_sort_mode(sort_mode: str) -> str:
    """Cycle between the two quick-view sorts used by the < key."""
    return "yes_low" if sort_mode != "yes_low" else "alpha"


def market_view_sort_key(market: dict, yes_prob: int | float | None, sort_mode: str) -> tuple:
    """Sort key for open-market displays."""
    label = market_word(market).lower()
    if sort_mode == "yes_low":
        if yes_prob is None:
            return (1, 101.0, label)
        try:
            return (0, float(yes_prob), label)
        except Exception:
            return (1, 101.0, label)
    return (label,)


def remaining_market_prob_lines(
    state: SharedBroadcastState,
    args,
    *,
    limit: int = 30,
    sort_mode: str = "alpha",
) -> list[str]:
    """Return formatted probability lines for markets not yet auto-finished or disqualified."""
    rows: list[tuple[tuple, str]] = []

    for market in state.markets:
        key = market_key(market)
        if key in state.finished_market_keys or key in state.disqualified_market_keys:
            continue

        try:
            quote = latest_quote_for_market(args, market)
            yes_prob = quote_yes_probability_cents(quote)
        except Exception:
            quote = {}
            yes_prob = None

        label = color_text(market_label(market), "bold")
        line = f"  • {label} {quote_prob_summary(quote, yes_prob, include_spread=True, args=args)}"
        rows.append((market_view_sort_key(market, yes_prob, sort_mode), line))

    rows.sort(key=lambda item: item[0])
    lines = [line for _, line in rows[:limit]]
    remaining = len(rows) - len(lines)
    if remaining > 0:
        lines.append(f"  … {remaining} more remaining market(s)")
    return lines


def remaining_actionable_market_prob_lines(
    state: SharedBroadcastState,
    args,
    *,
    limit: int = 30,
    sort_mode: str = "alpha",
) -> list[str]:
    """Return un-heard, un-queued, un-finished, non-disqualified markets with current probabilities."""
    rows: list[tuple[tuple, str]] = []

    for market in state.markets:
        key = market_key(market)
        if (
            key in state.disqualified_market_keys
            or key in state.finished_market_keys
            or key in state.heard_market_keys
            or key in state.queued_market_keys
            or key in state.end_queued_market_keys
        ):
            continue

        try:
            quote = latest_quote_for_market(args, market)
            yes_prob = quote_yes_probability_cents(quote)
        except Exception:
            quote = {}
            yes_prob = None

        label = color_text(market_label(market), "bold")
        line = f"  • {label} {quote_prob_summary(quote, yes_prob, include_spread=True, args=args)}"
        rows.append((market_view_sort_key(market, yes_prob, sort_mode), line))

    rows.sort(key=lambda item: item[0])
    lines = [line for _, line in rows[:limit]]
    remaining = len(rows) - len(lines)
    if remaining > 0:
        lines.append(f"  … {remaining} more remaining open market(s)")
    return lines


def print_remaining_actionable_markets(
    state: SharedBroadcastState,
    args,
    *,
    limit: int = 30,
    heading: str = "Remaining open markets",
    sort_mode: str = "alpha",
) -> None:
    """Print remaining markets that END could still act on, with probabilities."""
    with state.lock:
        lines = remaining_actionable_market_prob_lines(state, args, limit=limit, sort_mode=sort_mode)

    sort_suffix = f" ({market_view_sort_heading(sort_mode)})"
    if lines:
        safe_print(color_text(heading + sort_suffix + ":", "yellow", "bold") + "\n" + "\n".join(lines))
    else:
        safe_print(color_text(heading + sort_suffix + ": none", "yellow", "bold"))


def clear_current_terminal_line() -> None:
    """Erase the current in-progress typing line before printing a multi-line view."""
    with PRINT_LOCK:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def toggle_and_print_open_market_view(state: SharedBroadcastState, args, *, limit: int = 30) -> None:
    """Cycle the open-market sort and print the current view for the < hotkey."""
    with state.lock:
        state.market_view_sort_mode = next_market_view_sort_mode(state.market_view_sort_mode)
        sort_mode = state.market_view_sort_mode

    clear_current_terminal_line()
    print_remaining_actionable_markets(
        state,
        args,
        limit=limit,
        heading="Remaining open markets",
        sort_mode=sort_mode,
    )

def mark_finished_markets_from_quotes(state: SharedBroadcastState, args) -> int:
    """
    Passively mark markets finished when the book says they are effectively done.

    YES-finished: YES probability reaches --auto-finish-yes-at, default 99c.
    NO-finished:  YES probability falls to --auto-finish-no-at, default 1c.

    Finished markets are skipped by END so the script does not try to buy the
    other side after the market has already converged.
    """
    if args.auto_finish_yes_at_cents is None and args.auto_finish_no_at_cents is None:
        return 0

    newly_finished = 0
    yes_finished_labels: list[str] = []
    no_finished_labels: list[str] = []
    remaining_lines: list[str] = []

    with state.lock:
        for market in state.markets:
            key = market_key(market)
            if key in state.finished_market_keys:
                continue

            try:
                quote = latest_quote_for_market(args, market)
                yes_high_signal = quote_yes_guard_cents(quote)
                yes_low_signal = quote_yes_low_finish_cents(quote)
                yes_display = quote_yes_probability_cents(quote)
            except Exception:
                continue

            finished_side: str | None = None
            if (
                yes_high_signal is not None
                and args.auto_finish_yes_at_cents is not None
                and yes_high_signal >= args.auto_finish_yes_at_cents
            ):
                finished_side = "yes"
            elif (
                yes_low_signal is not None
                and args.auto_finish_no_at_cents is not None
                and yes_low_signal <= args.auto_finish_no_at_cents
            ):
                finished_side = "no"

            if finished_side is None:
                continue

            state.finished_market_keys.add(key)
            state.end_no_marked_finished += 1
            newly_finished += 1

            if finished_side == "yes":
                state.auto_finished_yes += 1
                yes_finished_labels.append(format_auto_finish_item(market, quote, yes_display, "yes"))
            else:
                state.auto_finished_no += 1
                no_finished_labels.append(format_auto_finish_item(market, quote, yes_display, "no"))

        if newly_finished:
            bits: list[str] = []
            if yes_finished_labels:
                bits.append(f"YES={len(yes_finished_labels)}")
            if no_finished_labels:
                bits.append(f"NO={len(no_finished_labels)}")
            state.status = f"Auto-finished {newly_finished} market(s): " + ", ".join(bits)
            remaining_lines = remaining_market_prob_lines(state, args)

    # Timestamp this console event at millisecond precision.  The intent is to
    # compare it with external real-time detectors running on this same host.
    auto_finish_stamp = local_clock_millis() if newly_finished else ""

    if yes_finished_labels:
        safe_print(
            "\n["
            + auto_finish_stamp
            + "] "
            + color_text("AUTO-FINISH YES detected", "green", "bold")
            + ": "
            + ", ".join(yes_finished_labels)
        )
    if no_finished_labels:
        safe_print(
            "\n["
            + auto_finish_stamp
            + "] "
            + color_text("AUTO-FINISH NO detected", "red", "bold")
            + ": "
            + ", ".join(no_finished_labels)
        )
    if newly_finished:
        if remaining_lines:
            safe_print(color_text("Remaining open markets:", "yellow", "bold") + "\n" + "\n".join(remaining_lines))
        else:
            safe_print(color_text("Remaining open markets: none", "yellow", "bold"))

    return newly_finished


def quote_yes_spike_signal_cents(quote: dict) -> int | None:
    """Return the earliest YES-side price signal used for spike detection.

    We still watch the YES ask because it moves immediately when aggressive YES
    buying eats through offers.  But the alert printer now includes bid/ask
    spread and labels huge-spread moves as unconfirmed, so one thin ask does
    not look like a clean market-wide repricing.
    """
    for field in ("yes_ask_cents", "mid_yes_cents", "yes_bid_cents"):
        value = quote.get(field)
        if isinstance(value, (int, float)):
            return int(round(value))

    return None


def detect_yes_spike_for_market(
    *,
    state: SharedBroadcastState,
    args,
    market: dict,
    key: str,
) -> bool:
    """Detect and print a fast YES price jump without marking the market heard."""
    if not getattr(args, "spike_detect", True):
        return False

    try:
        quote = latest_quote_for_market(args, market)
    except Exception:
        return False

    current = quote_yes_spike_signal_cents(quote)
    if current is None:
        return False

    now = time.time()
    window = float(args.spike_window_seconds)
    cutoff = now - window

    should_print = False
    prior_price = None
    prior_age = None
    rise = 0

    with state.lock:
        if (
            key in state.finished_market_keys
            or key in state.heard_market_keys
            or key in state.queued_market_keys
            or key in state.end_queued_market_keys
        ):
            return False

        samples = [sample for sample in state.spike_samples_by_key.get(key, []) if sample[0] >= cutoff]

        if samples:
            min_ts, min_price = min(samples, key=lambda sample: sample[1])
            rise = int(current) - int(min_price)
            last_print = state.last_spike_print_ts_by_key.get(key, 0.0)
            if rise >= args.spike_move_cents and now - last_print >= args.spike_cooldown_seconds:
                should_print = True
                prior_price = int(min_price)
                prior_age = now - min_ts
                state.last_spike_print_ts_by_key[key] = now
                state.spikes_detected += 1
                state.status = (
                    f"Spike detected: {market_word(market)} "
                    f"YES {cents_to_dollars(prior_price)} -> {cents_to_dollars(current)}"
                )

        samples.append((now, int(current)))
        # Keep a small extra tail so the next update can still compare against
        # prices just inside the window without letting this dict grow forever.
        state.spike_samples_by_key[key] = samples[-64:]

    if not should_print:
        return False

    prior_text = cents_to_dollars(prior_price) if prior_price is not None else "--"
    age_text = f" in {prior_age:.2f}s" if prior_age is not None else ""
    rise_text = f"+{rise}¢"
    yes_prob = quote_yes_probability_cents(quote)
    prob_text = quote_prob_summary(quote, yes_prob, include_spread=True, args=args)
    wide_spread, spread, max_spread = should_treat_spike_as_wide_spread(args, quote)
    book_text = quote_top_of_book_summary(quote, args=args)

    record_transcript_event(
        args,
        "yes_spike_detected",
        {
            "word": market_word(market),
            "ticker": market.get("ticker"),
            "market_key": key,
            "current_yes_cents": current,
            "prior_yes_cents": prior_price,
            "rise_cents": rise,
            "window_seconds": window,
            "prior_age_seconds": prior_age,
            "wide_spread": wide_spread,
            "spread_cents": spread,
            "max_spread_cents": max_spread,
            "quote": quote,
        },
    )

    title = color_text("⚡ SPIKE DETECTED", "yellow", "bold")
    spread_note = ""
    if wide_spread:
        title += " " + color_text("WIDE SPREAD / UNCONFIRMED", "red", "bold")
        spread_note = (
            f" | spread {format_cents_value(spread)} > "
            f"limit {format_cents_value(max_spread)}"
        )

    safe_print(
        "\n"
        + title
        + ": "
        + color_text(market_label(market), "bold", "cyan")
        + " "
        + prob_text.replace("YES=", color_text("YES=", "green", "bold"), 1)
        + f" | book={book_text}"
        + f" | move={color_text(rise_text, 'yellow', 'bold')} "
        + f"({prior_text} -> {cents_to_dollars(current)}{age_text})"
        + spread_note
    )
    return True


def spike_worker(*, args, state: SharedBroadcastState) -> None:
    """Watch WebSocket orderbook updates and print early YES spike alerts."""
    manager = getattr(args, "_orderbook_manager", None)
    if manager is None or not getattr(args, "spike_detect", True):
        return

    while not state.stop_event.is_set():
        update = manager.wait_for_update(timeout=0.1)
        if update is None:
            continue

        key, market = state.market_for_ticker(getattr(update, "market_ticker", ""))
        if key is None or market is None:
            continue

        detect_yes_spike_for_market(state=state, args=args, market=market, key=key)


def handle_end_token(
    *,
    state: SharedBroadcastState,
    args,
    typed_tail: str,
    source_label: str = "Ctrl-E",
) -> None:
    """
    Handle a confirmed Ctrl-E END trigger.

    END is intentionally conservative:
      - skip heard markets
      - skip markets already auto-finished at YES >= --auto-finish-yes-at
      - queue BUY NO only when the current YES signal is below
        --end-no-skip-yes-at
      - queue BUY NO only when the current NO ask is at least
        --end-no-min-no-ask, default 4c, which means NO > 3c
      - optionally require some usable YES quote before queueing
    """
    with state.lock:
        if state.ended_event.is_set():
            state.status = "END already handled"
            return

        state.ended_event.set()

        queued = 0
        skipped_heard = 0
        skipped_finished = 0
        skipped_disqualified = 0
        skipped_high_yes = 0
        skipped_no_quote = 0
        skipped_low_no_ask = 0
        skipped_already_queued = 0
        skipped_examples: list[str] = []

        if not args.end_no:
            state.status = "END detected; --no-end-no is set, no NO orders queued"
            record_transcript_event(
                args,
                "end",
                {
                    "queued_no": 0,
                    "disabled_by_no_end_no": True,
                    "typed_tail": typed_tail,
                    "source_label": source_label,
                },
            )
            safe_print("\nEND detected. No END=>NO orders queued because --no-end-no is set.")
            return

        # Refresh the passive finished set right before END so late joiners
        # don't buy NO against markets the book already treats as resolved YES.
        # This helper also takes state.lock, but it is an RLock.
        mark_finished_markets_from_quotes(state, args)

        end_candidates: list[dict] = []

        for original_index, market in enumerate(state.markets):
            key = market_key(market)

            if key in state.disqualified_market_keys:
                state.end_no_skipped_disqualified += 1
                skipped_disqualified += 1
                if len(skipped_examples) < 8:
                    skipped_examples.append(f"{market_label(market)} reason=disqualified")
                continue

            if not args.end_no_includes_heard and key in state.heard_market_keys:
                skipped_heard += 1
                continue

            if key in state.finished_market_keys:
                skipped_finished += 1
                continue

            if key in state.end_queued_market_keys:
                skipped_already_queued += 1
                continue

            should_queue, reason, quote, yes_guard = should_queue_end_no_order(args, market)

            if not should_queue:
                if reason == "finished_yes_high":
                    state.finished_market_keys.add(key)
                    state.end_no_marked_finished += 1
                    skipped_finished += 1
                elif reason in ("no_yes_quote", "no_no_ask_quote"):
                    state.end_no_skipped_no_quote += 1
                    skipped_no_quote += 1
                elif reason == "no_ask_too_low_for_end_no":
                    state.end_no_skipped_low_no_ask += 1
                    skipped_low_no_ask += 1
                else:
                    state.end_no_skipped_high_yes += 1
                    skipped_high_yes += 1

                if len(skipped_examples) < 8:
                    no_ask_text = cents_to_dollars(quote_side_ask_cents(quote, "no"))
                    skipped_examples.append(
                        f"{market_label(market)} {quote_prob_summary(quote, yes_guard)} "
                        f"NO_ASK={no_ask_text} reason={reason}"
                    )
                continue

            end_candidates.append(
                {
                    "index": original_index,
                    "market": market,
                    "key": key,
                    "quote": quote,
                    "yes_guard": yes_guard,
                    "no_ask": quote_side_ask_cents(quote, "no"),
                }
            )

        order_mode = getattr(args, "end_no_order", "no-ask-low")
        end_candidates.sort(key=lambda item: end_no_queue_sort_key(item, order_mode))

        queue_examples: list[str] = []
        for candidate in end_candidates:
            market = candidate["market"]
            key = candidate["key"]
            yes_guard = candidate["yes_guard"]
            quote = candidate["quote"]
            no_ask = candidate.get("no_ask")

            state.end_queued_market_keys.add(key)
            queued += 1
            state.end_no_queued += 1

            if len(queue_examples) < 8:
                queue_examples.append(
                    f"{market_label(market)} NO_ASK={cents_to_dollars(no_ask)} "
                    f"{quote_prob_summary(quote, yes_guard)}"
                )

            queue_order_task(
                state,
                OrderTask(
                    action="end_bulk_no",
                    market_key=key,
                    market_snapshot=market,
                    side="no",
                    trigger=(
                        f"{source_label} "
                        f"(priority={end_no_queue_order_heading(order_mode)}; "
                        f"NO_ASK={cents_to_dollars(no_ask)}; "
                        f"YES={cents_to_dollars(yes_guard)} below "
                        f"{args.end_no_skip_yes_at_cents}c safety gate; "
                        f"NO ask >= {args.end_no_min_no_ask_cents}c floor)"
                    ),
                    typed_tail=typed_tail,
                ),
            )

        state.status = (
            f"END detected: queued BUY NO for {queued}; "
            f"skipped_heard={skipped_heard}; skipped_finished={skipped_finished}; "
            f"skipped_disqualified={skipped_disqualified}; skipped_high_yes={skipped_high_yes}; "
            f"skipped_low_no_ask={skipped_low_no_ask}; "
            f"skipped_no_quote={skipped_no_quote}; skipped_already_queued={skipped_already_queued}"
        )

    record_transcript_event(
        args,
        "end",
        {
            "queued_no": queued,
            "skipped_heard": skipped_heard,
            "skipped_finished": skipped_finished,
            "skipped_disqualified": skipped_disqualified,
            "skipped_high_yes": skipped_high_yes,
            "skipped_low_no_ask": skipped_low_no_ask,
            "skipped_no_quote": skipped_no_quote,
            "skipped_already_queued": skipped_already_queued,
            "typed_tail": typed_tail,
            "source_label": source_label,
            "skip_examples": skipped_examples,
            "queue_examples": queue_examples,
            "end_no_order": getattr(args, "end_no_order", "no-ask-low"),
        },
    )

    safe_print(
        f"\n{source_label} detected -> queued BUY NO for {queued} remaining market(s) "
        f"ordered by {end_no_queue_order_heading(getattr(args, 'end_no_order', 'no-ask-low'))}. "
        f"Skipped heard={skipped_heard}, finished={skipped_finished}, disqualified={skipped_disqualified}, "
        f"YES>={args.end_no_skip_yes_at_cents}c={skipped_high_yes}, "
        f"NO ask<{args.end_no_min_no_ask_cents}c={skipped_low_no_ask}, "
        f"no quote={skipped_no_quote}."
    )
    if queue_examples:
        safe_print("END queue first examples: " + " | ".join(queue_examples))
    if skipped_examples:
        safe_print("END skip examples: " + " | ".join(skipped_examples))
    print_remaining_actionable_markets(state, args, heading="Remaining open markets after END")



def normalize_end_hotkey(spec: str | None) -> tuple[str | None, str]:
    """Return (single-character hotkey, human label) for the END hotkey.

    The default is Ctrl-E because it is quick, mnemonic-ish for END, and tmux
    passes it through normally. Accepted examples: ctrl-e, C-e, ^E, none.
    """
    if spec is None:
        spec = DEFAULT_END_HOTKEY
    raw = str(spec).strip()
    if not raw or raw.lower() in {"none", "off", "disabled", "disable", "0"}:
        return None, "disabled"

    lower = raw.lower().replace("_", "-").replace(" ", "")
    key: str | None = None

    for prefix in ("ctrl-", "control-", "c-"):
        if lower.startswith(prefix):
            key = raw[len(prefix) :].strip()
            break

    if key is None and raw.startswith("^") and len(raw) >= 2:
        key = raw[1:]

    if key is None:
        raise ValueError(
            f"unsupported END hotkey {raw!r}; use ctrl-e, C-e, ^E, or none"
        )

    aliases = {
        "space": " ",
        "spc": " ",
        "tab": "\t",
        "enter": "\r",
        "return": "\r",
        "esc": "[",
        "escape": "[",
        "backslash": "\\",
        "slash": "/",
        "minus": "-",
        "dash": "-",
        "underscore": "_",
        "leftbracket": "[",
        "rightbracket": "]",
        "lbracket": "[",
        "rbracket": "]",
    }
    key_norm = aliases.get(key.lower().replace("-", ""), key)
    if len(key_norm) != 1:
        raise ValueError(
            f"unsupported END hotkey {raw!r}; control key must name one character"
        )

    # Ctrl-letter/control-punctuation mapping. This follows the standard ASCII
    # control-code relationship: Ctrl-A == 0x01, Ctrl-E == 0x05, Ctrl-] == 0x1d.
    if key_norm == "?":
        ch = "\x7f"
    else:
        code = ord(key_norm.upper()) & 0x1F
        ch = chr(code)

    forbidden = {
        "\x00": "Ctrl-@/NUL",
        "\x03": "Ctrl-C",
        "\x04": "Ctrl-D",
        "\t": "Tab",
        "\r": "Enter",
        "\n": "Enter",
        "\x7f": "Backspace/Delete",
    }
    if ch in forbidden:
        raise ValueError(f"{forbidden[ch]} is not allowed as --end-hotkey")

    label_key = key_norm.upper() if key_norm.isalpha() else key_norm
    return ch, f"Ctrl-{label_key}"

def short_display(text: str, max_len: int = 48) -> str:
    """Return a compact one-line label for terminal prompts."""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)] + "…"


def read_keyboard_key(fd: int, first_byte: bytes) -> tuple[str, str]:
    """
    Decode one terminal keypress.

    Returns (kind, text):
      - ("char", ch) for normal single characters
      - ("up"/"down"/"left"/"right", seq) for arrow keys
      - ("escape", seq) for other escape sequences
    """
    if first_byte != b"\x1b":
        try:
            return "char", first_byte.decode("utf-8")
        except UnicodeDecodeError:
            return "unknown", ""

    seq = bytearray(first_byte)
    # Arrow keys usually arrive as ESC [ A/B/C/D. Read the already-buffered
    # bytes without blocking the listener if this was a plain Escape key.
    deadline = time.time() + 0.02
    while len(seq) < 8 and time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.002)
        if not readable:
            break
        try:
            seq.extend(os.read(fd, 1))
        except OSError:
            break

    mapping = {
        b"\x1b[A": "up",
        b"\x1b[B": "down",
        b"\x1b[C": "right",
        b"\x1b[D": "left",
    }
    return mapping.get(bytes(seq), "escape"), bytes(seq).decode("ascii", errors="ignore")


def autocomplete_prefix_text(recent_norm: str) -> str:
    """Return the current typed prefix used for autocomplete lookup."""
    return normalize_word(recent_norm)


def autocomplete_suggestions_for_prefix(
    state: SharedBroadcastState,
    args,
    prefix_text: str,
    *,
    limit: int | None = None,
) -> list[AutocompleteSuggestion]:
    """Return autocomplete candidates for the current interactive action.

    BUY mode keeps the historical behavior: show only unhandled/open candidates.
    SELL mode shows only the selected YES/NO leg that the local position cache
    says is owned. DISQUALIFY mode shows every not-yet-disqualified market.
    SELL and DISQUALIFY are committed only when Enter accepts one exact candidate;
    merely typing a word cannot submit an order or change eligibility.
    """
    if not getattr(args, "autocomplete", True):
        return []

    prefix = normalize_word(prefix_text)
    compact_prefix = normalize_compact_word(prefix_text)
    if len(compact_prefix) < args.autocomplete_min_chars:
        return []

    limit = args.autocomplete_limit if limit is None else limit
    scored: list[tuple[tuple[int, int, str], AutocompleteSuggestion]] = []
    seen_keys: set[str] = set()
    action, selected_side = state.trade_control_snapshot()
    selling = bool(getattr(args, "trade_controls", False) and action == "sell")
    disqualifying = bool(getattr(args, "trade_controls", False) and action == "disqualify")

    def alias_matches(alias: str) -> tuple[bool, bool]:
        alias_norm = normalize_word(alias)
        alias_compact = normalize_compact_word(alias)
        phrase_match = bool(prefix and alias_norm.startswith(prefix))
        compact_match = bool(compact_prefix and alias_compact.startswith(compact_prefix))
        return phrase_match or compact_match, compact_match and not phrase_match

    def sellable_position(ticker: str) -> tuple[bool, Decimal]:
        net = state.position_for_ticker(ticker)
        if selected_side == "yes":
            return net > 0, net
        return net < 0, net

    def add_aliases(alias_to_keys: dict[str, list[str]], *, repeated: bool) -> None:
        for alias, keys in alias_to_keys.items():
            matches, compact_only = alias_matches(alias)
            if not matches:
                continue

            alias_norm = normalize_word(alias)
            for key in keys:
                if key in seen_keys:
                    continue

                market = state.market_by_key.get(key)
                if market is None:
                    continue
                ticker = str(market.get("ticker") or "")
                net_position = state.position_for_ticker(ticker)

                if disqualifying:
                    # DISQUALIFY is an intentional exclusion command. Show any
                    # market not already excluded, even if it is heard/finished,
                    # so the user can make the state explicit without sending an order.
                    if key in state.disqualified_market_keys:
                        continue
                elif selling:
                    # A market may be heard/finished/queued but still hold a
                    # position that needs closing, so do not apply the normal
                    # BUY-mode visibility filters here.
                    can_sell, net_position = sellable_position(ticker)
                    if not can_sell:
                        continue
                elif (
                    key in state.disqualified_market_keys
                    or key in state.finished_market_keys
                    or key in state.heard_market_keys
                    or key in state.queued_market_keys
                    or key in state.end_queued_market_keys
                ):
                    continue

                try:
                    quote = latest_quote_for_market(args, market)
                    yes_prob = quote_yes_probability_cents(quote)
                except Exception:
                    yes_prob = None

                # Prefer aliases whose visible phrase starts with the typed
                # prefix, then shorter aliases, then alphabetic order.
                phrase_priority = 0 if alias_norm.startswith(prefix) else 1
                score = (phrase_priority, len(alias_norm), alias_norm)
                scored.append(
                    (
                        score,
                        AutocompleteSuggestion(
                            alias=alias_norm,
                            market_key=key,
                            market_word=market_word(market),
                            ticker=ticker,
                            repeated=repeated,
                            compact_match=compact_only,
                            yes_prob_cents=yes_prob,
                            net_position=net_position,
                        ),
                    )
                )
                seen_keys.add(key)

    with state.lock:
        add_aliases(state.word_to_keys, repeated=False)
        add_aliases(state.repeated_word_to_keys, repeated=True)

    scored.sort(key=lambda item: item[0])
    return [suggestion for _, suggestion in scored[:limit]]


def format_autocomplete_suggestion(suggestion: AutocompleteSuggestion, selected: bool) -> str:
    """Format one autocomplete option for the typing prompt."""
    label = short_display(suggestion.market_word, 36)
    prob = "" if suggestion.yes_prob_cents is None else f" {format_cents_value(suggestion.yes_prob_cents)}"
    repeat = " xN" if suggestion.repeated else ""
    position = ""
    if suggestion.net_position != 0:
        position = f" {format_net_position(suggestion.net_position)}"
    text = f"{label}{prob}{position}{repeat}"
    if selected:
        return color_text(f"[{text}]", "cyan", "bold")
    return color_text(text, "gray")


def print_typing_line(
    text: str,
    suggestions: list[AutocompleteSuggestion] | None = None,
    selected_index: int = 0,
) -> None:
    """Show the current typed buffer plus autocomplete candidates."""
    with PRINT_LOCK:
        sys.stdout.write("\r\033[K")
        line = f"typing: {text[-70:]}"
        if suggestions:
            selected_index = max(0, min(selected_index, len(suggestions) - 1))
            rendered = []
            for i, suggestion in enumerate(suggestions):
                rendered.append(format_autocomplete_suggestion(suggestion, i == selected_index))
            line += "  " + color_text("suggest:", "gray") + " " + "  ".join(rendered)
            line += " " + color_text("(↑/↓ choose, Enter select)", "gray")
        sys.stdout.write(line)
        sys.stdout.flush()


def print_autocomplete_line(
    state: SharedBroadcastState,
    args,
    recent_norm: str,
    selected_index: int = 0,
) -> list[AutocompleteSuggestion]:
    """Recompute and print autocomplete candidates for the current buffer."""
    suggestions = autocomplete_suggestions_for_prefix(state, args, recent_norm)
    if not args.debug_input:
        print_typing_line(recent_norm, suggestions, selected_index)
    return suggestions


def queue_selected_sell_position(
    *,
    state: SharedBroadcastState,
    args,
    suggestion: AutocompleteSuggestion,
    typed_tail: str,
) -> None:
    """Queue a close-the-selected-position task for exactly one Enter-selected market."""
    with state.lock:
        action, side = state.trade_control_action, state.trade_control_side
        if action != "sell":
            raise ValueError("selected-sell helper called outside SELL mode")
        market = state.market_by_key.get(suggestion.market_key)
        if market is None:
            safe_print(color_text("SELL SELECTION SKIPPED: selected market is no longer loaded", "yellow", "bold"))
            return
        net = state.position_for_ticker(str(market.get("ticker") or ""))
        owns_selected_leg = net > 0 if side == "yes" else net < 0
        if not owns_selected_leg:
            safe_print(
                color_text("SELL SELECTION SKIPPED", "yellow", "bold")
                + f": {market_word(market)} has no cached {side.upper()} position; press Ctrl-R to refresh positions."
            )
            return
        queue_order_task(
            state,
            OrderTask(
                action="manual_sell_position",
                market_key=suggestion.market_key,
                market_snapshot=market,
                side=side,
                trigger=f"trade controls autocomplete Enter {suggestion.alias}",
                typed_tail=typed_tail,
                trade_action="sell",
                # Internal name retained for the close-the-entire-selected-leg
                # behavior. This never means sell every market.
                sell_all=True,
                suppress_duplicate_guard=True,
            ),
        )
        summary = (
            f"Queued SELL {side.upper()} position for selected market: "
            f"{market_word(market)} (cached {format_net_position(net)})"
        )
        state.status = summary
        safe_print("\n" + color_text("MANUAL", "magenta", "bold") + ": " + summary)


def disqualify_selected_market(
    *,
    state: SharedBroadcastState,
    args,
    suggestion: AutocompleteSuggestion,
    typed_tail: str,
) -> None:
    """Commit an Enter-selected market to DISQUALIFY mode without placing an order."""
    changed, market = state.disqualify_market(suggestion.market_key)
    if market is None:
        safe_print(color_text("DISQUALIFY SKIPPED: selected market is no longer loaded", "yellow", "bold"))
        return

    payload = {
        "market_word": market_word(market),
        "ticker": market.get("ticker"),
        "alias": suggestion.alias,
        "typed_tail": typed_tail,
        "changed": changed,
    }
    record_transcript_event(args, "market_disqualified", payload)

    if changed:
        safe_print(
            "\n"
            + color_text("DISQUALIFIED", "yellow", "bold")
            + f": {market_word(market)} [{market.get('ticker')}] will receive no automatic BUY or Ctrl-E BUY NO order this run."
        )
    else:
        safe_print(
            "\n"
            + color_text("DISQUALIFY", "yellow", "bold")
            + f": {market_word(market)} was already excluded."
        )


def select_autocomplete_suggestion(
    *,
    state: SharedBroadcastState,
    args,
    suggestion: AutocompleteSuggestion,
    typed_tail: str,
) -> None:
    """Accept an autocomplete suggestion as if the matching word was typed."""
    record_transcript_event(
        args,
        "autocomplete_selected",
        {
            "alias": suggestion.alias,
            "market_word": suggestion.market_word,
            "ticker": suggestion.ticker,
            "repeated": suggestion.repeated,
            "typed_tail": typed_tail,
        },
    )
    if getattr(args, "trade_controls", False):
        action, _side = state.trade_control_snapshot()
        if action == "disqualify":
            # DISQUALIFY is Enter-confirmed just like SELL. It changes only
            # eligibility state; it never creates an order.
            disqualify_selected_market(
                state=state,
                args=args,
                suggestion=suggestion,
                typed_tail=typed_tail,
            )
            return
        if action == "sell":
            # SELL always targets the one highlighted candidate and requires
            # this explicit Enter selection. It never fires from typed-tail
            # matching and never fans out to other markets sharing an alias.
            queue_selected_sell_position(
                state=state,
                args=args,
                suggestion=suggestion,
                typed_tail=typed_tail,
            )
            return

    if suggestion.repeated:
        handle_repeated_word(
            state=state,
            args=args,
            word_key=suggestion.alias,
            typed_tail=typed_tail,
            compact_match=False,
        )
    else:
        handle_detected_word(
            state=state,
            args=args,
            word_key=suggestion.alias,
            typed_tail=typed_tail,
            compact_match=False,
        )


def arm_end_confirmation(*, state: SharedBroadcastState, args, typed_tail: str, source_label: str) -> None:
    """Arm the two-step END batch and make the safety state obvious on screen."""
    armed, message = state.arm_end_confirmation(source_label=source_label, typed_tail=typed_tail)
    if not armed:
        if message == "END confirmation already pending":
            # A second Ctrl-E / END is deliberately a cancel, never a confirmation.
            state.cancel_end_confirmation(reason="second END trigger")
            safe_print("\n" + color_text("END CONFIRMATION CANCELLED", "yellow", "bold") + ": no BUY NO orders were sent.")
        else:
            safe_print("\n" + color_text("END", "yellow", "bold") + f": {message}")
        return

    record_transcript_event(
        args,
        "end_confirmation_armed",
        {"typed_tail": typed_tail, "source_label": source_label},
    )
    _yes_count, no_count = state.trade_control_counts_snapshot()
    safe_print(
        "\n"
        + color_text("END CONFIRMATION ARMED", "yellow", "bold")
        + f": no orders sent. Current NO size is {no_count} per qualified market. Press "
        + color_text("Enter", "cyan", "bold")
        + " to BUY NO for all remaining qualified markets, or press Esc, any other key, or Ctrl-E again to cancel."
    )


def cancel_pending_end_confirmation(*, state: SharedBroadcastState, args, reason: str) -> bool:
    """Cancel a pending END confirmation and record it once."""
    if not state.cancel_end_confirmation(reason=reason):
        return False
    record_transcript_event(args, "end_confirmation_cancelled", {"reason": reason})
    safe_print("\n" + color_text("END CONFIRMATION CANCELLED", "yellow", "bold") + f": {reason}; no BUY NO orders were sent.")
    return True


def arm_exit_confirmation(*, state: SharedBroadcastState, args) -> None:
    """Make Ctrl-C an explicit two-step clean exit."""
    armed, message = state.arm_exit_confirmation()
    if not armed:
        safe_print("\n" + color_text("EXIT CONFIRMATION", "yellow", "bold") + f": {message}; press Enter to exit or Esc to continue.")
        return
    record_transcript_event(args, "exit_confirmation_armed", {})
    safe_print(
        "\n"
        + color_text("EXIT CONFIRMATION ARMED", "yellow", "bold")
        + ": trader is still running. Press "
        + color_text("Enter", "cyan", "bold")
        + " to exit cleanly, or "
        + color_text("Esc", "cyan", "bold")
        + " to continue."
    )


def cancel_pending_exit_confirmation(*, state: SharedBroadcastState, args, reason: str) -> bool:
    """Cancel a pending Ctrl-C exit confirmation and record it once."""
    if not state.cancel_exit_confirmation(reason=reason):
        return False
    record_transcript_event(args, "exit_confirmation_cancelled", {"reason": reason})
    safe_print("\n" + color_text("EXIT CONFIRMATION CANCELLED", "yellow", "bold") + f": {reason}; trader continues running.")
    return True


def input_worker(*, args, state: SharedBroadcastState, fd: int) -> None:
    """
    Read single keystrokes and maintain an explicit-confirmation autocomplete layer.

    With --trade-controls (the cmd_adv default), typing only filters and
    highlights candidates. Enter is the sole key that can select a market and
    submit a BUY/SELL action, apply DISQUALIFY, or confirm a pending Ctrl-E
    batch. Ctrl-E arms the END batch; Enter confirms it. Ctrl-C arms a separate
    exit confirmation; Enter exits and Esc cancels. Without --trade-controls, legacy typed-word
    auto-submission remains available for compatibility.

    Autocomplete behavior:
      - type a prefix and matching open markets are shown in gray
      - the highlighted suggestion is cyan/bold
      - Up/Down or Left/Right changes the highlighted suggestion
      - Enter accepts the highlighted suggestion
      - Tab intentionally does nothing, to avoid accidental order submission
    """
    raw_token = ""
    recent_norm = ""
    recent_compact = ""
    autocomplete_index = 0
    selected_market_key: str | None = None
    size_edit_side: str | None = None
    size_edit_buffer = ""

    def refresh_suggestions() -> list[AutocompleteSuggestion]:
        nonlocal autocomplete_index, selected_market_key
        suggestions = autocomplete_suggestions_for_prefix(state, args, recent_norm)
        if not suggestions:
            autocomplete_index = 0
            selected_market_key = None
            if not args.debug_input:
                print_typing_line(recent_norm, [], 0)
            return []

        if selected_market_key is not None:
            for i, suggestion in enumerate(suggestions):
                if suggestion.market_key == selected_market_key:
                    autocomplete_index = i
                    break
            else:
                autocomplete_index = 0
        else:
            autocomplete_index = 0

        autocomplete_index %= len(suggestions)
        selected_market_key = suggestions[autocomplete_index].market_key
        if not args.debug_input:
            print_typing_line(recent_norm, suggestions, autocomplete_index)
        return suggestions

    def clear_input_buffers() -> None:
        nonlocal raw_token, recent_norm, recent_compact, autocomplete_index, selected_market_key
        raw_token = ""
        recent_norm = ""
        recent_compact = ""
        autocomplete_index = 0
        selected_market_key = None
        if not args.debug_input:
            print_typing_line("")

    def print_size_edit_prompt() -> None:
        nonlocal size_edit_side, size_edit_buffer
        side = (size_edit_side or "").upper()
        yes_count, no_count = state.trade_control_counts_snapshot()
        current = yes_count if size_edit_side == "yes" else no_count
        with PRINT_LOCK:
            sys.stdout.write("\r\033[K")
            sys.stdout.write(
                color_text(f"EDIT {side} ORDER SIZE", "yellow", "bold")
                + f": current {current}; type whole number then Enter; Esc cancels > {size_edit_buffer}"
            )
            sys.stdout.flush()

    def begin_size_edit() -> None:
        nonlocal size_edit_side, size_edit_buffer
        _action, side = state.trade_control_snapshot()
        size_edit_side = side
        size_edit_buffer = ""
        print_size_edit_prompt()

    def cancel_size_edit(reason: str = "cancelled") -> bool:
        nonlocal size_edit_side, size_edit_buffer
        if size_edit_side is None:
            return False
        side = size_edit_side.upper()
        size_edit_side = None
        size_edit_buffer = ""
        safe_print("\n" + color_text("ORDER SIZE EDIT CANCELLED", "yellow", "bold") + f": {side} size unchanged ({reason}).")
        return True

    while not state.stop_event.is_set():
        readable, _, _ = select.select([fd], [], [], 0.05)
        if not readable:
            continue

        try:
            data = os.read(fd, 1)
        except OSError:
            break

        if not data:
            continue

        key_kind, key_text = read_keyboard_key(fd, data)

        recorder = transcript_recorder(args)
        if recorder is not None:
            if key_kind == "char":
                recorder.record_char(key_text)
            else:
                recorder.record_event("special_key", {"key": key_kind, "seq": key_text})

        if key_kind in ("up", "down", "left", "right"):
            if cancel_pending_exit_confirmation(state=state, args=args, reason=f"{key_kind} arrow"):
                clear_input_buffers()
                continue
            if cancel_pending_end_confirmation(state=state, args=args, reason=f"{key_kind} arrow"):
                clear_input_buffers()
            suggestions = autocomplete_suggestions_for_prefix(state, args, recent_norm)
            if suggestions:
                delta = -1 if key_kind in ("up", "left") else 1
                autocomplete_index = (autocomplete_index + delta) % len(suggestions)
                selected_market_key = suggestions[autocomplete_index].market_key
                if not args.debug_input:
                    print_typing_line(recent_norm, suggestions, autocomplete_index)
                else:
                    safe_print(
                        f"debug autocomplete: key={key_kind} "
                        f"selected={suggestions[autocomplete_index].market_word!r}"
                    )
            continue

        if key_kind == "escape":
            if cancel_pending_exit_confirmation(state=state, args=args, reason="Esc"):
                clear_input_buffers()
                continue
            if size_edit_side is not None:
                cancel_size_edit("Esc")
                continue
            if cancel_pending_end_confirmation(state=state, args=args, reason="Esc"):
                clear_input_buffers()
            continue

        if key_kind != "char":
            continue

        ch = key_text

        # Ctrl-C is terminal-intercepted (ISIG disabled below) so it never stops
        # the process immediately. The first press only arms a confirmation.
        if ch == "\x03":
            cancel_pending_end_confirmation(state=state, args=args, reason="Ctrl-C exit request")
            if size_edit_side is not None:
                cancel_size_edit("Ctrl-C exit request")
            clear_input_buffers()
            arm_exit_confirmation(state=state, args=args)
            continue

        # While exit confirmation is armed, Enter is the only key that exits.
        # Esc cancels explicitly; every other key also cancels but is consumed so
        # it cannot accidentally act on a market immediately afterward.
        with state.lock:
            exit_confirmation_pending = state.exit_confirmation_pending
        if exit_confirmation_pending:
            if ch in ("\r", "\n") and state.take_exit_confirmation():
                record_transcript_event(args, "exit_confirmation_confirmed", {})
                safe_print("\n" + color_text("EXIT CONFIRMED", "yellow", "bold") + ": stopping cleanly.")
                state.stop_event.set()
                break
            cancel_pending_exit_confirmation(state=state, args=args, reason="other key pressed")
            clear_input_buffers()
            continue

        # Ctrl-T size-edit mode is intentionally modal and Enter-confirmed.
        # While active, digits edit the selected side's order quantity; no market
        # selection or END confirmation can fire accidentally.
        if size_edit_side is not None:
            if ch in ("\r", "\n"):
                if not size_edit_buffer:
                    cancel_size_edit("empty input")
                    continue
                try:
                    new_count = int(size_edit_buffer)
                    if new_count <= 0:
                        raise ValueError("must be positive")
                except Exception:
                    safe_print("\n" + color_text("ORDER SIZE EDIT ERROR", "red", "bold") + ": enter a positive whole number.")
                    size_edit_buffer = ""
                    print_size_edit_prompt()
                    continue
                side = size_edit_side
                state.set_trade_control_count(side, new_count, args=args)
                size_edit_side = None
                size_edit_buffer = ""
                clear_input_buffers()
                print_trade_control_status(state, args, prefix="ORDER SIZE CHANGED")
                continue
            if ch == "\x08":  # Ctrl-H help (not backspace)
                cancel_size_edit("help")
                clear_input_buffers()
                print_key_help(state, args)
                continue
            if ch == "\x7f":  # Backspace / DEL only
                size_edit_buffer = size_edit_buffer[:-1]
                print_size_edit_prompt()
                continue
            if ch.isdigit():
                size_edit_buffer += ch
                print_size_edit_prompt()
                continue
            cancel_size_edit("non-number key pressed")
            clear_input_buffers()
            continue

        # Ctrl-H: in-session key help. ASCII BS (\x08) is help, not backspace.
        # Terminal Backspace is expected as DEL (\x7f).
        if ch == "\x08":
            cancel_pending_end_confirmation(state=state, args=args, reason="help")
            if size_edit_side is not None:
                cancel_size_edit("help")
            clear_input_buffers()
            print_key_help(state, args)
            continue

        # An armed END batch must never survive unrelated typing or mode changes.
        # Enter confirms it; Ctrl-E has its own arm logic below.
        if ch not in ("\r", "\n") and not (
            getattr(args, "end_hotkey_char", None) is not None and ch == args.end_hotkey_char
        ):
            cancel_pending_end_confirmation(state=state, args=args, reason="other key pressed")

        # Ctrl-D remains an exit without manual controls, but becomes
        # DISQUALIFY mode when --trade-controls is active. Ctrl-C was handled
        # above as an explicit Enter-confirmed exit.
        if ch == "\x04" and not getattr(args, "trade_controls", False):
            state.set_status("Exit requested")
            state.stop_event.set()
            break

        # Manual trade controls are intentionally explicit and disabled unless
        # --trade-controls is supplied.  Ctrl-S requires IXON to be disabled
        # below so terminals pass XOFF through as a normal keypress.
        if getattr(args, "trade_controls", False):
            if ch == "\x19":  # Ctrl-Y — side YES; from DISQUALIFY jumps to BUY YES
                action, _side = state.trade_control_snapshot()
                clear_input_buffers()
                if action == "disqualify":
                    switch_disqualify_side_to_buy(state, args, side_key="yes")
                else:
                    state.set_trade_control(side="yes")
                    print_trade_control_status(state, args, prefix="SIDE")
                continue
            if ch == "\x0e":  # Ctrl-N — side NO; from DISQUALIFY jumps to BUY NO
                action, _side = state.trade_control_snapshot()
                clear_input_buffers()
                if action == "disqualify":
                    switch_disqualify_side_to_buy(state, args, side_key="no")
                else:
                    state.set_trade_control(side="no")
                    print_trade_control_status(state, args, prefix="SIDE")
                continue
            if ch == "\x02":  # Ctrl-B
                state.set_trade_control(action="buy")
                clear_input_buffers()
                print_mode_entry_coach(state, args, action="buy")
                continue
            if ch == "\x13":  # Ctrl-S
                state.set_trade_control(action="sell")
                clear_input_buffers()
                print_mode_entry_coach(state, args, action="sell")
                continue
            if ch == "\x04":  # Ctrl-D
                state.set_trade_control(action="disqualify")
                clear_input_buffers()
                print_mode_entry_coach(state, args, action="disqualify")
                continue
            if ch == "\x14":  # Ctrl-T
                cancel_pending_end_confirmation(state=state, args=args, reason="size edit")
                clear_input_buffers()
                action, side = state.trade_control_snapshot()
                if action == "disqualify":
                    safe_print(
                        "\n"
                        + color_text("MODE COACH", "cyan", "bold")
                        + f": editing {side.upper()} size while in DISQUALIFY; sizes apply when you return to BUY/SELL."
                    )
                    safe_print("  Help: Ctrl-H")
                begin_size_edit()
                continue
            if ch == "\x12":  # Ctrl-R
                clear_input_buffers()
                refresh_trade_control_display(state, args, source="hotkey Ctrl-R")
                continue

        # Ctrl-E arms a two-step END batch. It never queues orders until Enter.
        if getattr(args, "end_hotkey_char", None) is not None and ch == args.end_hotkey_char:
            source_label = f"hotkey {getattr(args, 'end_hotkey_label', args.end_hotkey)}"
            arm_end_confirmation(
                state=state,
                args=args,
                typed_tail=recent_norm[-120:],
                source_label=source_label,
            )
            clear_input_buffers()
            continue

        # Tab intentionally does nothing. Enter is the only autocomplete
        # submit/select key, to avoid accidental orders from muscle memory.
        if ch == "\t":
            continue

        # Enter confirms a pending Ctrl-E batch before it can select an
        # autocomplete candidate. Confirmation rebuilds eligibility and quotes
        # at this moment, rather than relying on the earlier armed preview.
        if ch in ("\r", "\n"):
            confirmation = state.take_end_confirmation()
            if confirmation is not None:
                source_label, armed_typed_tail = confirmation
                record_transcript_event(
                    args,
                    "end_confirmation_confirmed",
                    {"typed_tail": armed_typed_tail, "source_label": source_label},
                )
                safe_print("\n" + color_text("END CONFIRMED", "yellow", "bold") + ": evaluating and submitting qualified BUY NO orders.")
                handle_end_token(
                    state=state,
                    args=args,
                    typed_tail=armed_typed_tail,
                    source_label=f"{source_label} + Enter confirmation",
                )
                clear_input_buffers()
                if args.exit_input_after_end:
                    break
                continue

            # Enter accepts the highlighted autocomplete candidate. If no candidate is
            # visible, leave the typed buffer alone rather than inserting whitespace.
            suggestions = autocomplete_suggestions_for_prefix(state, args, recent_norm)
            if suggestions:
                autocomplete_index %= len(suggestions)
                suggestion = suggestions[autocomplete_index]
                safe_print(
                    "\n"
                    + color_text("AUTOCOMPLETE", "cyan", "bold")
                    + ": "
                    + color_text(suggestion.market_word, "bold", "cyan")
                    + f" via {suggestion.alias!r}"
                )
                select_autocomplete_suggestion(
                    state=state,
                    args=args,
                    suggestion=suggestion,
                    typed_tail=recent_norm[-120:],
                )
                clear_input_buffers()
            continue

        # < toggles the open-market view between alpha and YES-lowest-first.
        # It is a command key, not typed input, so it does not affect autocomplete
        # or market word matching.
        if ch == "<":
            toggle_and_print_open_market_view(
                state,
                args,
                limit=(args.startup_word_limit if args.startup_word_limit > 0 else 1_000_000),
            )
            if not args.debug_input:
                refresh_suggestions()
            continue

        # Backspace/delete support. DEL (\x7f) only — Ctrl-H (\x08) is help above.
        if ch == "\x7f":
            raw_token = raw_token[:-1]
            recent_norm = recent_norm[:-1]
            recent_compact = recent_compact[:-1]
            selected_market_key = None
            if args.debug_input:
                safe_print(
                    f"debug input: key=<BACKSPACE> "
                    f"raw_token={raw_token!r} "
                    f"recent_norm={recent_norm[-120:]!r} "
                    f"recent_compact={recent_compact[-120:]!r}"
                )
            else:
                refresh_suggestions()
            continue

        with state.lock:
            state.typed_chars += 1

        if ch.isalnum():
            raw_token += ch
            recent_norm += ch.lower()
            recent_compact += ch.lower()
            if len(raw_token) > args.max_raw_token_chars:
                raw_token = raw_token[-args.max_raw_token_chars :]
            if len(recent_norm) > args.max_stream_chars:
                recent_norm = recent_norm[-args.max_stream_chars :]
            if len(recent_compact) > args.max_stream_chars:
                recent_compact = recent_compact[-args.max_stream_chars :]
        else:
            # Any separator ends the current raw token. For word detection, keep
            # one normalized space so multi-word market phrases can match and so
            # suffix matching respects word boundaries.
            raw_token = ""
            if recent_norm and not recent_norm.endswith(" "):
                recent_norm += " "
                if len(recent_norm) > args.max_stream_chars:
                    recent_norm = recent_norm[-args.max_stream_chars :].lstrip()
            selected_market_key = None
            if args.debug_input:
                safe_print(
                    f"debug input: key={ch!r} separator=True "
                    f"raw_token={raw_token!r} "
                    f"recent_norm={recent_norm[-120:]!r} "
                    f"recent_compact={recent_compact[-120:]!r}"
                )
            else:
                refresh_suggestions()
            continue

        typed_tail = recent_norm[-120:]

        if args.debug_input:
            safe_print(
                f"debug input: key={ch!r} separator=False "
                f"raw_token={raw_token!r} "
                f"recent_norm={typed_tail!r} "
                f"recent_compact={recent_compact[-120:]!r}"
            )
        else:
            refresh_suggestions()

        # With trade controls enabled, every manual action is Enter-confirmed.
        # Typing—including an exact full market word—only updates the highlighted
        # autocomplete candidate. This applies to BUY as well as SELL and
        # DISQUALIFY. Ctrl-E remains handled above and is unaffected.
        if getattr(args, "trade_controls", False):
            continue

        # Legacy behavior without --trade-controls: longest-first matching
        # auto-submits a typed market word. This remains available for direct
        # script users who deliberately do not enable manual controls.
        # Longest-first matching avoids firing a short word before a longer
        # overlapping word when both exist in the market list. First try the
        # readable phrase buffer, then compact aliases for multi-token phrases.
        with state.lock:
            word_keys = list(state.word_keys_by_length)
            compact_word_keys = list(state.compact_word_keys_by_length)
            repeated_word_keys = list(state.repeated_word_keys_by_length)
            compact_repeated_word_keys = list(state.compact_repeated_word_keys_by_length)

        matched = False
        for word_key in word_keys:
            if phrase_matches_tail(recent_norm, word_key):
                if args.debug_input:
                    with state.lock:
                        keys = list(state.word_to_keys.get(word_key, []))
                        market_names = [
                            market_word(state.market_by_key[k])
                            for k in keys
                            if k in state.market_by_key
                        ]
                    safe_print(
                        f"debug match: mode=phrase word_key={word_key!r} "
                        f"markets={market_names!r}"
                    )
                handle_detected_word(
                    state=state,
                    args=args,
                    word_key=word_key,
                    typed_tail=typed_tail,
                    compact_match=False,
                )
                matched = True
                break

        if not matched:
            for word_key in compact_word_keys:
                if recent_compact.endswith(word_key):
                    if args.debug_input:
                        with state.lock:
                            keys = list(state.compact_word_to_keys.get(word_key, []))
                            market_names = [
                                market_word(state.market_by_key[k])
                                for k in keys
                                if k in state.market_by_key
                            ]
                        safe_print(
                            f"debug match: mode=compact word_key={word_key!r} "
                            f"markets={market_names!r}"
                        )
                    handle_detected_word(
                        state=state,
                        args=args,
                        word_key=word_key,
                        typed_tail=typed_tail,
                        compact_match=True,
                    )
                    matched = True
                    break

        repeat_matched = False
        for word_key in repeated_word_keys:
            if phrase_matches_tail(recent_norm, word_key):
                if args.debug_input:
                    with state.lock:
                        keys = list(state.repeated_word_to_keys.get(word_key, []))
                        market_names = [
                            market_word(state.market_by_key[k])
                            for k in keys
                            if k in state.market_by_key
                        ]
                    safe_print(
                        f"debug match: mode=repeated phrase word_key={word_key!r} "
                        f"markets={market_names!r}"
                    )
                handle_repeated_word(
                    state=state,
                    args=args,
                    word_key=word_key,
                    typed_tail=typed_tail,
                    compact_match=False,
                )
                repeat_matched = True
                break

        if not repeat_matched:
            for word_key in compact_repeated_word_keys:
                if recent_compact.endswith(word_key):
                    if args.debug_input:
                        with state.lock:
                            keys = list(state.compact_repeated_word_to_keys.get(word_key, []))
                            market_names = [
                                market_word(state.market_by_key[k])
                                for k in keys
                                if k in state.market_by_key
                            ]
                        safe_print(
                            f"debug match: mode=repeated compact word_key={word_key!r} "
                            f"markets={market_names!r}"
                        )
                    handle_repeated_word(
                        state=state,
                        args=args,
                        word_key=word_key,
                        typed_tail=typed_tail,
                        compact_match=True,
                    )
                    repeat_matched = True
                    break

        if matched or repeat_matched:
            # A recognized market word consumes the current typed buffer. This
            # lets the next word be typed immediately without first typing a
            # separating space. Example: typing "TrumpIsrael" can trigger
            # Trump, reset, then trigger Israel as the next fresh token.
            clear_input_buffers()


def order_submission_preview(args, market: dict, side: str, task: OrderTask) -> str:
    """Return a one-line preview before an order is built/submitted."""
    word = market_word(market)
    ticker = market.get("ticker", "?")
    side_text = side.upper()
    trade_action = str(getattr(task, "trade_action", "buy")).upper()
    mode = "LIVE" if args.live else "DRY RUN"
    try:
        quote = latest_quote_for_market(args, market)
        prob = quote_prob_summary(quote, include_spread=True, args=args)
        ask = quote_side_ask_cents(quote, side)
        ask_text = cents_to_dollars(ask)
    except Exception:
        prob = "YES=-- NO≈--"
        ask_text = "--"

    exec_text = ""
    preview_mode = "simple" if str(getattr(task, "action", "")).startswith("manual_") else getattr(args, "execution_mode", "simple")
    if preview_mode == "ioc-ladder":
        exec_text = " ladder=" + ",".join(
            f"{count}@+{step}c" for count, step in ladder_plan_for_args(args, side=side)
        )

    return (
        f"{mode}: {word} [{ticker}] "
        f"{trade_action}{' ALL' if getattr(task, 'sell_all', False) else ''} {side_text} "
        f"count={'position' if getattr(task, 'sell_all', False) else effective_count_for_side(args, side)}{exec_text} ask={ask_text} {prob} "
        f"trigger={task.trigger}"
    )


def order_worker(
    *,
    worker_id: int,
    order_client: KalshiClient | None,
    args,
    state: SharedBroadcastState,
) -> None:
    """
    Consume queued order tasks.

    The worker asks state for the latest available market snapshot at execution
    time, so it benefits from the refresh thread's newest bid/ask data.
    """
    while not state.stop_event.is_set() or not state.order_queue.empty():
        try:
            task = state.order_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        state.mark_order_task_started()

        try:
            if task.action == "end_bulk_no":
                with state.lock:
                    disqualified_after_queue = task.market_key in state.disqualified_market_keys
                    if disqualified_after_queue:
                        state.end_no_skipped_disqualified += 1
                        state.end_queued_market_keys.discard(task.market_key)
                if disqualified_after_queue:
                    status = f"END NO SKIPPED: {market_word(task.market_snapshot)} [{task.market_snapshot.get('ticker')}] was disqualified before submission"
                    state.set_status(status)
                    safe_print(f"[{worker_id}] " + color_text("ORDER SKIPPED", "yellow", "bold") + f": {status}")
                    continue

            market = state.latest_market(task.market_key, task.market_snapshot)
            safe_print(
                f"[{worker_id}] "
                + color_text("ORDER SUBMITTING", "blue", "bold")
                + ": "
                + order_submission_preview(args, market, task.side, task)
            )
            trade_action = str(getattr(task, "trade_action", "buy")).lower()
            count_override = None
            if getattr(task, "sell_all", False):
                try:
                    # Always query the authoritative position immediately before
                    # a close.  Local fill accounting is for display; this makes
                    # SELL-position close correct after restart or a prior partial fill.
                    authoritative_position = fetch_position_for_market(args, market)
                    state.merge_positions(
                        {str(market.get("ticker") or "").upper(): authoritative_position},
                        note=datetime.now().strftime("%H:%M:%S") + " before SELL position",
                    )
                    if task.side == "yes":
                        count_override = max(Decimal("0"), authoritative_position)
                    else:
                        count_override = max(Decimal("0"), -authoritative_position)
                    if count_override <= 0:
                        status = (
                            f"SELL POSITION SKIPPED: {market_word(market)} | {market.get('ticker')} "
                            f"has no {task.side.upper()} position (net={format_net_position(authoritative_position)})"
                        )
                        state.record_order_result(True, status)
                        safe_print(f"[{worker_id}] " + color_text("ORDER STATUS", "yellow", "bold") + f": {status}")
                        continue
                except Exception as exc:
                    status = f"SELL POSITION ERROR: {market_word(market)} | {market.get('ticker')}: {exc}"
                    state.record_order_result(False, status)
                    safe_print(f"[{worker_id}] " + color_text("ORDER STATUS", "red", "bold") + f": {status}")
                    continue

            ok, status, details = place_order_for_market(
                client=order_client,
                args=args,
                market=market,
                side=task.side,
                action=task.action,
                trigger=task.trigger,
                typed_tail=task.typed_tail,
                trade_action=trade_action,
                count_override=count_override,
            )
            if getattr(args, "trade_controls", False) and ok:
                filled_count = details.get("filled_count") if isinstance(details, dict) else None
                if filled_count is not None:
                    net = state.apply_manual_fill(
                        ticker=str(market.get("ticker") or ""),
                        side=task.side,
                        trade_action=trade_action,
                        filled_count=filled_count,
                    )
                    status += f" | position now {format_net_position(net)}"
            state.record_order_result(ok, status)
            status_style = "green" if ok else "red"
            safe_print(
                f"[{worker_id}] "
                + color_text("ORDER STATUS", status_style, "bold")
                + f": {status}"
            )
        finally:
            state.order_queue.task_done()
            # File I/O is intentionally deferred during order bursts. Flush only
            # after this order has received its API response/status and the
            # order side is truly idle. With multiple workers, Queue.empty()
            # alone is not enough because another worker may still be in-flight.
            if state.mark_order_task_finished_and_idle():
                flush_deferred_logs(args, reason="order_side_idle_after_response")


def status_worker(*, args, state: SharedBroadcastState) -> None:
    """Print compact status periodically without taking over the terminal."""
    last_line = None

    while not state.stop_event.is_set():
        mark_finished_markets_from_quotes(state, args)
        snap = state.snapshot_for_status()
        refreshing = " refreshing" if snap["refresh_in_progress"] else ""
        ended = " ENDED" if snap["ended"] else ""
        line = (
            f"status:{ended}{refreshing} | markets={snap['n_markets']} words={snap['n_words']} "
            f"heard={snap['heard']} finished={snap['finished']} disqualified={snap['disqualified']} "
            f"(Y={snap['auto_finished_yes']} N={snap['auto_finished_no']}) "
            f"spikes={snap['spikes_detected']} queue={snap['queued']} "
            f"ok={snap['orders_ok']} err={snap['orders_error']} "
            + (
                (
                    f"control={'DISQUALIFY' if snap['trade_control_action'] == 'disqualify' else snap['trade_control_action'].upper() + ' ' + snap['trade_control_side'].upper()} "
                    f"sizeY={snap['trade_control_yes_count']} sizeN={snap['trade_control_no_count']} "
                )
                if getattr(args, "trade_controls", False) else ""
            )
            + f"updated={snap['last_refresh_text']} | {snap['status']}"
        )

        if line != last_line:
            safe_print(line)
            last_line = line

        time.sleep(args.status_seconds)


# -----------------------------------------------------------------------------
# Main runner / CLI
# -----------------------------------------------------------------------------


def print_startup_summary(args, markets: list[dict], state: SharedBroadcastState) -> None:
    """Print a friendly startup summary and a preview of recognized words."""
    mode = "LIVE" if args.live else "DRY RUN"
    safe_print("=" * 88)
    safe_print("Kalshi broadcast word trader")
    safe_print(f"Mode: {mode}")
    safe_print(f"Kalshi env: {args.kalshi_env.upper()} | REST: {args.api_host} | WS: {args.ws_url}")
    safe_print(
        f"Local API throttle: reads <= {args.read_rate_limit:g}/s, "
        f"writes <= {args.write_rate_limit:g}/s; 429 retries={args.max_429_retries}"
    )
    safe_print(f"Markets: {len(markets)}")
    fetch_method = getattr(args, "_market_fetch_method", None)
    if fetch_method:
        safe_print(
            f"Market fetch: {fetch_method}; pages={getattr(args, '_market_fetch_pages', '?')} "
            f"total_before_filter={getattr(args, '_market_fetch_total', '?')}"
        )
    if getattr(args, "_market_fetch_rest_error", None):
        safe_print(f"Market fetch REST fallback reason: {args._market_fetch_rest_error}")
    market_filter_status = getattr(args, "_market_filter_status", "none")
    safe_print(f"Market filter: {market_filter_status}")
    safe_print(f"REST refresh: {args.refresh_seconds:g}s")
    safe_print(
        "Orderbook WS: "
        + (
            f"enabled ({len(getattr(args, '_ws_market_tickers', []))} markets; "
            f"update_queue_size={args.ws_update_queue_size})"
            if args.ws_orderbook
            else "disabled"
        )
    )
    if args.ws_orderbook and args.ws_snapshot_max_age_ms > 0:
        safe_print(
            "WS cache freshness: request snapshot when a ticker exceeds "
            f"{args.ws_snapshot_max_age_ms}ms; stale order wait up to "
            f"{args.ws_fresh_order_wait_ms}ms"
        )
    elif args.ws_orderbook:
        safe_print("WS cache freshness: background snapshot repair disabled")
    safe_print(f"On detected word: BUY {args.mention_side.upper()}")
    safe_print(f"Order submit API: {args.order_submit_api} ({'/portfolio/events/orders' if args.order_submit_api == 'v2' else '/portfolio/orders legacy'})")
    safe_print("Order sizes: " + count_summary_for_args(args))
    safe_print("Simple-mode slippage: " + slippage_summary_for_args(args))
    if args.execution_mode == "ioc-ladder":
        yes_plan_text = ",".join(str(count) for count, _step in ladder_plan_for_args(args, side="yes"))
        no_plan_text = ",".join(str(count) for count, _step in ladder_plan_for_args(args, side="no"))
        if yes_plan_text == no_plan_text:
            plan_text = f"counts={yes_plan_text}"
        else:
            plan_text = f"YES counts={yes_plan_text}; NO counts={no_plan_text}"
        safe_print(
            "Execution: IOC ladder | "
            f"weights={args.ladder_slices} -> {plan_text}"
            + f" | price steps={args.ladder_price_steps}c | tif={args.ladder_time_in_force}"
        )
    else:
        safe_print("Execution: simple one-order mode")
    safe_print(
        "On END: "
        + (
            (
                "BUY NO only on un-heard markets with YES < "
                f"{args.end_no_skip_yes_at_cents}c and NO ask >= "
                f"{args.end_no_min_no_ask_cents}c; queue order "
                f"{end_no_queue_order_heading(args.end_no_order)}"
            )
            if args.end_no and not args.end_no_includes_heard
            else (
                "BUY NO on all markets, including heard, but still skip "
                f"YES >= {args.end_no_skip_yes_at_cents}c and NO ask < "
                f"{args.end_no_min_no_ask_cents}c; queue order "
                f"{end_no_queue_order_heading(args.end_no_order)}"
            )
            if args.end_no
            else "disabled"
        )
    )
    finish_bits: list[str] = []
    if args.auto_finish_yes_at_cents is not None:
        finish_bits.append(f"YES >= {args.auto_finish_yes_at_cents}c")
    if args.auto_finish_no_at_cents is not None:
        finish_bits.append(f"YES <= {args.auto_finish_no_at_cents}c (NO finished)")
    safe_print("Auto-finish markers: " + (", ".join(finish_bits) if finish_bits else "disabled"))
    safe_print(
        "Spike detect: "
        + (
            f"enabled; YES ask move >= {args.spike_move_cents}c within "
            f"{args.spike_window_seconds:g}s, cooldown {args.spike_cooldown_seconds:g}s, "
            f"wide-spread warning above {args.spike_max_spread_cents}c"
            if args.spike_detect and args.ws_orderbook
            else "disabled"
        )
    )
    safe_print(f"Wide spread display: red when spread > {args.display_wide_spread_cents}c")
    safe_print(
        "END quote requirement: "
        + ("require YES quote" if args.end_no_require_yes_quote else "allow missing YES quote")
    )
    if args.end_no_min_no_ask_cents is not None:
        safe_print(
            f"END NO floor: require NO ask >= {args.end_no_min_no_ask_cents}c "
            f"(default 4c means > 3c)"
        )
    safe_print(f"Log file: {args.log_file or '(disabled)'}")
    safe_print("Log writes: " + ("deferred until order side is idle after responses" if args.defer_log_writes else "immediate"))
    safe_print(f"Transcript: {args.transcript_file or '(disabled; use --transcript to enable)'}")
    safe_print(
        "Autocomplete: "
        + (
            f"enabled after {args.autocomplete_min_chars} char(s); "
            "↑/↓ choose, Enter select"
            if args.autocomplete
            else "disabled"
        )
    )
    end_hotkey_label = getattr(args, "end_hotkey_label", "disabled")
    if end_hotkey_label == "disabled":
        end_instruction = "END hotkey is disabled. Press < to toggle market view sort. Ctrl-C then Enter exits; Esc cancels."
    else:
        end_instruction = (
            f"Type words as you hear them. Press {end_hotkey_label}, then Enter, to submit the qualified BUY NO batch. "
            "Typing END has no special meaning. Press < to toggle market view sort. Ctrl-C then Enter exits; Esc cancels."
        )
    safe_print(end_instruction)
    if getattr(args, "trade_controls", False):
        safe_print(
            color_text("Trade controls enabled", "magenta", "bold")
            + ": Ctrl-B/S/D = mode (BUY/SELL/DISQUALIFY); Ctrl-Y/N = side (YES/NO; from DISQUALIFY → BUY). "
            + "Ctrl-T size, Ctrl-R refresh, Ctrl-H help. Type to highlight; Enter confirms. "
            + "Ctrl-E arms END; Enter confirms its BUY NO batch."
        )
        print_trade_control_status(state, args, prefix="STARTING MODE")

    with state.lock:
        trigger_aliases = sorted(state.word_to_keys.keys())
        repeated_aliases = sorted(
            f"{alias} x{state.repeated_required_by_key.get(key, '?')}"
            for alias, keys in state.repeated_word_to_keys.items()
            for key in keys
        )
        trigger_aliases = sorted(trigger_aliases + repeated_aliases)

    safe_print("Trigger words / aliases, alpha:")
    if trigger_aliases:
        safe_print("  " + ", ".join(trigger_aliases))
    else:
        safe_print("  (none)")

    startup_lines = remaining_actionable_market_prob_lines(
        state,
        args,
        limit=(args.startup_word_limit if args.startup_word_limit > 0 else 1_000_000),
        sort_mode="alpha",
    )
    if startup_lines:
        safe_print(color_text("Open markets, alpha:", "yellow", "bold"))
        safe_print("\n".join(startup_lines))
    safe_print("=" * 88)


def wait_for_ws_snapshots(manager, market_tickers: list[str], timeout: float) -> int:
    """Wait briefly for initial orderbook snapshots and return ready count."""
    if timeout <= 0:
        return 0

    deadline = time.time() + timeout
    ready = 0

    while time.time() < deadline:
        ready = 0
        for ticker in market_tickers:
            view = manager.get_view(ticker)
            if view is not None and view.ready:
                ready += 1

        if ready >= len(market_tickers):
            return ready

        manager.wait_for_update(timeout=0.1)

    return ready


def run_demo_rate_limit_test(
    *,
    args,
    markets: list[dict],
    state: SharedBroadcastState,
    order_client: KalshiClient | None,
) -> int:
    """
    Submit a bounded number of demo orders through the normal order path.

    This mode is intentionally demo-only and sequential. It exercises exactly the
    same write limiter, 429 retry/backoff, payload build, order logging, and API
    pass/fail accounting used during an interactive run.
    """
    if order_client is None:
        raise RuntimeError("--demo-rate-limit-test requires a live demo order client")
    if not markets:
        raise RuntimeError("No markets available for demo rate-limit test")

    total = args.demo_rate_limit_test
    safe_print("=" * 88)
    safe_print("Kalshi DEMO rate-limit test")
    safe_print(f"REST: {args.api_host}")
    safe_print(f"WS:   {args.ws_url if args.ws_orderbook else '(disabled)'}")
    safe_print(
        f"Submitting {total} demo order call(s), count={effective_count_for_side(args, args.mention_side)}, side={args.mention_side.upper()}, "
        f"write throttle={args.write_rate_limit:g}/s, 429 retries={args.max_429_retries}"
    )
    safe_print(f"API log: {args.api_log_file or '(disabled)'}")
    safe_print("Log writes: " + ("deferred until test completes" if args.defer_log_writes else "immediate"))
    safe_print("=" * 88)

    for i in range(total):
        market = markets[i % len(markets)]
        ok, status, _details = place_order_for_market(
            client=order_client,
            args=args,
            market=market,
            side=args.mention_side,
            action="demo_rate_limit_test",
            trigger=f"demo_rate_limit_test#{i + 1}",
            typed_tail=None,
        )
        state.record_order_result(ok, status)
        style = "green" if ok else "red"
        safe_print(
            f"[{i + 1:>4}/{total:<4}] "
            + color_text("PASS" if ok else "FAIL", style, "bold")
            + f" {status}"
        )

    snap = state.snapshot_for_status()
    api_failures = current_api_stats(args).total_failures() if current_api_stats(args) else 0
    passed = snap["orders_error"] == 0 and api_failures == 0

    flush_deferred_logs(args, reason="demo_rate_limit_test_done")

    safe_print("\nDemo rate-limit test summary:")
    safe_print(f"  orders: success={snap['orders_ok']} fail={snap['orders_error']}")
    print_api_summary(args)
    safe_print("  result: " + color_text("PASS" if passed else "FAIL", "green" if passed else "red", "bold"))
    return 0 if passed else 1


def run(args) -> int:
    """Create clients, start threads, and coordinate shutdown."""
    setup_runtime_api_helpers(args)

    auth_needed = args.auth_check or (not args.file) or args.live or (args.ws_orderbook and not args.dump_words)
    if auth_needed:
        resolve_auth_settings(args)
        print_auth_summary(args)
        should_auth_check = args.auth_check or (args.ws_orderbook and args.auth_check_before_ws)
        if should_auth_check:
            if not perform_rest_auth_check(args):
                raise SystemExit(
                    "Auth check failed before opening WebSocket. Check that the API key id "
                    "and private key file belong to the same Kalshi environment. To bypass this "
                    "diagnostic, pass --no-auth-check-before-ws."
                )
        if args.auth_check:
            return 0

    refresh_client = None
    order_client = None

    if not args.file:
        refresh_client = make_client(args._auth_api_key_id, args._auth_private_key_file, rest_host=args.api_host)

    if args.live:
        order_client = make_client(args._auth_api_key_id, args._auth_private_key_file, rest_host=args.api_host)

    markets = load_current_markets(refresh_client, args)
    if getattr(args, "_full_name_parsed", None):
        safe_print(
            f"Parsed --full-name {args._full_name_parsed}: "
            f"series={args.ticker} event={args.market_event}"
        )
    state = SharedBroadcastState(markets, args=args)

    if args.dump_words:
        for market in sorted(markets, key=lambda m: market_word(m).lower()):
            aliases_list = market_trigger_aliases(market)
            repeated_list = repeated_trigger_preview(market)
            aliases = ", ".join(aliases_list + repeated_list)
            print(
                f"{market_word(market):<40} "
                f"{market.get('ticker', ''):<36} "
                f"triggers=[{aliases}]"
            )
        return 0

    orderbook_manager = None
    args._orderbook_manager = None
    args._ws_market_tickers = []
    args._ws_freshness_stats = WebSocketFreshnessStats()

    if args.ws_orderbook:
        if SeriesOrderbookManager is None:
            raise SystemExit(
                "--ws-orderbook requires the kx_orderbooks package. "
                "From kalshi_multiplex_orderbooks, run: python3 -m pip install -e .\n"
                f"Import error was: {KX_ORDERBOOK_IMPORT_ERROR}"
            )

        ws_tickers = sorted({str(m.get("ticker", "")).upper() for m in markets if m.get("ticker")})
        if not ws_tickers:
            raise SystemExit("No market tickers available for WebSocket subscription")

        args._ws_market_tickers = ws_tickers
        if load_ws_private_key is None:
            raise SystemExit(
                "--ws-orderbook requires kx_orderbooks.auth.load_private_key. "
                f"Import error was: {KX_ORDERBOOK_IMPORT_ERROR}"
            )
        ws_private_key = load_ws_private_key(args._auth_private_key_file)
        orderbook_manager = SeriesOrderbookManager.from_market_tickers(
            ws_tickers,
            ws_url=args.ws_url,
            api_key_id=args._auth_api_key_id,
            private_key=ws_private_key,
            log_raw=args.ws_raw,
            update_queue_size=args.ws_update_queue_size,
        )
        args._orderbook_manager = orderbook_manager

        safe_print(f"Starting multiplexed orderbook WebSocket for {len(ws_tickers)} markets...")
        orderbook_manager.start()

        ready = wait_for_ws_snapshots(orderbook_manager, ws_tickers, args.ws_wait_seconds)
        safe_print(
            f"WebSocket snapshots ready: {ready}/{len(ws_tickers)} "
            f"after up to {args.ws_wait_seconds:g}s"
        )
        if hasattr(orderbook_manager, "update_queue_stats"):
            safe_print(f"WebSocket update queue: {orderbook_manager.update_queue_stats()}")

    if args.demo_rate_limit_test:
        try:
            return run_demo_rate_limit_test(
                args=args,
                markets=markets,
                state=state,
                order_client=order_client,
            )
        finally:
            if orderbook_manager is not None:
                if hasattr(orderbook_manager, "update_queue_stats"):
                    safe_print(f"Final WebSocket update queue stats: {orderbook_manager.update_queue_stats()}")
                orderbook_manager.stop()

    print_startup_summary(args, markets, state)

    if args.trade_controls:
        # Seed the display before the first keystroke.  A later SELL position still
        # re-queries the one market immediately before closing it.
        refresh_trade_control_display(state, args, source="startup")

    if not sys.stdin.isatty():
        raise SystemExit("This script needs an interactive terminal for no-Enter typing.")

    fd = sys.stdin.fileno()
    old_term_attrs = termios.tcgetattr(fd)

    args._transcript_recorder = TranscriptRecorder(args.transcript_file)
    if args._transcript_recorder.enabled():
        args._transcript_recorder.write_header(args, markets)

    threads: list[threading.Thread] = []

    try:
        # cbreak gives character-at-a-time input. Disable ISIG so Ctrl-C is
        # delivered to input_worker as a character instead of terminating via
        # SIGINT; that allows the explicit Enter/Esc exit confirmation. Disable
        # software flow control so Ctrl-S reaches manual trade controls.
        tty.setcbreak(fd)
        current_attrs = termios.tcgetattr(fd)
        current_attrs[3] &= ~termios.ISIG
        if args.trade_controls:
            current_attrs[0] &= ~termios.IXON
        termios.tcsetattr(fd, termios.TCSADRAIN, current_attrs)

        if (
            orderbook_manager is not None
            and args.ws_snapshot_max_age_ms > 0
        ):
            threads.append(
                threading.Thread(
                    target=ws_snapshot_freshness_worker,
                    kwargs={
                        "args": args,
                        "state": state,
                        "manager": orderbook_manager,
                        "market_tickers": list(args._ws_market_tickers),
                    },
                    name="ws-snapshot-freshness",
                    daemon=True,
                )
            )

        threads.append(
            threading.Thread(
                target=market_refresh_worker,
                kwargs={"refresh_client": refresh_client, "args": args, "state": state},
                name="market-refresh",
                daemon=True,
            )
        )

        threads.append(
            threading.Thread(
                target=input_worker,
                kwargs={"args": args, "state": state, "fd": fd},
                name="keyboard-input",
                daemon=True,
            )
        )

        for i in range(args.order_workers):
            threads.append(
                threading.Thread(
                    target=order_worker,
                    kwargs={
                        "worker_id": i + 1,
                        "order_client": order_client,
                        "args": args,
                        "state": state,
                    },
                    name=f"order-worker-{i + 1}",
                    daemon=True,
                )
            )

        if args.spike_detect and orderbook_manager is not None:
            threads.append(
                threading.Thread(
                    target=spike_worker,
                    kwargs={"args": args, "state": state},
                    name="yes-spike-detector",
                    daemon=True,
                )
            )

        threads.append(
            threading.Thread(
                target=status_worker,
                kwargs={"args": args, "state": state},
                name="status-printer",
                daemon=True,
            )
        )

        for thread in threads:
            thread.start()

        while not state.stop_event.is_set():
            if state.ended_event.is_set() and not args.stay_open_after_end:
                # Let all queued orders finish before exiting automatically.
                state.order_queue.join()
                flush_deferred_logs(args, reason="ended_order_queue_joined")
                state.stop_event.set()
                break
            time.sleep(0.1)

    except KeyboardInterrupt:
        safe_print("\nCtrl-C received. Stopping...")
        state.stop_event.set()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term_attrs)
        state.stop_event.set()
        state.refresh_now_event.set()

        # Give workers a small chance to flush cleanly. Daemon threads make sure
        # a stuck API call cannot keep the process alive forever.
        for thread in threads:
            thread.join(timeout=1.0)

        record_transcript_event(
            args,
            "shutdown",
            {
                "reason": "finally",
                "queue_empty": state.order_queue.empty(),
                "ended": state.ended_event.is_set(),
            },
        )

        # Final safety flush for Ctrl-C, errors, or daemon worker shutdown paths.
        flush_deferred_logs(args, reason="shutdown")

        if orderbook_manager is not None:
            if hasattr(orderbook_manager, "update_queue_stats"):
                safe_print(f"Final WebSocket update queue stats: {orderbook_manager.update_queue_stats()}")
            orderbook_manager.stop()

    snap = state.snapshot_for_status()
    safe_print("\nFinal summary:")
    safe_print(
        f"heard={snap['heard']} finished={snap['finished']} "
        f"auto_yes={snap['auto_finished_yes']} auto_no={snap['auto_finished_no']} "
        f"spikes={snap['spikes_detected']} "
        f"detected_yes={snap['detected_yes']} "
        f"end_no_queued={snap['end_no_queued']} "
        f"end_skipped_high_yes={snap['end_no_skipped_high_yes']} "
        f"end_skipped_no_quote={snap['end_no_skipped_no_quote']} "
        f"ok={snap['orders_ok']} err={snap['orders_error']}"
    )
    safe_print(f"Last status: {snap['last_order_status']}")
    print_api_summary(args)

    recorder = transcript_recorder(args)
    if recorder is not None and recorder.enabled():
        safe_print(f"Transcript: {recorder.transcript_file}")
        safe_print(f"Transcript events: {recorder.jsonl_file}")
        recorder.close()

    return 0


def parse_args():
    """Parse command line arguments."""
    p = argparse.ArgumentParser(
        description="Type-as-you-hear Kalshi mention market trader."
    )

    env = p.add_mutually_exclusive_group(required=True)
    env.add_argument(
        "--demo",
        dest="kalshi_env",
        action="store_const",
        const="demo",
        help="Use Kalshi demo REST/WS endpoints. Required choice: --demo or --prod.",
    )
    env.add_argument(
        "--prod",
        dest="kalshi_env",
        action="store_const",
        const="prod",
        help="Use Kalshi production REST/WS endpoints. Required choice: --demo or --prod.",
    )

    p.add_argument(
        "--demo-host-style",
        choices=["external", "direct"],
        default="external",
        help=(
            "Demo endpoint pair to use. external = external-api.demo/external-api-ws.demo; "
            "direct = demo-api REST and demo-api WS. Default: external"
        ),
    )

    p.add_argument(
        "--api-host",
        default=None,
        help="Override the REST Trade API host selected by --demo/--prod.",
    )

    p.add_argument(
        "ticker",
        nargs="?",
        help="Series/event ticker, e.g., KXVANCEMENTION-26MAY15",
    )

    p.add_argument(
        "--full-name",
        "--event-name",
        "--paste-name",
        dest="full_name",
        help=(
            "Paste-friendly browser/event name in SERIES-EVENT form. Example: "
            "--full-name kxnbamention-26jun05nyksas is equivalent to "
            "KXNBAMENTION --market-event 26JUN05NYKSAS."
        ),
    )

    p.add_argument(
        "--file",
        help="Read dumped get_markets.py output from file instead of fetching.",
    )

    p.add_argument(
        "--market-event",
        "--event",
        "--game",
        dest="market_event",
        help=(
            "Filter a broad series to one event/game code. Example: "
            "KXMLBMENTION --market-event 26MAY16NYYNYM keeps only "
            "KXMLBMENTION-26MAY16NYYNYM-* markets."
        ),
    )

    p.add_argument(
        "--market-prefix",
        action="append",
        default=[],
        help=(
            "Filter loaded markets to tickers starting with this prefix. "
            "Can be repeated. Example: --market-prefix KXMLBMENTION-26MAY16NYYNYM-"
        ),
    )

    p.add_argument(
        "--api-key-id-env",
        default=None,
        help=(
            "Environment variable containing the Kalshi API key id. "
            f"Defaults: demo={DEMO_API_KEY_ID_ENV}, prod={PROD_API_KEY_ID_ENV}; "
            f"legacy prod fallback={LEGACY_PROD_API_KEY_ID_ENV}."
        ),
    )

    p.add_argument(
        "--private-key-file-env",
        default=None,
        help=(
            "Environment variable containing the private-key PEM file path. "
            f"Defaults: demo={DEMO_PRIVATE_KEY_FILE_ENV}, prod={PROD_PRIVATE_KEY_FILE_ENV}; "
            f"legacy prod fallback={LEGACY_PROD_PRIVATE_KEY_FILE_ENV}."
        ),
    )

    p.add_argument(
        "--private-key-file",
        default=None,
        help=(
            "Private key PEM file path. Overrides the env-var file path. "
            "For demo, prefer KALSHI_DEMO_PRIVATE_KEY_FILE. "
            "For prod, prefer KALSHI_PROD_PRIVATE_KEY_FILE."
        ),
    )

    p.add_argument(
        "--count",
        type=int,
        default=1,
        help="Fallback number of contracts to buy per detected/END order. Default: 1",
    )

    p.add_argument(
        "--count-yes",
        type=int,
        default=None,
        help=(
            "Override order size for BUY YES triggers. If omitted, --count is used. "
            "Example: --count 50 --count-yes 300 makes heard-word YES orders 300 contracts."
        ),
    )

    p.add_argument(
        "--count-no",
        type=int,
        default=None,
        help=(
            "Override order size for BUY NO triggers, including Ctrl-E/END bulk-NO orders. "
            "If omitted, --count is used. Example: --count-yes 300 --count-no 50."
        ),
    )

    p.add_argument(
        "--slippage-cents",
        "--slippage",
        type=int,
        default=1,
        help=(
            "Fallback slippage in cents for simple limit IOC orders. "
            "Overridden per side by --slippage-yes/--slippage-no. Default: 1"
        ),
    )

    p.add_argument(
        "--slippage-yes",
        dest="slippage_yes_cents",
        type=int,
        default=None,
        help=(
            "Override simple-mode BUY YES slippage in cents. If omitted, --slippage is used. "
            "Example: --slippage 3 --slippage-yes 2 --slippage-no 5"
        ),
    )

    p.add_argument(
        "--slippage-no",
        dest="slippage_no_cents",
        type=int,
        default=None,
        help=(
            "Override simple-mode BUY NO slippage in cents, including Ctrl-E/END orders. "
            "If omitted, --slippage is used. IOC ladder uses --ladder-price-steps instead."
        ),
    )

    p.add_argument(
        "--hard-bid",
        dest="hard_bid_cents",
        type=int,
        default=None,
        help=(
            "Fixed limit bid price in cents for every BUY order. "
            "When set, slippage is forced to 0 and ask+slippage pricing is ignored. "
            f"Allowed range: 1-{MAX_BID_CENTS}. Example: --hard-bid 75"
        ),
    )

    p.add_argument(
        "--order-type",
        choices=["limit", "market"],
        default="limit",
        help="Default is limit, which sends IOC at ask+slippage.",
    )

    p.add_argument(
        "--time-in-force",
        choices=["fill_or_kill", "good_till_canceled", "immediate_or_cancel"],
        default="immediate_or_cancel",
        help="Time-in-force for limit orders. Default: immediate_or_cancel",
    )

    p.add_argument(
        "--order-submit-api",
        choices=["v2", "legacy"],
        default="v2",
        help=(
            "Order submit endpoint to use. Default: v2 uses POST /portfolio/events/orders "
            "with the lower-cost V2 request shape. Use legacy only to troubleshoot old SDK behavior."
        ),
    )

    p.add_argument(
        "--order-submit-timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds for raw V2 order submits. Default: 10",
    )

    p.add_argument(
        "--execution-mode",
        choices=["simple", "ioc-ladder"],
        default="simple",
        help=(
            "Order execution strategy. simple = one IOC/limit order for the side-specific count. "
            "ioc-ladder = split the side-specific count into sequential IOC slices at increasing "
            "ask+step prices. Default: simple"
        ),
    )

    p.add_argument(
        "--ladder-slices",
        default="50,30,20",
        help=(
            "Comma-separated positive slice weights for --execution-mode ioc-ladder. "
            "Default 50,30,20 means roughly 50%%,30%%,20%% of the side-specific count."
        ),
    )

    p.add_argument(
        "--ladder-price-steps",
        default="1,2,3",
        help=(
            "Comma-separated ask-plus cents for each IOC ladder slice. "
            "Default 1,2,3 means ask+1c, then ask+2c, then ask+3c. "
            "Ignored when --hard-bid is set."
        ),
    )

    p.add_argument(
        "--ladder-time-in-force",
        choices=["immediate_or_cancel", "fill_or_kill"],
        default="immediate_or_cancel",
        help="Time-in-force for ladder slices. Default: immediate_or_cancel",
    )

    p.add_argument(
        "--refresh-seconds",
        type=float,
        default=0.5,
        help=(
            "Auto-refresh market list / REST fallback values every N seconds. "
            "Order pricing prefers the WebSocket book. Use 0 to disable automatic REST refresh. "
            "Default: 0.5"
        ),
    )

    p.add_argument(
        "--ws-orderbook",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Use one multiplexed Kalshi WebSocket for latest orderbook asks. "
            "Default: enabled. Use --no-ws-orderbook to fall back to REST snapshots."
        ),
    )

    p.add_argument(
        "--ws-url",
        default=None,
        help="Override the WebSocket URL selected by --demo/--prod.",
    )

    p.add_argument(
        "--read-rate-limit",
        type=float,
        default=DEFAULT_READ_RATE_LIMIT_PER_SECOND,
        help=(
            "Max REST/read API calls per second before local throttling. "
            f"Default: {DEFAULT_READ_RATE_LIMIT_PER_SECOND:g}, below Basic cap "
            f"{BASIC_READ_LIMIT_PER_SECOND}/s. Use 0 to disable."
        ),
    )

    p.add_argument(
        "--write-rate-limit",
        type=float,
        default=DEFAULT_WRITE_RATE_LIMIT_PER_SECOND,
        help=(
            "Max write/order API calls per second before local throttling. "
            f"Default: {DEFAULT_WRITE_RATE_LIMIT_PER_SECOND:g}, a conservative Basic-tier "
            "starting point under Kalshi's token model. Use 0 to disable."
        ),
    )

    p.add_argument(
        "--max-429-retries",
        type=int,
        default=5,
        help="Retry HTTP 429 failures this many times with backoff. Default: 5",
    )

    p.add_argument(
        "--backoff-base-seconds",
        type=float,
        default=0.5,
        help="Initial exponential backoff delay for HTTP 429. Default: 0.5",
    )

    p.add_argument(
        "--backoff-max-seconds",
        type=float,
        default=10.0,
        help="Maximum exponential backoff delay for HTTP 429. Default: 10",
    )

    p.add_argument(
        "--api-log-file",
        default=os.path.expanduser("~/.local/state/kalshi_broadcast_word_trader/api_calls.log"),
        help="Human-readable API pass/fail/rate-limit log. Use '' to disable.",
    )

    p.add_argument(
        "--demo-rate-limit-test",
        type=int,
        default=0,
        help=(
            "Demo-only test mode: submit this many one-at-a-time demo orders using "
            "the normal write limiter/backoff, print PASS/FAIL summary, then exit. "
            "Requires --demo --live. Use --count 1 or side-specific count 1 for safest testing."
        ),
    )

    p.add_argument(
        "--ws-wait-seconds",
        type=float,
        default=3.0,
        help="Wait this long for initial WebSocket snapshots before accepting input. Default: 3.0",
    )

    p.add_argument(
        "--ws-snapshot-max-age-ms",
        type=int,
        default=1000,
        help=(
            "Keep quiet subscribed orderbooks fresh by requesting a WebSocket "
            "get_snapshot when their cache age exceeds this value. The order path "
            "also repairs a stale ticker before pricing. Default: 1000. Use 0 to disable."
        ),
    )

    p.add_argument(
        "--ws-fresh-order-wait-ms",
        type=int,
        default=80,
        help=(
            "When an order's WS cache is stale/missing, request a fresh WS snapshot "
            "and wait up to this many milliseconds before using the current cached "
            "quote. Default: 80. Use 0 for no hot-path wait."
        ),
    )

    p.add_argument(
        "--ws-update-queue-size",
        type=int,
        default=10_000,
        help=(
            "Maximum wait_for_update() notification queue size inside kx_orderbooks. "
            "When full, patched kx_orderbooks drops the oldest notification and keeps "
            "the newest. Default: 10000"
        ),
    )

    p.add_argument(
        "--ws-required",
        action="store_true",
        help=(
            "Require a ready WebSocket quote before building an order. "
            "Without this, the script falls back to the REST market snapshot."
        ),
    )

    p.add_argument(
        "--ws-raw",
        action="store_true",
        help="Log raw WebSocket messages from kx_orderbooks for debugging.",
    )

    p.add_argument(
        "--mention-side",
        choices=["yes", "no"],
        default="yes",
        help="Side to BUY when a typed word is detected. Default: yes",
    )

    p.add_argument(
        "--trade-controls",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable manual side/action controls: Ctrl-B BUY, Ctrl-S SELL, Ctrl-D DISQUALIFY, "
            "Ctrl-Y YES side, Ctrl-N NO side, Ctrl-T edit selected-side size, Ctrl-H help. "
            "With these controls, typing only highlights a market; Enter is required to select it "
            "for BUY, SELL, or DISQUALIFY. Ctrl-R refreshes/displays positions and books. "
            "Default: disabled."
        ),
    )

    p.add_argument(
        "--trade-control-refresh-wait-ms",
        type=int,
        default=120,
        help=(
            "After Ctrl-R requests WS snapshots, wait this many milliseconds before "
            "rendering the trade-control book. Default: 120."
        ),
    )

    p.add_argument(
        "--end-no",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When END is typed, BUY NO on un-heard markets. Default: enabled",
    )

    p.add_argument(
        "--end-no-includes-heard",
        action="store_true",
        help="On END, also BUY NO for markets already marked heard. Default skips heard markets.",
    )

    p.add_argument(
        "--end-no-skip-yes-at",
        dest="end_no_skip_yes_at_cents",
        type=int,
        default=DEFAULT_END_NO_SKIP_YES_AT_CENTS,
        help=(
            "END safety gate: skip BUY NO when the current YES signal is this "
            "many cents or higher. Default: 97. This makes END buy only the "
            "remaining NOs where YES is still below 97c."
        ),
    )

    p.add_argument(
        "--end-no-min-no-ask",
        dest="end_no_min_no_ask_cents",
        type=int,
        default=DEFAULT_END_NO_MIN_NO_ASK_CENTS,
        help=(
            "END safety gate: only queue BUY NO when the current NO ask is at "
            "least this many cents. Default: 4, which enforces NO > 3c. "
            "Use 0 to disable this NO-side floor."
        ),
    )

    p.add_argument(
        "--end-no-order",
        choices=["no-ask-low", "no-ask-high", "yes-low", "alpha", "market"],
        default="no-ask-low",
        help=(
            "Order used when END queues multiple BUY NO tasks. Default: no-ask-low, "
            "which prioritizes the cheapest NO asks first for highest upside. "
            "Use market to preserve the loaded market order."
        ),
    )

    p.add_argument(
        "--auto-finish-yes-at",
        dest="auto_finish_yes_at_cents",
        type=int,
        default=DEFAULT_AUTO_FINISH_YES_AT_CENTS,
        help=(
            "Passively mark markets finished YES when the current YES signal reaches "
            "this many cents. Finished markets are skipped by END. Default: 99. "
            "Use 0 to disable the YES finished marker."
        ),
    )

    p.add_argument(
        "--auto-finish-no-at",
        dest="auto_finish_no_at_cents",
        type=int,
        default=DEFAULT_AUTO_FINISH_NO_AT_CENTS,
        help=(
            "Passively mark markets finished NO when the current YES signal is this "
            "many cents or lower. Finished markets are skipped by END. Default: 1. "
            "Use 0 to disable the NO finished marker."
        ),
    )

    p.add_argument(
        "--end-no-require-yes-quote",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Require a usable YES quote before END can queue BUY NO. Default: enabled. "
            "Use --no-end-no-require-yes-quote to allow blind END NO fallback."
        ),
    )

    p.add_argument(
        "--end-hotkey",
        default=DEFAULT_END_HOTKEY,
        help=(
            "Control-key hotkey that triggers END immediately. Default: ctrl-e. "
            "Examples: ctrl-e, C-e, ^E, ctrl-], none. Ctrl-C/Ctrl-D/Tab/Enter are rejected (Ctrl-D is reserved for DISQUALIFY mode when trade controls are enabled)."
        ),
    )

    p.add_argument(
        "--allow-orders-after-end",
        action="store_true",
        help="Allow detected words after END. Default: ignore word detections after END.",
    )

    p.add_argument(
        "--exit-input-after-end",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop reading typed input after END. Default: enabled",
    )

    p.add_argument(
        "--stay-open-after-end",
        action="store_true",
        help="Do not exit automatically after END orders drain.",
    )

    p.add_argument(
        "--min-word-len",
        type=int,
        default=2,
        help="Ignore market words shorter than this after normalization. Default: 2",
    )

    p.add_argument(
        "--max-stream-chars",
        type=int,
        default=500,
        help="Rolling normalized input buffer length. Default: 500",
    )

    p.add_argument(
        "--max-raw-token-chars",
        type=int,
        default=50,
        help="Max current raw token length used for END detection. Default: 50",
    )


    p.add_argument(
        "--autocomplete",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Show zsh/bash-like autocomplete candidates while typing. "
            "Use arrow keys to choose and Enter to select. Tab intentionally does nothing. Default: enabled."
        ),
    )

    p.add_argument(
        "--autocomplete-min-chars",
        type=int,
        default=1,
        help="Start showing autocomplete after this many typed characters. Default: 1",
    )

    p.add_argument(
        "--autocomplete-limit",
        type=int,
        default=8,
        help="Maximum autocomplete candidates to consider/display. Default: 8",
    )

    p.add_argument(
        "--spike-detect",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Print an early alert when the live WebSocket YES price jumps quickly. "
            "This never places orders and never marks the market heard. Default: enabled."
        ),
    )

    p.add_argument(
        "--spike-move-cents",
        type=int,
        default=DEFAULT_SPIKE_MOVE_CENTS,
        help=(
            "YES price move, in cents, required to print SPIKE DETECTED. "
            f"Default: {DEFAULT_SPIKE_MOVE_CENTS}"
        ),
    )

    p.add_argument(
        "--spike-window-seconds",
        type=float,
        default=DEFAULT_SPIKE_WINDOW_SECONDS,
        help=(
            "Lookback window for rapid YES price moves. "
            f"Default: {DEFAULT_SPIKE_WINDOW_SECONDS:g}"
        ),
    )

    p.add_argument(
        "--spike-cooldown-seconds",
        type=float,
        default=DEFAULT_SPIKE_COOLDOWN_SECONDS,
        help=(
            "Minimum seconds between repeated spike alerts for the same market. "
            f"Default: {DEFAULT_SPIKE_COOLDOWN_SECONDS:g}"
        ),
    )

    p.add_argument(
        "--spike-max-spread-cents",
        type=int,
        default=DEFAULT_SPIKE_MAX_SPREAD_CENTS,
        help=(
            "When YES bid/ask spread is wider than this, spike alerts are printed "
            "as WIDE SPREAD / UNCONFIRMED instead of a clean spike. "
            f"Default: {DEFAULT_SPIKE_MAX_SPREAD_CENTS}"
        ),
    )

    p.add_argument(
        "--display-wide-spread-cents",
        type=int,
        default=DEFAULT_DISPLAY_WIDE_SPREAD_CENTS,
        help=(
            "Show bid/ask spread in red when either YES or NO spread is wider "
            "than this many cents. Default: 15"
        ),
    )

    p.add_argument(
        "--startup-word-limit",
        type=int,
        default=0,
        help=(
            "Maximum open-market lines to print at startup. 0 means print all. "
            "Default: 0"
        ),
    )

    p.add_argument(
        "--debug-input",
        action="store_true",
        help=(
            "Print per-keystroke matcher state: raw_token, recent_norm, "
            "recent_compact, separators, and detected alias matches."
        ),
    )

    p.add_argument(
        "--order-workers",
        type=int,
        default=1,
        help="Number of order worker threads. Default: 1. Increase carefully.",
    )

    p.add_argument(
        "--status-seconds",
        type=float,
        default=0.5,
        help="How often to print compact status lines. Default: 0.5",
    )

    p.add_argument(
        "--live",
        action="store_true",
        help="Actually place orders. Without this, only dry-runs.",
    )

    p.add_argument(
        "--log-file",
        default=os.path.expanduser("~/.local/state/kalshi_broadcast_word_trader/orders.log"),
        help="Human-readable file to append order attempts to.",
    )

    p.add_argument(
        "--defer-log-writes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Buffer API/order log writes in memory during hot order bursts and flush "
            "after the queue drains or at shutdown. Default: enabled. Use "
            "--no-defer-log-writes for immediate per-order disk writes while debugging."
        ),
    )

    p.add_argument(
        "--transcript",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Save typed listening transcript and JSONL events. Default: disabled. "
            "Use --transcript to enable."
        ),
    )

    p.add_argument(
        "--transcript-file",
        default=None,
        help=(
            "Readable transcript output file. Default: "
            "~/.local/state/kalshi_broadcast_word_trader/transcripts/"
            "transcript-YYYYmmdd-HHMMSS.txt"
        ),
    )

    p.add_argument(
        "--auth-check",
        action="store_true",
        help="Do a signed REST /portfolio/balance auth check and exit.",
    )

    p.add_argument(
        "--auth-check-before-ws",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Before opening a required WebSocket, verify the same key/private-key pair "
            "with a signed REST balance request. Default: enabled."
        ),
    )

    p.add_argument(
        "--auth-check-timeout",
        type=float,
        default=5.0,
        help="Timeout in seconds for --auth-check / --auth-check-before-ws. Default: 5.0",
    )

    p.add_argument(
        "--dump-words",
        action="store_true",
        help="Load markets, print searchable words/tickers, then exit.",
    )

    args = p.parse_args()

    if args.kalshi_env == "demo":
        default_api_host = DEMO_REST_HOST if args.demo_host_style == "external" else DEMO_REST_HOST_ALT
        default_ws_url = DEMO_WS_URL if args.demo_host_style == "external" else DEMO_WS_URL_ALT
    else:
        default_api_host = PROD_REST_HOST
        default_ws_url = PROD_WS_URL

    if not args.api_host:
        args.api_host = default_api_host
    args.api_host = str(args.api_host).rstrip("/")

    if not args.ws_url:
        args.ws_url = default_ws_url

    if args.full_name:
        try:
            full_series, full_event = split_full_market_event_name(args.full_name)
        except ValueError as exc:
            p.error(f"--full-name: {exc}")

        if args.ticker and args.ticker.strip().upper() != full_series:
            p.error(
                "--full-name already includes the series ticker; do not also pass "
                f"a different positional ticker ({args.ticker!r})"
            )
        if args.market_event and args.market_event.strip().strip('"').strip("'").upper().strip("-") != full_event:
            p.error(
                "--full-name already includes the event code; do not also pass "
                f"a different --market-event ({args.market_event!r})"
            )

        args.ticker = full_series
        args.market_event = full_event
        args._full_name_parsed = f"{full_series}-{full_event}"
    else:
        args._full_name_parsed = None

    if args.log_file:
        Path(args.log_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.log_file = str(Path(args.log_file).expanduser())

    if args.api_log_file:
        Path(args.api_log_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.api_log_file = str(Path(args.api_log_file).expanduser())

    if args.transcript:
        if not args.transcript_file:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            args.transcript_file = os.path.expanduser(
                "~/.local/state/kalshi_broadcast_word_trader/transcripts/"
                f"transcript-{stamp}.txt"
            )
        Path(args.transcript_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.transcript_file = str(Path(args.transcript_file).expanduser())
    else:
        args.transcript_file = None

    if not args.auth_check and not args.file and not args.ticker:
        p.error("Provide a ticker, --full-name, or --file")

    if args.ticker:
        args.ticker = args.ticker.upper()

    if args.market_event:
        args.market_event = args.market_event.strip().strip('"').strip("'").upper()

    args.market_prefix = [
        prefix.strip().strip('"').strip("'").upper()
        for prefix in (args.market_prefix or [])
        if prefix and prefix.strip().strip('"').strip("'")
    ]

    if args.market_event and not args.ticker and "-" not in args.market_event:
        p.error(
            "--market-event without a positional ticker must be a full prefix like "
            "KXMLBMENTION-26MAY16NYYNYM. With --file and just an event code, "
            "use --market-prefix instead."
        )

    if args.count <= 0:
        p.error("--count must be positive")
    if args.count_yes is not None and args.count_yes <= 0:
        p.error("--count-yes must be positive")
    if args.count_no is not None and args.count_no <= 0:
        p.error("--count-no must be positive")

    if args.slippage_cents < 0:
        p.error("--slippage-cents must be >= 0")
    if args.slippage_yes_cents is not None and args.slippage_yes_cents < 0:
        p.error("--slippage-yes must be >= 0")
    if args.slippage_no_cents is not None and args.slippage_no_cents < 0:
        p.error("--slippage-no must be >= 0")

    try:
        args.ladder_slice_weights = parse_positive_int_csv(args.ladder_slices, name="--ladder-slices")
        args.ladder_price_steps_cents = parse_positive_int_csv(
            args.ladder_price_steps,
            name="--ladder-price-steps",
            allow_zero=True,
        )
    except ValueError as exc:
        p.error(str(exc))

    if len(args.ladder_slice_weights) != len(args.ladder_price_steps_cents):
        p.error("--ladder-slices and --ladder-price-steps must have the same number of entries")

    if args.execution_mode == "ioc-ladder" and args.order_type != "limit":
        p.error("--execution-mode ioc-ladder requires --order-type limit")

    if args.hard_bid_cents is not None:
        if not 1 <= args.hard_bid_cents <= MAX_BID_CENTS:
            p.error(f"--hard-bid must be between 1 and {MAX_BID_CENTS}")
        args.slippage_cents = 0

    if args.hard_bid_cents is not None and args.order_type != "limit":
        p.error("--hard-bid requires --order-type limit")

    if args.order_submit_api == "v2" and args.order_type != "limit":
        p.error("--order-submit-api v2 currently requires --order-type limit; use --order-submit-api legacy for market orders")

    if args.order_submit_timeout <= 0:
        p.error("--order-submit-timeout must be > 0")

    if args.refresh_seconds < 0:
        p.error("--refresh-seconds must be >= 0")

    if args.read_rate_limit < 0:
        p.error("--read-rate-limit must be >= 0")

    if args.write_rate_limit < 0:
        p.error("--write-rate-limit must be >= 0")

    if args.max_429_retries < 0:
        p.error("--max-429-retries must be >= 0")

    if args.backoff_base_seconds <= 0:
        p.error("--backoff-base-seconds must be > 0")

    if args.backoff_max_seconds <= 0:
        p.error("--backoff-max-seconds must be > 0")

    if args.backoff_max_seconds < args.backoff_base_seconds:
        p.error("--backoff-max-seconds must be >= --backoff-base-seconds")

    if args.ws_wait_seconds < 0:
        p.error("--ws-wait-seconds must be >= 0")

    if args.ws_snapshot_max_age_ms < 0:
        p.error("--ws-snapshot-max-age-ms must be >= 0")

    if args.ws_fresh_order_wait_ms < 0:
        p.error("--ws-fresh-order-wait-ms must be >= 0")

    if args.ws_update_queue_size <= 0:
        p.error("--ws-update-queue-size must be positive")

    if args.trade_control_refresh_wait_ms < 0:
        p.error("--trade-control-refresh-wait-ms must be >= 0")

    if args.trade_controls and args.hard_bid_cents is not None:
        p.error("--trade-controls cannot be combined with --hard-bid; BUY and SELL use live book prices")

    if args.status_seconds <= 0:
        p.error("--status-seconds must be > 0")

    if args.min_word_len <= 0:
        p.error("--min-word-len must be positive")

    if args.max_stream_chars < 50:
        p.error("--max-stream-chars must be at least 50")

    if args.autocomplete_min_chars <= 0:
        p.error("--autocomplete-min-chars must be positive")

    if args.autocomplete_limit <= 0:
        p.error("--autocomplete-limit must be positive")

    if args.spike_move_cents <= 0:
        p.error("--spike-move-cents must be positive")

    if args.spike_window_seconds <= 0:
        p.error("--spike-window-seconds must be > 0")

    if args.spike_cooldown_seconds < 0:
        p.error("--spike-cooldown-seconds must be >= 0")

    if args.spike_max_spread_cents < 0:
        p.error("--spike-max-spread-cents must be >= 0")

    if args.spike_detect and not args.ws_orderbook:
        safe_print("WARNING: --spike-detect needs --ws-orderbook; spike detection will be inactive.")

    if args.end_no_skip_yes_at_cents <= 0 or args.end_no_skip_yes_at_cents > 101:
        p.error("--end-no-skip-yes-at must be between 1 and 101")

    if args.end_no_min_no_ask_cents <= 0:
        args.end_no_min_no_ask_cents = None
    elif args.end_no_min_no_ask_cents > 101:
        p.error("--end-no-min-no-ask must be between 1 and 101, or 0 to disable")

    if args.auto_finish_yes_at_cents <= 0:
        args.auto_finish_yes_at_cents = None
    elif args.auto_finish_yes_at_cents > 101:
        p.error("--auto-finish-yes-at must be between 1 and 101, or 0 to disable")

    if args.auto_finish_no_at_cents <= 0:
        args.auto_finish_no_at_cents = None
    elif args.auto_finish_no_at_cents > 101:
        p.error("--auto-finish-no-at must be between 1 and 101, or 0 to disable")

    if (
        args.auto_finish_yes_at_cents is not None
        and args.auto_finish_no_at_cents is not None
        and args.auto_finish_no_at_cents >= args.auto_finish_yes_at_cents
    ):
        p.error("--auto-finish-no-at must be lower than --auto-finish-yes-at")

    if (
        args.auto_finish_yes_at_cents is not None
        and args.auto_finish_yes_at_cents < args.end_no_skip_yes_at_cents
    ):
        safe_print(
            "WARNING: --auto-finish-yes-at is below --end-no-skip-yes-at. "
            "END will still skip both finished and high-YES markets."
        )

    if args.order_workers <= 0:
        p.error("--order-workers must be positive")

    if args.demo_rate_limit_test < 0:
        p.error("--demo-rate-limit-test must be >= 0")

    if args.demo_rate_limit_test and args.kalshi_env != "demo":
        p.error("--demo-rate-limit-test is intentionally only allowed with --demo")

    if args.demo_rate_limit_test and not args.live:
        p.error("--demo-rate-limit-test requires --live so it actually exercises write calls")

    if args.order_workers > 1 and args.live:
        safe_print(
            "WARNING: --order-workers > 1 in --live mode can send many orders concurrently. "
            "Use carefully."
        )

    try:
        args.end_hotkey_char, args.end_hotkey_label = normalize_end_hotkey(args.end_hotkey)
    except ValueError as exc:
        p.error(str(exc))

    return args


def main() -> int:
    """Script entry point."""
    args = parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
