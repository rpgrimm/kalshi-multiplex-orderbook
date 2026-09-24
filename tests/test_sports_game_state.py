#!/usr/bin/env python3
"""Unit tests for in-game sports state (PACK-001 follow-on). No network."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from kalshi_sports_game_state import (
    GameState,
    parse_play_command,
    parse_score_command,
    resolve_end_quarter_no_bets,
    resolve_play_bets,
)

BUF = "f9acd396-ba35-4cae-a431-a44b2af707b0"
DET = "f95a55a7-8472-4ffa-be20-14547e3c32ff"


def row(ticker: str, title: str, *, series: str, team: str | None = None, floor: float | None = None, player: str | None = None):
    raw: dict = {}
    if floor is not None:
        raw["floor_strike"] = floor
    custom = {}
    if team:
        custom["football_team"] = team
    if player:
        custom["football_player"] = player
    if custom:
        raw["custom"] = custom
    return SimpleNamespace(
        ticker=ticker.upper(),
        title=title,
        series_ticker=series.upper(),
        yes_sub_title=title.split(":")[0],
        no_sub_title="",
        event_ticker="",
        status="active",
        raw=raw,
    )


def fixture() -> list:
    return [
        row("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", "Khalil Shakir: 1st Touchdown", series="KXNFLFIRSTTD", team=BUF, player="shakir"),
        row("KXNFLFIRSTTD-26SEP17DETBUF-DETJGOFF16", "Jared Goff: 1st Touchdown", series="KXNFLFIRSTTD", team=DET, player="goff"),
        row("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", "Khalil Shakir: 1+ touchdowns", series="KXNFLTD", team=BUF, floor=0.5, player="shakir"),
        row("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-2", "Khalil Shakir: 2+ touchdowns", series="KXNFLTD", team=BUF, floor=1.5, player="shakir"),
        row("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", "Josh Allen: 1+ passing touchdowns", series="KXNFLPASSTDS", team=BUF, floor=0.5, player="allen"),
        row("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", "Josh Allen: 2+ passing touchdowns", series="KXNFLPASSTDS", team=BUF, floor=1.5, player="allen"),
        row("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", "Jared Goff: 1+ passing touchdowns", series="KXNFLPASSTDS", team=DET, floor=0.5, player="goff"),
        row("KXNFL1QTOTAL-26SEP17DETBUF-7", "Will there be over 6.5 1Q points scored?", series="KXNFL1QTOTAL", floor=6.5),
        row("KXNFL1QTOTAL-26SEP17DETBUF-8", "Will there be over 7.5 1Q points scored?", series="KXNFL1QTOTAL", floor=7.5),
        row("KXNFL1QTOTAL-26SEP17DETBUF-4", "Will there be over 3.5 1Q points scored?", series="KXNFL1QTOTAL", floor=3.5),
        row("KXNFL2QTOTAL-26SEP17DETBUF-7", "Will there be over 6.5 2Q points scored?", series="KXNFL2QTOTAL", floor=6.5),
    ]


class TestGameState(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = fixture()
        self.state = GameState.from_game_code("26SEP17DETBUF")

    def test_parse_play_and_score(self) -> None:
        self.assertEqual(parse_play_command("shakir td re"), (["shakir"], "receiving"))
        self.assertEqual(parse_play_command("shakir td ru"), (["shakir"], "rush"))
        self.assertIsNone(parse_play_command("shakir td"))
        self.assertEqual(parse_score_command("det 7", self.state), ("DET", 7))
        self.assertEqual(parse_score_command("buf=14", self.state), ("BUF", 14))
        self.assertIsNone(parse_score_command("sea 3", self.state))

    def test_first_rec_td_arms_first_plus_1_plus_qb1_plus_q1(self) -> None:
        bets, msg = resolve_play_bets(
            state=self.state, rows=self.rows, player_tokens=["shakir"], intent="receiving"
        )
        self.assertIn("ARMED", msg)
        tickers = {b.ticker for b in bets}
        self.assertIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", tickers)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-1", tickers)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", tickers)
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", tickers)
        self.assertNotIn("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", tickers)
        self.assertEqual(self.state.players[list(self.state.players)[0]].rec_td, 1)
        self.assertEqual(self.state.game_tds, 1)

    def test_second_rec_td_arms_2plus_and_qb2_not_first(self) -> None:
        resolve_play_bets(state=self.state, rows=self.rows, player_tokens=["shakir"], intent="receiving")
        bets, msg = resolve_play_bets(
            state=self.state, rows=self.rows, player_tokens=["shakir"], intent="receiving"
        )
        self.assertIn("ARMED", msg)
        tickers = {b.ticker for b in bets}
        self.assertNotIn("KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10", tickers)
        self.assertIn("KXNFLTD-26SEP17DETBUF-BUFKSHAKIR10-2", tickers)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", tickers)
        self.assertNotIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", tickers)
        # Q1 6.5 already armed
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-7", tickers)

    def test_rush_omits_qb(self) -> None:
        bets, _msg = resolve_play_bets(
            state=self.state, rows=self.rows, player_tokens=["shakir"], intent="rush"
        )
        tickers = {b.ticker for b in bets}
        self.assertTrue(any("FIRSTTD" in t for t in tickers))
        self.assertFalse(any("PASSTDS" in t for t in tickers))

    def test_end_quarter_no_on_overs_that_missed(self) -> None:
        self.state.set_score("DET", 3)
        nos = resolve_end_quarter_no_bets(state=self.state, rows=self.rows)
        tickers = {b.ticker for b in nos}
        self.assertTrue(all(b.side == "buy_no" for b in nos))
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-4", tickers)  # 3.5 did not hit
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", tickers)
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-8", tickers)

        st2 = GameState.from_game_code("26SEP17DETBUF")
        st2.set_score("BUF", 7)
        nos2 = resolve_end_quarter_no_bets(state=st2, rows=self.rows)
        t2 = {b.ticker for b in nos2}
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-4", t2)  # 3.5 hit
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-7", t2)  # 6.5 hit (7>6.5)
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-8", t2)  # 7.5 did not

    def test_header_includes_quarter_and_score(self) -> None:
        self.state.set_score("DET", 7)
        self.state.set_score("BUF", 14)
        line = self.state.header_line()
        self.assertIn("Q1", line)
        self.assertIn("DET 7-14 BUF", line)


if __name__ == "__main__":
    unittest.main()
