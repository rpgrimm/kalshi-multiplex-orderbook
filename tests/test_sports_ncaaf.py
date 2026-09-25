#!/usr/bin/env python3
"""College team TD cluster. No network."""

from __future__ import annotations

import argparse
import unittest
from types import SimpleNamespace

from kalshi_sports_trader import make_headless_state, run_key_script
from sports_engine.models import EventType, GameEvent
from sports_engine.browse import (
    apply_extra_draft,
    apply_extra_miss,
    apply_fg_draft,
    apply_qend_draft,
    apply_td_draft,
    arm_extra_draft,
    arm_ncaaf_fg_draft,
    arm_ncaaf_td_draft,
    arm_qend_draft,
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
        row(f"KXNCAAFSPREAD-{g}-FLA4", "Florida wins by over 3.5 points", series="KXNCAAFSPREAD", floor=3.5),
        row(f"KXNCAAFSPREAD-{g}-FLA7", "Florida wins by over 6.5 points", series="KXNCAAFSPREAD", floor=6.5),
        row(f"KXNCAAFSPREAD-{g}-MISS4", "Ole Miss wins by over 3.5 points", series="KXNCAAFSPREAD", floor=3.5),
        row(f"KXNCAAFTOTAL-{g}-60", "Over 59.5 points scored", series="KXNCAAFTOTAL", floor=59.5),
        row(f"KXNCAAFTEAMTOTAL-{g}-FLA31", "Florida scores over 30.5 points", series="KXNCAAFTEAMTOTAL", floor=30.5),
        row(f"KXNCAAFTEAMTOTAL-{g}-MISS28", "Ole Miss scores over 27.5 points", series="KXNCAAFTEAMTOTAL", floor=27.5),
        row(f"KXNCAAF4Q-{g}-FLA", "Florida wins the 4th quarter", series="KXNCAAF4Q"),
        row(f"KXNCAAF2H-{g}-FLA", "Florida wins the 2nd half", series="KXNCAAF2H"),
        row(f"KXNCAAF1Q-{g}-FLA", "Florida wins the 1st quarter", series="KXNCAAF1Q"),
        row(f"KXNCAAF1Q-{g}-MISS", "Ole Miss wins the 1st quarter", series="KXNCAAF1Q"),
        row(f"KXNCAAF1Q-{g}-TIE", "1st quarter tie", series="KXNCAAF1Q"),
        row(f"KXNCAAF2Q-{g}-FLA", "Florida wins the 2nd quarter", series="KXNCAAF2Q"),
        row(f"KXNCAAF2Q-{g}-MISS", "Ole Miss wins the 2nd quarter", series="KXNCAAF2Q"),
        row(f"KXNCAAF2Q-{g}-TIE", "2nd quarter tie", series="KXNCAAF2Q"),
        row(f"KXNCAAF1QSPREAD-{g}-FLA3", "Florida wins 1Q by over 2.5 points", series="KXNCAAF1QSPREAD", floor=2.5),
        row(f"KXNCAAF1QSPREAD-{g}-FLA4", "Florida wins 1Q by over 3.5 points", series="KXNCAAF1QSPREAD", floor=3.5),
        row(f"KXNCAAF1QSPREAD-{g}-FLA7", "Florida wins 1Q by over 6.5 points", series="KXNCAAF1QSPREAD", floor=6.5),
        row(f"KXNCAAF1QSPREAD-{g}-FLA8", "Florida wins 1Q by over 7.5 points", series="KXNCAAF1QSPREAD", floor=7.5),
        row(f"KXNCAAF1QSPREAD-{g}-MISS3", "Ole Miss wins 1Q by over 2.5 points", series="KXNCAAF1QSPREAD", floor=2.5),
        row(f"KXNCAAF1QSPREAD-{g}-MISS7", "Ole Miss wins 1Q by over 6.5 points", series="KXNCAAF1QSPREAD", floor=6.5),
        row(f"KXNCAAF1H-{g}-FLA", "Florida wins the 1st half", series="KXNCAAF1H"),
        row(f"KXNCAAF1H-{g}-MISS", "Ole Miss wins the 1st half", series="KXNCAAF1H"),
        row(f"KXNCAAF1H-{g}-TIE", "Tie in the 1st half", series="KXNCAAF1H"),
        row(f"KXNCAAF1HSPREAD-{g}-FLA5", "Florida wins 1H by over 4.5 points", series="KXNCAAF1HSPREAD", floor=4.5),
        row(f"KXNCAAF1HSPREAD-{g}-FLA7", "Florida wins 1H by over 6.5 points", series="KXNCAAF1HSPREAD", floor=6.5),
        row(f"KXNCAAF1HSPREAD-{g}-MISS5", "Ole Miss wins 1H by over 4.5 points", series="KXNCAAF1HSPREAD", floor=4.5),
        row(f"KXNCAAF1HTOTAL-{g}-14", "Over 13.5 1H points scored", series="KXNCAAF1HTOTAL", floor=13.5),
        row(f"KXNCAAF1HTEAMTOTAL-{g}-MISS6", "Ole Miss scores over 5.5 1H points", series="KXNCAAF1HTEAMTOTAL", floor=5.5),
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

    def test_qend_florida_7_0(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        td, _, _ = arm_ncaaf_td_draft(session, rows, parse_play_query("f td"))
        assert td is not None
        apply_td_draft(session, td)
        extra, _, _ = arm_extra_draft(session, rows, "pat")
        assert extra is not None
        apply_extra_draft(session, extra)
        self.assertEqual(session.state().home_score, 7)
        qend, cands, _ = arm_qend_draft(session, rows)
        by = {(c.side.value, c.market_id.split("-")[-1], "SPREAD" in c.market_id, "TOTAL" in c.market_id) for c in cands}
        ids = {c.market_id: c.side.value for c in cands}
        self.assertEqual(ids.get("KXNCAAF1Q-26SEP26MISSFLA-FLA"), "yes")
        self.assertEqual(ids.get("KXNCAAF1Q-26SEP26MISSFLA-MISS"), "no")
        self.assertEqual(ids.get("KXNCAAF1Q-26SEP26MISSFLA-TIE"), "no")
        self.assertEqual(ids.get("KXNCAAF1QSPREAD-26SEP26MISSFLA-FLA7"), "yes")
        self.assertEqual(ids.get("KXNCAAF1QSPREAD-26SEP26MISSFLA-FLA8"), "no")
        self.assertEqual(ids.get("KXNCAAF1QSPREAD-26SEP26MISSFLA-MISS3"), "no")
        self.assertEqual(ids.get("KXNCAAF1QTOTAL-26SEP26MISSFLA-6"), "yes")
        self.assertEqual(ids.get("KXNCAAF1QTOTAL-26SEP26MISSFLA-8"), "no")
        self.assertFalse(any("1HSPREAD" in i or i.startswith("KXNCAAF1H-") for i in ids))
        self.assertFalse(any(i.startswith("KXNCAAFGAME-") or "KXNCAAFSPREAD" in i for i in ids))
        self.assertEqual(session.state().quarter, 1)
        assert qend is not None
        apply_qend_draft(session, qend)
        self.assertEqual(session.state().quarter, 2)

    def test_script_qend(self) -> None:
        state = make_headless_state(
            seed_series="KXNCAAFGAME",
            game_code="26SEP26MISSFLA",
            rows=fixture(),
            args=args(),
        )
        report = run_key_script(state, "/ qend Enter Enter")
        self.assertEqual(report["state"]["quarter"], 2)
        confirms = [e for e in report["log"] if e["event"] == "confirm"]
        sent = {b["ticker"] for b in confirms[0]["would_send"]}
        self.assertTrue(any(t.endswith("-TIE") for t in sent))

    def test_q2_end_settles_first_half(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        session.ingest(
            GameEvent(type=EventType.QUARTER, quarter=2, source="test"),
            evaluate=False,
        )
        session.ingest(
            GameEvent(
                type=EventType.SCORE,
                team="MISS",
                payload={"set": 6},
                source="test",
            ),
            evaluate=False,
        )
        session.ingest(
            GameEvent(
                type=EventType.SCORE,
                team="FLA",
                payload={"set": 14},
                source="test",
            ),
            evaluate=False,
        )
        self.assertEqual(session.state().quarter, 2)
        _draft, cands, msg = arm_qend_draft(session, rows)
        ids = {c.market_id: c.side.value for c in cands}
        self.assertIn("1H", msg)
        self.assertEqual(ids.get("KXNCAAF1H-26SEP26MISSFLA-FLA"), "yes")
        self.assertEqual(ids.get("KXNCAAF1H-26SEP26MISSFLA-MISS"), "no")
        self.assertEqual(ids.get("KXNCAAF1H-26SEP26MISSFLA-TIE"), "no")
        self.assertEqual(ids.get("KXNCAAF1HSPREAD-26SEP26MISSFLA-FLA7"), "yes")
        self.assertEqual(ids.get("KXNCAAF1HSPREAD-26SEP26MISSFLA-MISS5"), "no")
        self.assertEqual(ids.get("KXNCAAF1HTOTAL-26SEP26MISSFLA-14"), "yes")
        self.assertEqual(ids.get("KXNCAAF1HTEAMTOTAL-26SEP26MISSFLA-MISS6"), "yes")
        self.assertTrue(any("2Q" in t for t in ids))

    def test_f_fg_is_plus_3_clears_q25_not_q55(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        draft, cands, _ = arm_ncaaf_fg_draft(session, rows, parse_play_query("f fg"))
        ids = {c.market_id for c in cands}
        self.assertTrue(any(i.endswith("-3") and "1QTOTAL" in i for i in ids))
        self.assertFalse(any(i.endswith("-6") and "1QTOTAL" in i for i in ids))
        self.assertEqual(session.state().home_score, 0)
        assert draft is not None
        apply_fg_draft(session, draft)
        self.assertEqual(session.state().home_score, 3)
        self.assertIsNone(session.pending_extra_team)

    def test_q4_qend_settles_game_if_not_tied(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        session.ingest(GameEvent(type=EventType.QUARTER, quarter=4, source="test"), evaluate=False)
        session.ingest(
            GameEvent(type=EventType.SCORE, team="MISS", payload={"set": 27}, source="test"),
            evaluate=False,
        )
        session.ingest(
            GameEvent(type=EventType.SCORE, team="FLA", payload={"set": 31}, source="test"),
            evaluate=False,
        )
        draft, cands, msg = arm_qend_draft(session, rows)
        ids = {c.market_id: c.side.value for c in cands}
        self.assertIn("2H+game", msg)
        self.assertEqual(ids.get("KXNCAAFGAME-26SEP26MISSFLA-FLA"), "yes")
        self.assertEqual(ids.get("KXNCAAFGAME-26SEP26MISSFLA-MISS"), "no")
        self.assertEqual(ids.get("KXNCAAFSPREAD-26SEP26MISSFLA-FLA4"), "yes")
        self.assertEqual(ids.get("KXNCAAFSPREAD-26SEP26MISSFLA-FLA7"), "no")
        self.assertEqual(ids.get("KXNCAAFSPREAD-26SEP26MISSFLA-MISS4"), "no")
        self.assertEqual(ids.get("KXNCAAFTOTAL-26SEP26MISSFLA-60"), "no")  # 58 not over 59.5
        self.assertEqual(ids.get("KXNCAAFTEAMTOTAL-26SEP26MISSFLA-FLA31"), "yes")
        self.assertEqual(ids.get("KXNCAAFTEAMTOTAL-26SEP26MISSFLA-MISS28"), "no")
        assert draft is not None
        apply_qend_draft(session, draft)
        self.assertEqual(session.state().quarter, 4)

    def test_q4_tie_goes_to_ot_without_game_settle(self) -> None:
        rows = fixture()
        session = make_browse_session("26SEP26MISSFLA", rows)
        session.ingest(GameEvent(type=EventType.QUARTER, quarter=4, source="test"), evaluate=False)
        session.ingest(
            GameEvent(type=EventType.SCORE, team="MISS", payload={"set": 24}, source="test"),
            evaluate=False,
        )
        session.ingest(
            GameEvent(type=EventType.SCORE, team="FLA", payload={"set": 24}, source="test"),
            evaluate=False,
        )
        draft, cands, msg = arm_qend_draft(session, rows)
        ids = {c.market_id for c in cands}
        self.assertIn("OT", msg)
        self.assertFalse(any(i.startswith("KXNCAAFGAME-") for i in ids))
        self.assertFalse(any("KXNCAAFSPREAD" in i for i in ids))
        assert draft is not None
        apply_qend_draft(session, draft)
        self.assertEqual(session.state().quarter, 5)
        session.ingest(
            GameEvent(type=EventType.SCORE, team="FLA", payload={"set": 27}, source="test"),
            evaluate=False,
        )
        _d, cands2, msg2 = arm_qend_draft(session, rows)
        ids2 = {c.market_id: c.side.value for c in cands2}
        self.assertIn("game", msg2)
        self.assertEqual(ids2.get("KXNCAAFGAME-26SEP26MISSFLA-FLA"), "yes")
        self.assertEqual(ids2.get("KXNCAAFSPREAD-26SEP26MISSFLA-FLA4"), "no")  # 3-point win


if __name__ == "__main__":
    unittest.main()
