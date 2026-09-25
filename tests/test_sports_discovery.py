#!/usr/bin/env python3
"""Candidate series lists. No network."""

from __future__ import annotations

import unittest

from kalshi_sports_trader import build_candidate_series, league_prefix_from_series


class TestDiscovery(unittest.TestCase):
    def test_ncaaf_is_not_moneyline_only(self) -> None:
        prefix = league_prefix_from_series("KXNCAAFGAME")
        self.assertEqual(prefix, "KXNCAAF")
        cands = build_candidate_series(
            "https://unused.example",
            league_prefix=prefix,
            seed_series="KXNCAAFGAME",
            explicit_series=None,
            include_season_long=False,
            scan_all_series=False,
        )
        self.assertIn("KXNCAAFGAME", cands)
        self.assertIn("KXNCAAFSPREAD", cands)
        self.assertIn("KXNCAAFTOTAL", cands)
        self.assertIn("KXNCAAF1QTOTAL", cands)
        self.assertIn("KXNCAAFOT", cands)
        self.assertGreater(len(cands), 10)

    def test_nfl_still_has_priority_list(self) -> None:
        cands = build_candidate_series(
            "https://unused.example",
            league_prefix="KXNFL",
            seed_series="KXNFLGAME",
            explicit_series=None,
            include_season_long=False,
            scan_all_series=False,
        )
        self.assertIn("KXNFLFIRSTTD", cands)
        self.assertIn("KXNFLFG", cands)


if __name__ == "__main__":
    unittest.main()
