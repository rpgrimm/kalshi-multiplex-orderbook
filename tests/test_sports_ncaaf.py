#!/usr/bin/env python3
"""College team TD cluster. No network."""

from __future__ import annotations

import argparse
import unittest
from types import SimpleNamespace

from kalshi_sports_trader import make_headless_state, run_key_script
from sports_engine.browse import (
    apply_extra_draft,
    apply_extra_miss,
    apply_td_draft,
    arm_extra_draft,
    arm_ncaaf_td_draft,
    make_browse_session,
)
from sports_engine.catalog import teams_from_game_code
from sports_engine.play_protocol import parse_play_query


def row(ticker, title, *, series, floor=None):
    raw: dict = {}
    if floor is not None:
        raw["floor_strike"] = floor
    return SimpleNamespace(
        ticker=ticker.upper(),
        title=title,
        series_ticker=series.upper(),
        yes_sub_title=title,
        raw=raw,
    )


def fixture():
    g = "26SEP26MISSFLA"
    return [
        row(f"KXNCAAFFIRSTTDTEAM-{g}-FLA", "Florida scores first TD", series="KXNCAAFFIRSTTDTEAM"),
        row(f"KXNCAAFFIRSTTDTEAM-{g}-MISS", "Ole Miss scores first TD", series="KXNCAAFFIRSTTDTEAM"),
        row(f"KXNCAAFFIRSTTDTEAM-{g}-NONE", "No team scores a TD", series="KXNCAAFFIRSTTDTEAM"),
        row(f"KXNCAAFDSTTD-{g}-Y", "1+ defensive or special teams touchdowns", series="KXNCAAFDSTTD", floor=0.5),
        row(f"KXNCAAFTEAMRECTD-{g}-FLA2", "Florida: 2+ receiving touchdowns", series="KXNCAAFTEAMRECTD", floor=1.5),
        row(f"KXNCAAFTEAMRECTD-{g}-MISS2", "Ole Miss: 2+ receiving touchdowns", series="KXNCAAFTEAMRECTD", floor=1.5),
        row(f"KXNCAAF1QTOTAL-{g}-3", "Over 2.5 1Q points scored", series="KXNCAAF1QTOTAL", floor=2.5),
        row(f"KXNCAAF1QTOTAL-{g}-6", "Over 5.5 1Q points scored", series="KXNCAAF1QTOTAL", floor=5.5),
        row(f"KXNCAAF1QTOTAL-{g}-8", "Over 7.5 1Q points scored", series="KXNCAAF1QTOTAL", floor=7.5),
        row(f"KXNCAAF1HTOTAL-{g}-3", "Over 2.5 1H points scored", series="KXNCAAF1HTOTAL", floor=2.5),
        row(f"KXNCAAF1HTOTAL-{g}-7", "Over 6.5 1H points scored", series="KXNCAAF1HTOTAL", floor=6.5),
        row(f"KXNCAAF1HTEAMTOTAL-{g}-FLA5", "Florida scores over 4.5 1H points", series="KXNCAAF1HTEAMTOTAL", floor=4.5),
        row(f"KXNCAAF1HTEAMTOTAL-{g}-FLA8", "Florida scores over 7.5 1H points", series="KXNCAAF1HTEAMTOTAL", floor=7.5),
        row(f"KXNCAAFGAME-{g}-FLA", "Florida wins", series="KXNCAAFGAME"),
        row(f"KXNCAAFGAME-{g}-MISS", "Ole Miss wins", series="KXNCAAFGAME"),
    ]


def args():
    return argparse.Namespace(live=False, count=1, count_yes=1, slippage_cents=1)


class TestNcaafTd(unittest.TestCase):
    def test_game_code_miss_fla(self) -> None:
        self.assertEqual(teams_from_game_code("26SEP26MISSFLA"), ("MISS", "FLA"))
        q = parse_play_query("f td d")
        self.assertEqual(q.kind, "td")
        self.assertEqual(q.intent, "defense")
        self.assertEqual(q.name_tokens, ("f",))

    def test_f_td_arms_first_td_and_cleared_overs(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        self.assertEqual((session.state().away, session.state().home), ("MISS", "FLA"))
        draft, cands, _ = arm_ncaaf_td_draft(session, rows, parse_play_query("f td"))
        by = {(c.side.value, c.market_id.split("-")[-1]) for c in cands}
        self.assertIn(("yes", "FLA"), by)
        self.assertIn(("no", "MISS"), by)
        self.assertIn(("no", "NONE"), by)
        self.assertIn(("yes", "3"), by)  # 2.5 1Q
        self.assertIn(("yes", "6"), by)  # 5.5 1Q
        self.assertNotIn(("yes", "8"), by)  # 7.5 needs PAT
        ids = {c.market_id for c in cands}
        self.assertTrue(any("1HTOTAL" in i and i.endswith("-3") for i in ids))
        self.assertTrue(any(i.endswith("-FLA5") for i in ids))
        self.assertFalse(any(i.endswith("-FLA8") for i in ids))
        self.assertFalse(any("DSTTD" in i for i in ids))
        self.assertFalse(any("TEAMRECTD" in i for i in ids))
        self.assertEqual(session.state().game_tds, 0)

    def test_td_d_adds_dst(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        _draft, cands, _ = arm_ncaaf_td_draft(
            session, rows, parse_play_query("f td d"), intent="defense"
        )
        self.assertTrue(any("DSTTD" in c.market_id for c in cands))

    def test_re_notes_state_second_rec_is_2plus(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        draft, cands, msg = arm_ncaaf_td_draft(
            session, rows, parse_play_query("f td re"), intent="receiving"
        )
        self.assertFalse(any("TEAMRECTD" in c.market_id for c in cands))
        self.assertIn("receiving", msg)
        assert draft is not None
        apply_td_draft(session, draft)
        st = session.state()
        self.assertEqual(st.home_score, 6)
        self.assertEqual(st.team_rec_tds.get("fla"), 1)
        draft2, cands2, _ = arm_ncaaf_td_draft(
            session, rows, parse_play_query("f td re"), intent="receiving"
        )
        ids = {c.market_id for c in cands2}
        self.assertTrue(any(i.endswith("-FLA2") for i in ids))
        self.assertFalse(any("FIRSTTDTEAM" in i for i in ids))

    def test_script_and_hide_sent(self) -> None:
        state = make_headless_state(
            seed_series="KXNCAAFGAME",
            game_code="26SEP26MISSFLA",
            rows=fixture(),
            args=args(),
        )
        report = run_key_script(state, "/ f td Enter Enter")
        confirms = [e for e in report["log"] if e["event"] == "confirm"]
        sent = {(b["side"], b["ticker"].split("-")[-1]) for b in confirms[0]["would_send"]}
        self.assertIn(("yes", "FLA"), sent)
        self.assertIn(("no", "MISS"), sent)
        self.assertIn(("no", "NONE"), sent)
        self.assertEqual(report["filter"].strip(), "")
        self.assertEqual(report["state"]["home_score"], 6)
        hidden = state.session.sent_markets
        self.assertTrue(any("FIRSTTDTEAM" in t and t.endswith("-FLA") for t in hidden))
        report2 = run_key_script(state, "/ f td Enter")
        drafts = [e for e in report2["log"] if e["event"] == "draft"]
        armed = {a["ticker"] for a in drafts[-1]["armed"]}
        self.assertFalse(any("FIRSTTDTEAM" in t for t in armed))

    def test_pat_after_td_clears_1h_65_not_q75(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        draft, _, _ = arm_ncaaf_td_draft(session, rows, parse_play_query("f td"))
        assert draft is not None
        apply_td_draft(session, draft)
        self.assertEqual(session.pending_extra_team, "FLA")
        extra, cands, _ = arm_extra_draft(session, rows, "pat")
        ids = {c.market_id for c in cands}
        self.assertTrue(any(i.endswith("-7") and "1HTOTAL" in i for i in ids))
        self.assertFalse(any(i.endswith("-8") and "1QTOTAL" in i for i in ids))
        assert extra is not None
        apply_extra_draft(session, extra)
        self.assertEqual(session.state().home_score, 7)
        self.assertIsNone(session.pending_extra_team)

    def test_2pt_clears_q75_and_team_1h_75(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        draft, _, _ = arm_ncaaf_td_draft(session, rows, parse_play_query("f td"))
        assert draft is not None
        apply_td_draft(session, draft)
        extra, cands, _ = arm_extra_draft(session, rows, "2pt")
        ids = {c.market_id for c in cands}
        self.assertTrue(any(i.endswith("-8") and "1QTOTAL" in i for i in ids))
        self.assertTrue(any(i.endswith("-FLA8") for i in ids))
        assert extra is not None
        apply_extra_draft(session, extra)
        self.assertEqual(session.state().home_score, 8)

    def test_nopat_keeps_score_at_6(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        draft, _, _ = arm_ncaaf_td_draft(session, rows, parse_play_query("f td"))
        assert draft is not None
        apply_td_draft(session, draft)
        apply_extra_miss(session, "nopat")
        self.assertEqual(session.state().home_score, 6)
        self.assertIsNone(session.pending_extra_team)

    def test_script_pat_after_td(self) -> None:
        state = make_headless_state(
            seed_series="KXNCAAFGAME",
            game_code="26SEP26MISSFLA",
            rows=fixture(),
            args=args(),
        )
        report = run_key_script(state, "/ f td Enter Enter / pat Enter Enter")
        self.assertEqual(report["state"]["home_score"], 7)
        confirms = [e for e in report["log"] if e["event"] == "confirm"]
        last = {b["ticker"] for b in confirms[-1]["would_send"]}
        self.assertTrue(any("1HTOTAL" in t and t.endswith("-7") for t in last))


if __name__ == "__main__":
    unittest.main()
