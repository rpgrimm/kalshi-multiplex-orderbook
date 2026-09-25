#!/usr/bin/env python3
"""Cache + headless key script. Uses production browse handlers. No network."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kalshi_sports_trader import (
    SESSION_BETS,
    MarketRow,
    append_filter_char,
    confirm_td_draft,
    make_headless_state,
    run_key_script,
)
from sports_engine.key_script import parse_key_script
from sports_engine.play_protocol import parse_play_query
from sports_engine.market_cache import load_market_cache, save_market_cache

BUF = "team-buf"
DET = "team-det"


def market(
    ticker: str,
    title: str,
    *,
    series: str,
    team: str | None = None,
    floor: float | None = None,
) -> MarketRow:
    event = f"{series}-26SEP17DETBUF"
    raw: dict = {
        "ticker": ticker.upper(),
        "title": title,
        "event_ticker": event,
        "status": "active",
        "yes_sub_title": title.split(":")[0],
    }
    if floor is not None:
        raw["floor_strike"] = floor
    if team:
        raw["custom"] = {"football_team": team}
    return MarketRow(
        ticker=ticker.upper(),
        title=title,
        event_ticker=event,
        series_ticker=series.upper(),
        status="active",
        yes_sub_title=title.split(":")[0],
        raw=raw,
    )


def fixture_rows() -> list[MarketRow]:
    return [
        market("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", "Khalil Shakir: 1st Touchdown", series="KXNFLFIRSTTD", team=BUF),
        market("KXNFLFIRSTTD-26SEP17DETBUF-DETJGOFF16", "Jared Goff: 1st Touchdown", series="KXNFLFIRSTTD", team=DET),
        market("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", "Khalil Shakir: 1+ touchdowns", series="KXNFLTD", team=BUF, floor=0.5),
        market("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-2", "Khalil Shakir: 2+ touchdowns", series="KXNFLTD", team=BUF, floor=1.5),
        market("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", "Josh Allen: 1+ passing touchdowns", series="KXNFLPASSTDS", team=BUF, floor=0.5),
        market("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", "Josh Allen: 2+ passing touchdowns", series="KXNFLPASSTDS", team=BUF, floor=1.5),
        market("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", "Jared Goff: 1+ passing touchdowns", series="KXNFLPASSTDS", team=DET, floor=0.5),
        market("KXNFL1QTOTAL-26SEP17DETBUF-7", "Will there be over 6.5 1Q points scored?", series="KXNFL1QTOTAL", floor=6.5),
    ]


def args() -> argparse.Namespace:
    return argparse.Namespace(
        live=False,
        count=1,
        count_yes=1,
        slippage_cents=1,
        time_in_force="immediate_or_cancel",
        kalshi_env="prod",
    )


class TestKeyScript(unittest.TestCase):
    def test_parse_operator_line(self) -> None:
        steps = parse_key_script("/ wa Enter td Enter re Enter")
        kinds = [k for k, _ in steps]
        self.assertEqual(kinds, ["slash", "word", "enter", "word", "enter", "word", "enter"])
        self.assertEqual(steps[1], ("word", "wa"))

    def test_cache_roundtrip_preserves_custom_team(self) -> None:
        rows = fixture_rows()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            save_market_cache(
                path,
                rows=rows,
                game_code="26SEP17DETBUF",
                seed_series="KXNFLGAME",
                kalshi_env="prod",
            )
            data = load_market_cache(path)
        self.assertEqual(data["market_count"], len(rows))
        first = next(m for m in data["markets"] if "SHAKIR" in m["ticker"] and "FIRSTTD" in m["ticker"])
        self.assertEqual(first["custom"]["football_team"], BUF)

    def test_script_td_enter_re_enter_dumps_bets_and_records_one_td(self) -> None:
        state = make_headless_state(
            seed_series="KXNFLGAME",
            game_code="26SEP17DETBUF",
            rows=fixture_rows(),
            args=args(),
        )
        report = run_key_script(state, "/ shakir Enter td Enter re Enter")
        drafts = [e for e in report["log"] if e["event"] == "draft"]
        confirms = [e for e in report["log"] if e["event"] == "confirm"]
        self.assertGreaterEqual(len(drafts), 2)
        first_ids = {a["ticker"] for a in drafts[0]["armed"]}
        self.assertIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", first_ids)
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-7", first_ids)
        self.assertFalse(any("PASSTDS" in t for t in first_ids))
        rec_ids = {a["ticker"] for a in drafts[-1]["armed"]}
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", rec_ids)
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-7", rec_ids)
        self.assertEqual(len(confirms), 1)
        sent = {b["ticker"] for b in confirms[0]["would_send"]}
        self.assertEqual(sent, rec_ids)
        self.assertEqual(report["state"]["game_tds"], 1)
        self.assertEqual(report["state"]["away_score"] + report["state"]["home_score"], 6)

    def test_td_enter_leaves_space_so_re_is_not_tdre(self) -> None:
        state = make_headless_state(
            seed_series="KXNFLGAME",
            game_code="26SEP17DETBUF",
            rows=fixture_rows(),
            args=args(),
        )
        run_key_script(state, "/ shakir Enter td Enter")
        self.assertTrue(state.filter_text.endswith(" "), state.filter_text)
        append_filter_char(state, "r")
        append_filter_char(state, "e")
        q = parse_play_query(state.filter_text)
        self.assertEqual(q.kind, "td")
        self.assertEqual(q.intent, "receiving")
        self.assertNotIn("tdre", state.filter_text.lower())
        self.assertIn("td re", state.filter_text.lower())

    def test_script_pat_adds_q_total(self) -> None:
        state = make_headless_state(
            seed_series="KXNFLGAME",
            game_code="26SEP17DETBUF",
            rows=fixture_rows(),
            args=args(),
        )
        report = run_key_script(state, "/ shakir Enter td Enter re pat Enter")
        confirms = [e for e in report["log"] if e["event"] == "confirm"]
        sent = {b["ticker"] for b in confirms[0]["would_send"]}
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", sent)
        self.assertEqual(report["state"]["away_score"] + report["state"]["home_score"], 7)

    def test_dry_confirm_records_qb_leg_without_quotes(self) -> None:
        SESSION_BETS.bets.clear()
        state = make_headless_state(
            seed_series="KXNFLGAME",
            game_code="26SEP17DETBUF",
            rows=fixture_rows(),
            args=args(),
        )
        run_key_script(state, "/ shakir Enter td Enter re")
        state.script_mode = False
        confirm_td_draft(state)
        tickers = {b.ticker.upper() for b in SESSION_BETS.bets if b.remaining > 0}
        self.assertIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", tickers)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", tickers)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", tickers)
        SESSION_BETS.bets.clear()

    def test_script_without_confirm_does_not_change_score(self) -> None:
        state = make_headless_state(
            seed_series="KXNFLGAME",
            game_code="26SEP17DETBUF",
            rows=fixture_rows(),
            args=args(),
        )
        report = run_key_script(state, "/ shakir Enter td Enter re")
        self.assertEqual(report["state"]["game_tds"], 0)
        self.assertTrue(any(e["event"] == "draft" for e in report["log"]))
        self.assertFalse(any(e["event"] == "confirm" for e in report["log"]))


if __name__ == "__main__":
    unittest.main()
