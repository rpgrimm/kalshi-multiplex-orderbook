#!/usr/bin/env python3
"""Unit tests for sports bet packages (PACK-001). No network."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kalshi_sports_packages import (
    load_browse_packages,
    load_packages,
    package_for_trigger,
    package_preview_lines,
    resolve_package,
    team_key,
)

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "examples" / "packages" / "first_td_cluster.json"

BUF = "f9acd396-ba35-4cae-a431-a44b2af707b0"
DET = "f95a55a7-8472-4ffa-be20-14547e3c32ff"


def row(
    ticker: str,
    title: str,
    *,
    series: str,
    team: str | None = None,
    floor_strike: float | None = None,
    yes_sub: str = "",
    custom: bool = True,
) -> SimpleNamespace:
    raw: dict = {}
    if floor_strike is not None:
        raw["floor_strike"] = floor_strike
    if team and custom:
        raw["custom"] = {"football_team": team}
    return SimpleNamespace(
        ticker=ticker.upper(),
        title=title,
        series_ticker=series.upper(),
        yes_sub_title=yes_sub,
        no_sub_title="",
        event_ticker="",
        status="active",
        raw=raw,
    )


def det_buf_fixture(*, include_q1: bool = True) -> list[SimpleNamespace]:
    rows = [
        row(
            "KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10",
            "Khalil Shakir: 1st Touchdown",
            series="KXNFLFIRSTTD",
            team=BUF,
        ),
        row(
            "KXNFLFIRSTTD-26SEP17DETBUF-BUFBUFDST",
            "BUF Bills D/ST: 1st Touchdown",
            series="KXNFLFIRSTTD",
            team=BUF,
        ),
        row(
            "KXNFLFIRSTTD-26SEP17DETBUF-DETNO-TD",
            "No Touchdown: 1st Touchdown",
            series="KXNFLFIRSTTD",
            team=DET,
        ),
        row(
            "KXNFLFIRSTTD-26SEP17DETBUF-DETJGOFF16",
            "Jared Goff: 1st Touchdown",
            series="KXNFLFIRSTTD",
            team=DET,
        ),
        row(
            "KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1",
            "Josh Allen: 1+ passing touchdowns",
            series="KXNFLPASSTDS",
            team=BUF,
            floor_strike=0.5,
        ),
        row(
            "KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2",
            "Josh Allen: 2+ passing touchdowns",
            series="KXNFLPASSTDS",
            team=BUF,
            floor_strike=1.5,
        ),
        row(
            "KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1",
            "Jared Goff: 1+ passing touchdowns",
            series="KXNFLPASSTDS",
            team=DET,
            floor_strike=0.5,
        ),
        row(
            "KXNFL1QTOTAL-26SEP17DETBUF-4",
            "Will there be over 3.5 1Q points scored?",
            series="KXNFL1QTOTAL",
            floor_strike=3.5,
        ),
    ]
    if include_q1:
        rows.append(
            row(
                "KXNFL1QTOTAL-26SEP17DETBUF-7",
                "Will there be over 6.5 1Q points scored?",
                series="KXNFL1QTOTAL",
                floor_strike=6.5,
            )
        )
    return rows


def shipped_package():
    packages = load_packages([SHIPPED])
    assert packages, "shipped first_td_cluster.json must load"
    return packages[0]


class TestSportsPackages(unittest.TestCase):
    def setUp(self) -> None:
        self.pkg = shipped_package()
        self.rows = det_buf_fixture()
        self.by_ticker = {r.ticker: r for r in self.rows}

    def test_shipped_json_loads(self) -> None:
        packages = load_packages([SHIPPED])
        self.assertEqual(len(packages), 1)
        pkg = packages[0]
        self.assertEqual(pkg.id, "first_td_cluster")
        self.assertTrue(pkg.enabled)
        self.assertEqual(pkg.trigger.series, "KXNFLFIRSTTD")
        self.assertIn("D/ST", pkg.trigger.exclude_title_substrings)
        self.assertIn("No Touchdown", pkg.trigger.exclude_title_substrings)
        self.assertEqual([leg.id for leg in pkg.legs], [
            "player_first_td",
            "qb_1_pass_td",
            "q1_over_6_5",
        ])

    def test_shakir_resolves_allen_1_and_q1_6_5_not_goff_or_allen_2(self) -> None:
        shakir = self.by_ticker["KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10"]
        self.assertIs(package_for_trigger([self.pkg], shakir), self.pkg)
        resolved = resolve_package(self.pkg, shakir, self.rows)
        tickers = {leg.row.ticker for leg in resolved.resolved_legs()}
        self.assertEqual(
            tickers,
            {
                "KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10",
                "KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1",
                "KXNFL1QTOTAL-26SEP17DETBUF-7",
            },
        )
        self.assertNotIn("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", tickers)
        self.assertNotIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-2", tickers)
        self.assertNotIn("KXNFL1QTOTAL-26SEP17DETBUF-4", tickers)
        self.assertEqual(team_key(shakir), BUF.lower())
        preview = "\n".join(package_preview_lines(resolved))
        self.assertIn("Khalil Shakir: 1st Touchdown", preview)
        self.assertIn("Josh Allen: 1+ passing touchdowns", preview)
        self.assertIn("over 6.5 1Q points", preview)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", preview)
        self.assertNotIn("Jared Goff", preview)
        self.assertNotIn("2+ passing", preview)

    def test_goff_first_td_resolves_goff_1_not_allen(self) -> None:
        goff = self.by_ticker["KXNFLFIRSTTD-26SEP17DETBUF-DETJGOFF16"]
        self.assertIs(package_for_trigger([self.pkg], goff), self.pkg)
        resolved = resolve_package(self.pkg, goff, self.rows)
        tickers = {leg.row.ticker for leg in resolved.resolved_legs()}
        self.assertIn("KXNFLFIRSTTD-26SEP17DETBUF-DETJGOFF16", tickers)
        self.assertIn("KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1", tickers)
        self.assertNotIn("KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1", tickers)
        self.assertIn("KXNFL1QTOTAL-26SEP17DETBUF-7", tickers)

    def test_dst_is_not_a_trigger(self) -> None:
        dst = self.by_ticker["KXNFLFIRSTTD-26SEP17DETBUF-BUFBUFDST"]
        self.assertIsNone(package_for_trigger([self.pkg], dst))

    def test_no_touchdown_is_not_a_trigger(self) -> None:
        none = self.by_ticker["KXNFLFIRSTTD-26SEP17DETBUF-DETNO-TD"]
        self.assertIsNone(package_for_trigger([self.pkg], none))

    def test_missing_1q_still_resolves_trigger_and_qb(self) -> None:
        rows = det_buf_fixture(include_q1=False)
        shakir = next(r for r in rows if r.ticker.endswith("BUFKSHAKIR10"))
        resolved = resolve_package(self.pkg, shakir, rows)
        tickers = {leg.row.ticker for leg in resolved.resolved_legs()}
        self.assertEqual(
            tickers,
            {
                "KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10",
                "KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1",
            },
        )
        missed = resolved.unresolved_legs()
        self.assertEqual([leg.id for leg in missed], ["q1_over_6_5"])
        self.assertFalse(resolved.required_unresolved())

    def test_team_abbrev_fallback_when_custom_missing(self) -> None:
        shakir = row(
            "KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10",
            "Khalil Shakir: 1st Touchdown",
            series="KXNFLFIRSTTD",
            custom=False,
        )
        allen = row(
            "KXNFLPASSTDS-26SEP17DETBUF-BUFJALLEN17-1",
            "Josh Allen: 1+ passing touchdowns",
            series="KXNFLPASSTDS",
            floor_strike=0.5,
            custom=False,
        )
        goff = row(
            "KXNFLPASSTDS-26SEP17DETBUF-DETJGOFF16-1",
            "Jared Goff: 1+ passing touchdowns",
            series="KXNFLPASSTDS",
            floor_strike=0.5,
            custom=False,
        )
        self.assertEqual(team_key(shakir), "BUF")
        resolved = resolve_package(self.pkg, shakir, [shakir, allen, goff])
        tickers = {leg.row.ticker for leg in resolved.resolved_legs()}
        self.assertIn(allen.ticker, tickers)
        self.assertNotIn(goff.ticker, tickers)

    def test_ambiguous_same_team_qbs_are_not_sent(self) -> None:
        shakir = self.by_ticker["KXNFLFIRSTTD-26SEP17DETBUF-BUFKSHAKIR10"]
        extra_qb = row(
            "KXNFLPASSTDS-26SEP17DETBUF-BUFOTHER-1",
            "Other Buf: 1+ passing touchdowns",
            series="KXNFLPASSTDS",
            team=BUF,
            floor_strike=0.5,
        )
        rows = self.rows + [extra_qb]
        resolved = resolve_package(self.pkg, shakir, rows)
        qb = next(leg for leg in resolved.legs if leg.id == "qb_1_pass_td")
        self.assertIsNone(qb.row)
        self.assertEqual(qb.reason, "ambiguous: 2 matches")

    def test_non_first_td_is_not_a_trigger(self) -> None:
        spread = row(
            "KXNFLSPREAD-26SEP17DETBUF-DET",
            "Detroit at Buffalo spread",
            series="KXNFLSPREAD",
        )
        self.assertIsNone(package_for_trigger([self.pkg], spread))

    def test_first_id_wins_across_load_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "first_td_cluster.json"
            override.write_text(
                '{"id":"first_td_cluster","title":"Owner override","enabled":true,'
                '"trigger":{"series":"KXNFLFIRSTTD","exclude_title_substrings":[]},'
                '"legs":[{"id":"player_first_td","role":"trigger","action":"buy_yes",'
                '"count":"session"}]}',
                encoding="utf-8",
            )
            packages = load_packages([override, SHIPPED])
            self.assertEqual(len(packages), 1)
            self.assertEqual(packages[0].title, "Owner override")
            self.assertEqual(packages[0].source_kind, "shipped")  # kind from expand default

    def test_invalid_file_is_skipped(self) -> None:
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "nope.json"
            bad.write_text("{not json", encoding="utf-8")
            packages = load_packages([bad, SHIPPED], warn=warnings.append)
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].id, "first_td_cluster")
        self.assertTrue(any("skip" in w for w in warnings))

    def test_no_packages_summary(self) -> None:
        packages, summary, owner_n = load_browse_packages(
            script_file=ROOT / "kalshi_sports_trader.py",
            no_packages=True,
        )
        self.assertEqual(packages, [])
        self.assertIn("disabled", summary)
        self.assertEqual(owner_n, 0)


if __name__ == "__main__":
    unittest.main()
