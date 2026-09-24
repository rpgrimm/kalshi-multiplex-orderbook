#!/usr/bin/env python3
"""Play protocol, TD draft/confirm, and quarter-end. No network."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sports_engine.browse import (
    apply_td_draft,
    arm_td_draft,
    ingest_quarter_end,
    make_browse_session,
)
from sports_engine.catalog import player_last_names
from sports_engine.play_protocol import parse_play_query, tab_complete_player

BUF = "team-buf"
DET = "team-det"


def row(ticker, title, *, series, team=None, floor=None):
    raw: dict = {}
    if floor is not None:
        raw["floor_strike"] = floor
    if team:
        raw["custom"] = {"football_team": team}
    return SimpleNamespace(
        ticker=ticker.upper(),
        title=title,
        series_ticker=series.upper(),
        yes_sub_title=title.split(":")[0],
        raw=raw,
    )


def fixture():
    return [
        row("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", "Khalil Shakir: 1st Touchdown", series="KXNFLFIRSTTD", team=BUF),
        row("KXNFLFIRSTTD-26SEP17DETBUF-DETJGOFF16", "Jared Goff: 1st Touchdown", series="KXNFLFIRSTTD", team=DET),
        row("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", "Khalil Shakir: 1+ touchdowns", series="KXNFLTD", team=BUF, floor=0.5),
        row("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-2", "Khalil Shakir: 2+ touchdowns", series="KXNFLTD", team=BUF, floor=1.5),
        row("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", "Josh Allen: 1+ passing touchdowns", series="KXNFLPASSTDS", team=BUF, floor=0.5),
        row("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", "Josh Allen: 2+ passing touchdowns", series="KXNFLPASSTDS", team=BUF, floor=1.5),
        row("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", "Jared Goff: 1+ passing touchdowns", series="KXNFLPASSTDS", team=DET, floor=0.5),
        row("KXNFL1QTOTAL-26SEP17DETBUF-7", "Will there be over 6.5 1Q points scored?", series="KXNFL1QTOTAL", floor=6.5),
        row("KXNFL1QTOTAL-26SEP17DETBUF-8", "Will there be over 7.5 1Q points scored?", series="KXNFL1QTOTAL", floor=7.5),
        row("KXNFL2QTOTAL-26SEP17DETBUF-7", "Will there be over 6.5 2Q points scored?", series="KXNFL2QTOTAL", floor=6.5),
    ]


class TestPlayProtocol(unittest.TestCase):
    def test_reserved_tokens_stripped_from_filter(self) -> None:
        q = parse_play_query("wa td rec")
        self.assertEqual(q.name_tokens, ("wa",))
        self.assertEqual(q.kind, "td")
        self.assertEqual(q.intent, "receiving")
        self.assertEqual(q.filter_tokens(), ["wa", "td"])
        self.assertTrue(parse_play_query("shakir td rec pat").pat)
        self.assertEqual(parse_play_query("shakir td rec pat").filter_tokens(), ["shakir", "td"])
        self.assertTrue(parse_play_query("shakir td rec").is_complete_td())
        self.assertFalse(parse_play_query("shakir td").is_complete_td())

    def test_tab_completes_last_name(self) -> None:
        names = player_last_names(fixture())
        text, hits = tab_complete_player("sha", names)
        self.assertEqual(text, "shakir")
        self.assertEqual(hits, ["shakir"])

    def test_td_enter_does_not_change_score_and_omits_qb(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        q = parse_play_query("shakir td")
        draft, cands, msg = arm_td_draft(session, rows, q, intent=None)
        ids = {c.market_id for c in cands}
        self.assertIsNotNone(draft)
        self.assertIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", ids)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", ids)
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-7", ids)
        self.assertFalse(any("PASSTDS" in i for i in ids))
        self.assertEqual(session.state().game_tds, 0)
        self.assertEqual(session.state().away_score + session.state().home_score, 0)
        self.assertIn("Enter confirms", msg)

    def test_re_adds_qb_rec_does_not_double(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        q_td = parse_play_query("shakir td")
        draft, cands, _ = arm_td_draft(session, rows, q_td, intent=None)
        self.assertFalse(any("PASSTDS" in c.market_id for c in cands))
        q_re = parse_play_query("shakir td re")
        draft, cands, _ = arm_td_draft(
            session, rows, q_re, previous=draft, intent="receiving"
        )
        ids = {c.market_id for c in cands}
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", ids)
        self.assertEqual(session.state().game_tds, 0)
        q_rec = parse_play_query("shakir td rec")
        draft2, cands2, _ = arm_td_draft(
            session, rows, q_rec, previous=draft, intent="receiving"
        )
        self.assertEqual({c.market_id for c in cands2}, ids)
        self.assertEqual(session.state().home_score + session.state().away_score, 0)

    def test_qb_from_ticker_team_when_cache_has_no_uuid(self) -> None:
        rows = [
            row(
                "KXNFLFIRSTTD-26SEP24ATLGB-GBCWATSON9",
                "Christian Watson: 1st Touchdown",
                series="KXNFLFIRSTTD",
            ),
            row(
                "KXNFLTD-26SEP24ATLGB-GBCWATSON9-1",
                "Christian Watson: 1+ touchdowns",
                series="KXNFLTD",
            ),
            row(
                "KXNFLPASSTDS-26SEP24ATLGB-GBJLOVE10-1",
                "Jordan Love: 1+ passing touchdowns",
                series="KXNFLPASSTDS",
            ),
            row(
                "KXNFLPASSTDS-26SEP24ATLGB-ATLMRILEY9-1",
                "Michael Penix: 1+ passing touchdowns",
                series="KXNFLPASSTDS",
            ),
            row(
                "KXNFL1QTOTAL-26SEP24ATLGB-7",
                "Will there be over 6.5 1Q points scored?",
                series="KXNFL1QTOTAL",
                floor=6.5,
            ),
        ]
        session = make_browse_session("26SEP24ATLGB", rows)
        _draft, cands, msg = arm_td_draft(
            session, rows, parse_play_query("watson td re"), intent="receiving"
        )
        ids = {c.market_id for c in cands}
        self.assertIn("KXNFLPASSTDS-26SEP24ATLGB-GBJLOVE10-1", ids)
        self.assertNotIn("KXNFLPASSTDS-26SEP24ATLGB-ATLMRILEY9-1", ids)
        self.assertNotIn("KXNFL1QTOTAL-26SEP24ATLGB-7", ids)
        self.assertIn("QB pass armed", msg)

    def test_pat_adds_q_total_and_one_point(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        q = parse_play_query("shakir td re pat")
        self.assertTrue(q.pat)
        draft, cands, msg = arm_td_draft(
            session, rows, q, intent="receiving", include_pat=True
        )
        ids = {c.market_id for c in cands}
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", ids)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", ids)
        self.assertIn("Q 6.5 armed", msg)
        assert draft is not None
        apply_td_draft(session, draft)
        self.assertEqual(session.state().home_score + session.state().away_score, 7)

    def test_rush_omits_qb(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        _draft, cands, _ = arm_td_draft(
            session, rows, parse_play_query("shakir td ru"), intent="rush"
        )
        ids = {c.market_id for c in cands}
        self.assertTrue(any("FIRSTTD" in i for i in ids))
        self.assertFalse(any("PASSTDS" in i for i in ids))

    def test_confirm_applies_one_td_then_next_is_2plus(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        draft, _cands, _ = arm_td_draft(
            session, rows, parse_play_query("shakir td rec"), intent="receiving"
        )
        assert draft is not None
        apply_td_draft(session, draft)
        st = session.state()
        self.assertEqual(st.game_tds, 1)
        self.assertEqual(st.home_score + st.away_score, 6)
        draft2, cands2, _ = arm_td_draft(
            session, rows, parse_play_query("shakir td rec"), intent="receiving"
        )
        ids = {c.market_id for c in cands2}
        self.assertNotIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", ids)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-2", ids)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", ids)
        self.assertEqual(session.state().game_tds, 1)

    def test_qend_no_on_missed_overs_and_advances_quarter(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        cands, msg = ingest_quarter_end(session, auto_arm=False)
        ids = {c.market_id for c in cands}
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", ids)
        self.assertEqual(session.state().quarter, 2)
        self.assertIn("now Q2", msg)


if __name__ == "__main__":
    unittest.main()
