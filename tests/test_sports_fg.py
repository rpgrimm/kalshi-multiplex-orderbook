#!/usr/bin/env python3
"""Team FG ladder from game-code teams. No network."""

from __future__ import annotations

import argparse
import unittest
from types import SimpleNamespace

from kalshi_sports_trader import make_headless_state, run_key_script
from sports_engine.browse import apply_fg_draft, arm_fg_draft, make_browse_session
from sports_engine.catalog import resolve_game_team, teams_from_game_code
from sports_engine.play_protocol import parse_play_query


def row(ticker, title, *, series, floor=None):
    raw: dict = {}
    if floor is not None:
        raw["floor_strike"] = floor
    return SimpleNamespace(
        ticker=ticker.upper(),
        title=title,
        series_ticker=series.upper(),
        yes_sub_title=title.split(":")[0],
        raw=raw,
    )


def fixture():
    return [
        row("KXNFLFG-26SEP27KCMIA-MIA1", "Miami: 1+ Field Goals", series="KXNFLFG", floor=0.5),
        row("KXNFLFG-26SEP27KCMIA-MIA2", "Miami: 2+ Field Goals", series="KXNFLFG", floor=1.5),
        row("KXNFLFG-26SEP27KCMIA-KC1", "Kansas City: 1+ Field Goals", series="KXNFLFG", floor=0.5),
        row("KXNFLFG-26SEP27KCMIA-KC2", "Kansas City: 2+ Field Goals", series="KXNFLFG", floor=1.5),
        row(
            "KXNFL1QTOTAL-26SEP27KCMIA-7",
            "Will there be over 6.5 1Q points scored?",
            series="KXNFL1QTOTAL",
            floor=6.5,
        ),
    ]


class TestFieldGoals(unittest.TestCase):
    def test_game_code_teams_and_m_is_miami(self) -> None:
        away, home = teams_from_game_code("26SEP27KCMIA")
        self.assertEqual((away, home), ("KC", "MIA"))
        self.assertEqual(resolve_game_team("m", away, home), "MIA")
        self.assertEqual(resolve_game_team("k", away, home), "KC")
        self.assertEqual(resolve_game_team("mia", away, home), "MIA")
        q = parse_play_query("fg m")
        self.assertEqual(q.kind, "fg")
        self.assertEqual(q.name_tokens, ("m",))
        self.assertEqual(q.filter_tokens(), ["fg"])

    def test_fg_m_arms_miami_1plus_not_kc_or_qtotal(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP27KCMIA", rows)
        draft, cands, msg = arm_fg_draft(session, rows, parse_play_query("fg m"))
        ids = {c.market_id for c in cands}
        self.assertEqual(ids, {"KXNFLFG-26SEP27KCMIA-MIA1"})
        self.assertIsNotNone(draft)
        self.assertEqual(draft.kind, "fg")
        self.assertEqual(draft.team, "MIA")
        self.assertNotIn("QTOTAL", msg)
        self.assertEqual(session.state().away_score + session.state().home_score, 0)

    def test_confirm_fg_is_plus_3_then_next_is_2plus(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP27KCMIA", rows)
        draft, _, _ = arm_fg_draft(session, rows, parse_play_query("fg m"))
        assert draft is not None
        apply_fg_draft(session, draft)
        st = session.state()
        self.assertEqual(st.home_score, 3)
        self.assertEqual(st.home_stats.field_goals, 1)
        self.assertEqual(st.game_tds, 0)
        draft2, cands2, _ = arm_fg_draft(session, rows, parse_play_query("fg mia"))
        ids = {c.market_id for c in cands2}
        self.assertEqual(ids, {"KXNFLFG-26SEP27KCMIA-MIA2"})

    def test_script_fg_m_enter_enter(self) -> None:
        args = argparse.Namespace(live=False, count=1, count_yes=1, slippage_cents=1)
        state = make_headless_state(
            seed_series="KXNFLGAME",
            game_code="26SEP27KCMIA",
            rows=fixture(),
            args=args,
        )
        report = run_key_script(state, "/ fg m Enter Enter")
        confirms = [e for e in report["log"] if e["event"] == "confirm"]
        self.assertEqual(len(confirms), 1)
        sent = {b["ticker"] for b in confirms[0]["would_send"]}
        self.assertEqual(sent, {"KXNFLFG-26SEP27KCMIA-MIA1"})
        self.assertEqual(report["filter"].strip(), "")
        self.assertEqual(report["state"]["home_score"], 3)
        self.assertEqual(report["state"]["away_score"], 0)


if __name__ == "__main__":
    unittest.main()
