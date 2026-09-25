#!/usr/bin/env python3
"""NCAAF scrolling prompt. No TTY."""

from __future__ import annotations

import unittest

from kalshi_sports_trader import handle_prompt_line, make_headless_state
from tests.test_sports_ncaaf import args, fixture


class TestNcaafPrompt(unittest.TestCase):
    def _state(self):
        state = make_headless_state(
            seed_series="KXNCAAFGAME",
            game_code="26SEP26MISSFLA",
            rows=fixture(),
            args=args(),
        )
        state.script_mode = False
        return state

    def test_f_td_then_empty_enter_sends(self) -> None:
        state = self._state()
        armed = handle_prompt_line(state, "f td")
        self.assertTrue(any(line.startswith("READY ") for line in armed))
        self.assertTrue(any("YES" in line and "FLA" in line for line in armed))
        self.assertTrue(any(line.startswith("  NO") and "MISS" in line for line in armed))
        self.assertEqual(state.session.state().home_score, 0)
        sent = handle_prompt_line(state, "")
        self.assertTrue(any("recorded" in line.lower() or "sent" in line.lower() for line in sent))
        self.assertEqual(state.session.state().home_score, 6)
        self.assertIsNone(state.draft)

    def test_pat_and_o_and_no_slash(self) -> None:
        state = self._state()
        handle_prompt_line(state, "f td")
        handle_prompt_line(state, "")
        pat = handle_prompt_line(state, "pat")
        self.assertTrue(any(line.startswith("READY ") for line in pat))
        handle_prompt_line(state, "")
        self.assertEqual(state.session.state().home_score, 7)
        orders = handle_prompt_line(state, "o")
        self.assertTrue(orders[0].startswith("ORDERS") or orders == ["no orders"])
        # dry-run confirm records session bets
        self.assertNotEqual(orders, ["no orders"])

    def test_f_fg(self) -> None:
        state = self._state()
        armed = handle_prompt_line(state, "f fg")
        self.assertTrue(any(line.startswith("READY ") for line in armed))
        handle_prompt_line(state, "")
        self.assertEqual(state.session.state().home_score, 3)

    def test_quit(self) -> None:
        state = self._state()
        self.assertEqual(handle_prompt_line(state, "q"), ["quit"])


if __name__ == "__main__":
    unittest.main()
