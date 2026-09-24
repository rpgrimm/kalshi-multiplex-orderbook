#!/usr/bin/env python3
"""Play protocol, TD cluster, and quarter-end. No network."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sports_engine.browse import ingest_quarter_end, ingest_td_play, make_browse_session
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
        self.assertTrue(parse_play_query("shakir td rec").is_complete_td())
        self.assertFalse(parse_play_query("shakir td").is_complete_td())

    def test_tab_completes_last_name(self) -> None:
        names = player_last_names(fixture())
        text, hits = tab_complete_player("sha", names)
        self.assertEqual(text, "shakir")
        self.assertEqual(hits, ["shakir"])

    def test_first_rec_td_constructs_first_plus_qb_plus_q1(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        q = parse_play_query("shakir td rec")
        cands, msg = ingest_td_play(session, rows, q, auto_arm=True)
        ids = {c.market_id for c in cands}
        self.assertIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", ids)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", ids)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", ids)
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", ids)
        self.assertNotIn("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", ids)
        self.assertIn("armed", msg)
        self.assertEqual(len(session.arming.armed_bets()), len(cands))
        st = session.state()
        self.assertEqual(st.game_tds, 1)
        self.assertEqual(st.home_score + st.away_score, 6)

    def test_rush_omits_qb(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        cands, _ = ingest_td_play(session, rows, parse_play_query("shakir td ru"), auto_arm=False)
        ids = {c.market_id for c in cands}
        self.assertTrue(any("FIRSTTD" in i for i in ids))
        self.assertFalse(any("PASSTDS" in i for i in ids))

    def test_second_rec_td_is_2plus_not_first(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP17DETBUF", rows)
        ingest_td_play(session, rows, parse_play_query("shakir td rec"), auto_arm=False)
        cands, _ = ingest_td_play(session, rows, parse_play_query("shakir td rec"), auto_arm=False)
        ids = {c.market_id for c in cands}
        self.assertNotIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", ids)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-2", ids)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", ids)

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
