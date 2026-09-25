#!/usr/bin/env python3
"""ASCII banner files. No network."""

from __future__ import annotations

import unittest

from sports_engine.banner import load_banner


class TestBanner(unittest.TestCase):
    def test_default_has_title(self) -> None:
        text = load_banner()
        self.assertIn("KALSHI SPORTS TRADER", text)
        self.assertGreater(len(text.splitlines()), 5)

    def test_swap_heisman(self) -> None:
        text = load_banner("heisman")
        self.assertIn("KALSHI SPORTS TRADER", text)
        self.assertNotEqual(text, load_banner("football"))


if __name__ == "__main__":
    unittest.main()
