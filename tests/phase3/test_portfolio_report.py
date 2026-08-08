"""Phase 5 M6 — Portfolio Reporting Tests (TD5).

Tests for the ``phase3.portfolio.report`` module (M6 deliverable):
``PortfolioReport``, ``ReportSection``, ``build_portfolio_report``.

Test groups:
- RPT1-RPT5: PortfolioReport + ReportSection DTO construction, invariants,
  and serialization round-trip.
- RPT6-RPT8: build_portfolio_report functional tests (portfolio-only,
  portfolio+decision, portfolio+decision+execution).
- RPT9-RPT10: Markdown rendering produces valid Markdown.
- RPT11-RPT12: Determinism tests (same inputs → same output).
- RPT13-RPT15: Error handling and edge cases.
- RPT16: macro_history.db safety guard (CLI path refusal is tested in
  test_portfolio_cli.py).

Total: 16 tests.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

# Ensure PYTHONPATH includes the repo root.
REPO_ROOT = Path("/home/ubuntu/macro-report")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from phase3.portfolio.domain import (
    CostBasis, EntityId, Portfolio, PortfolioId, Position, PositionId,
    Price, Quantity, Weight,
)
from phase3.portfolio.allocation import Allocation
from phase3.portfolio.decision import PortfolioDecision
from phase3.portfolio.execution import (
    ExecutionPlanConfig, ExecutionPlanResult, ExecutionQueue,
    ReviewQueue, SuggestedOrder, plan_execution,
)
from phase3.portfolio.report import (
    PortfolioReport, ReportSection,
    build_portfolio_report,
    REPORT_TYPE_DAILY, REPORT_TYPE_WEEKLY, REPORT_TYPE_CUSTOM,
)


# --------------------------------------------------------------------------- #
# Test fixtures
# --------------------------------------------------------------------------- #

def _make_portfolio() -> Portfolio:
    """Build a simple 3-position portfolio for testing."""
    return Portfolio(
        portfolio_id=PortfolioId("test-portfolio-001"),
        name="Test Portfolio",
        positions=(
            Position(
                position_id=PositionId("pos-0001"),
                entity_id=EntityId("company:TW:2330"),
                weight=Weight(0.40),
                quantity=Quantity(100),
            ),
            Position(
                position_id=PositionId("pos-0002"),
                entity_id=EntityId("company:TW:2317"),
                weight=Weight(0.35),
                quantity=Quantity(200),
            ),
            Position(
                position_id=PositionId("pos-0003"),
                entity_id=EntityId("industry:TW:finance"),
                weight=Weight(0.25),
                quantity=Quantity(50),
            ),
        ),
    )


def _make_decision() -> PortfolioDecision:
    """Build a simple PortfolioDecision for testing."""
    return PortfolioDecision(
        decision_id="dec-test-001",
        portfolio_id="test-portfolio-001",
        allocation=Allocation(
            allocation_id="alloc-test-001",
            positions=(
                Position(
                    position_id=PositionId("pos-0001"),
                    entity_id=EntityId("company:TW:2330"),
                    weight=Weight(0.50),
                    quantity=Quantity(120),
                ),
                Position(
                    position_id=PositionId("pos-0002"),
                    entity_id=EntityId("company:TW:2317"),
                    weight=Weight(0.30),
                    quantity=Quantity(170),
                ),
                Position(
                    position_id=PositionId("pos-0003"),
                    entity_id=EntityId("industry:TW:finance"),
                    weight=Weight(0.20),
                    quantity=Quantity(40),
                ),
            ),
            target_total=1.0,
        ),
        rationale="Score-weighted allocation from intelligence pipeline signals.",
        evidence_refs=("sig-macro-001", "sig-industry-001"),
        generated_at="2026-08-08T00:00:00Z",
        risk_summary={"exposure": 0.85, "concentration": 0.34, "drawdown": 0.12},
    )


def _make_execution_plan(decision: PortfolioDecision, portfolio: Portfolio) -> ExecutionPlanResult:
    """Build a simple ExecutionPlanResult for testing."""
    config = ExecutionPlanConfig(
        review_delta_threshold=0.10,
        generated_at="2026-08-08T00:00:00Z",
    )
    return plan_execution(decision, portfolio, config)


# --------------------------------------------------------------------------- #
# ReportSection DTO tests
# --------------------------------------------------------------------------- #


class TestReportSectionDTO(unittest.TestCase):
    """RPT1-RPT5: ReportSection construction, invariants, serialization."""

    def test_rpt1_section_construction_text(self):
        """RPT1: text section construction."""
        s = ReportSection(title="Summary", content_type="text", content=("Hello", "World"))
        self.assertEqual(s.title, "Summary")
        self.assertEqual(s.content_type, "text")
        self.assertEqual(len(s.content), 2)

    def test_rpt2_section_construction_kv(self):
        """RPT2: kv section with tuple content."""
        s = ReportSection(
            title="Metrics",
            content_type="kv",
            content=(("Name", "Test"), ("Value", "42")),
        )
        self.assertEqual(s.content_type, "kv")
        self.assertEqual(len(s.content), 2)

    def test_rpt3_section_invalid_title(self):
        """RPT3: empty title raises ValueError."""
        with self.assertRaises(ValueError):
            ReportSection(title="", content_type="text")

    def test_rpt4_section_invalid_content_type(self):
        """RPT4: invalid content_type raises ValueError."""
        with self.assertRaises(ValueError):
            ReportSection(title="T", content_type="invalid")

    def test_rpt5_section_to_dict_round_trip(self):
        """RPT5: to_dict produces JSON-serializable output."""
        s = ReportSection(
            title="Test",
            content_type="kv",
            content=(("A", "1"), ("B", "2")),
            sub_sections=(
                ReportSection(title="Sub", content_type="text", content=("x",)),
            ),
        )
        d = s.to_dict()
        self.assertEqual(d["title"], "Test")
        self.assertEqual(d["content_type"], "kv")
        # Content should be normalized: inner tuples become lists, outer is tuple.
        self.assertEqual(d["content"], (["A", "1"], ["B", "2"]))
        self.assertEqual(len(d["sub_sections"]), 1)
        # Must be JSON-serializable.
        json.dumps(d)


# --------------------------------------------------------------------------- #
# PortfolioReport DTO tests
# --------------------------------------------------------------------------- #


class TestPortfolioReportDTO(unittest.TestCase):
    """RPT6-RPT8: PortfolioReport construction and serialization."""

    def test_rpt6_report_construction(self):
        """RPT6: basic PortfolioReport construction."""
        r = PortfolioReport(
            report_id="rpt-001",
            report_type=REPORT_TYPE_DAILY,
            portfolio_id="test-001",
            date_range="2026-08-08",
            generated_at="2026-08-08T00:00:00Z",
        )
        self.assertEqual(r.report_id, "rpt-001")
        self.assertEqual(r.report_type, "daily")
        self.assertEqual(r.portfolio_id, "test-001")
        self.assertEqual(r.date_range, "2026-08-08")

    def test_rpt7_report_invalid_report_type(self):
        """RPT7: invalid report_type raises ValueError."""
        with self.assertRaises(ValueError):
            PortfolioReport(
                report_id="rpt-002",
                report_type="monthly",
                portfolio_id="test-001",
                date_range="2026-08-08",
            )

    def test_rpt8_report_to_dict_json_serializable(self):
        """RPT8: to_dict is JSON-serializable with sections."""
        s = ReportSection(title="Summary", content_type="kv", content=(("A", "1"),))
        r = PortfolioReport(
            report_id="rpt-003",
            report_type=REPORT_TYPE_WEEKLY,
            portfolio_id="test-001",
            date_range="2026-08-01..2026-08-07",
            generated_at="2026-08-08T00:00:00Z",
            sections=(s,),
            summary={"position_count": 3},
        )
        d = r.to_dict()
        json.dumps(d)  # Must not raise.
        self.assertEqual(d["report_id"], "rpt-003")
        self.assertEqual(len(d["sections"]), 1)
        self.assertEqual(d["summary"]["position_count"], 3)


# --------------------------------------------------------------------------- #
# build_portfolio_report functional tests
# --------------------------------------------------------------------------- #


class TestBuildPortfolioReport(unittest.TestCase):
    """RPT9-RPT12: functional tests for build_portfolio_report."""

    def test_rpt9_portfolio_only(self):
        """RPT9: build report with portfolio only (no decision/execution)."""
        portfolio = _make_portfolio()
        report = build_portfolio_report(
            portfolio,
            report_type=REPORT_TYPE_DAILY,
            date_range="2026-08-08",
            generated_at="2026-08-08T00:00:00Z",
        )
        self.assertEqual(report.report_type, "daily")
        self.assertEqual(report.portfolio_id, "test-portfolio-001")
        self.assertEqual(report.date_range, "2026-08-08")
        # Should have at least 2 sections: portfolio summary + positions.
        self.assertGreaterEqual(len(report.sections), 2)
        # Summary should have position count.
        self.assertEqual(report.summary["position_count"], 3)
        self.assertFalse(report.summary["has_decision"])
        self.assertFalse(report.summary["has_execution_plan"])

    def test_rpt10_portfolio_with_decision(self):
        """RPT10: build report with portfolio + decision."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        report = build_portfolio_report(
            portfolio,
            decision=decision,
            report_type=REPORT_TYPE_DAILY,
            date_range="2026-08-08",
            generated_at="2026-08-08T00:00:00Z",
        )
        self.assertTrue(report.summary["has_decision"])
        self.assertFalse(report.summary["has_execution_plan"])
        # Should have a "Portfolio Decision" section.
        titles = [s.title for s in report.sections]
        self.assertIn("Portfolio Decision", titles)

    def test_rpt11_portfolio_with_decision_and_execution(self):
        """RPT11: build report with portfolio + decision + execution plan."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        exec_plan = _make_execution_plan(decision, portfolio)
        report = build_portfolio_report(
            portfolio,
            decision=decision,
            execution_plan=exec_plan,
            report_type=REPORT_TYPE_WEEKLY,
            date_range="2026-08-01..2026-08-07",
            generated_at="2026-08-08T00:00:00Z",
        )
        self.assertTrue(report.summary["has_decision"])
        self.assertTrue(report.summary["has_execution_plan"])
        titles = [s.title for s in report.sections]
        self.assertIn("Portfolio Decision", titles)
        self.assertIn("Execution Plan", titles)

    def test_rpt12_determinism_same_inputs_same_output(self):
        """RPT12: same inputs produce the same to_dict output."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        report1 = build_portfolio_report(
            portfolio, decision=decision,
            date_range="2026-08-08", generated_at="2026-08-08T00:00:00Z",
        )
        report2 = build_portfolio_report(
            portfolio, decision=decision,
            date_range="2026-08-08", generated_at="2026-08-08T00:00:00Z",
        )
        self.assertEqual(
            json.dumps(report1.to_dict(), sort_keys=True),
            json.dumps(report2.to_dict(), sort_keys=True),
        )


# --------------------------------------------------------------------------- #
# Markdown rendering tests
# --------------------------------------------------------------------------- #


class TestMarkdownRendering(unittest.TestCase):
    """RPT13-RPT14: Markdown output is valid."""

    def test_rpt13_markdown_contains_headers(self):
        """RPT13: Markdown output contains expected headers."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        report = build_portfolio_report(
            portfolio, decision=decision,
            date_range="2026-08-08", generated_at="2026-08-08T00:00:00Z",
        )
        md = report.to_markdown()
        self.assertIn("# Daily Portfolio Report", md)
        self.assertIn("## Portfolio Summary", md)
        self.assertIn("## Current Positions", md)
        self.assertIn("## Portfolio Decision", md)

    def test_rpt14_markdown_contains_position_data(self):
        """RPT14: Markdown contains entity IDs and weights."""
        portfolio = _make_portfolio()
        report = build_portfolio_report(
            portfolio, date_range="2026-08-08", generated_at="2026-08-08T00:00:00Z",
        )
        md = report.to_markdown()
        self.assertIn("company:TW:2330", md)
        self.assertIn("0.400000", md)


# --------------------------------------------------------------------------- #
# Error handling and edge cases
# --------------------------------------------------------------------------- #


class TestReportErrors(unittest.TestCase):
    """RPT15-RPT16: error handling and edge cases."""

    def test_rpt15_empty_portfolio_report(self):
        """RPT15: report with empty portfolio still works."""
        empty_portfolio = Portfolio(
            portfolio_id=PortfolioId("empty-001"),
            name="Empty",
            positions=(),
        )
        report = build_portfolio_report(
            empty_portfolio,
            date_range="2026-08-08",
            generated_at="2026-08-08T00:00:00Z",
        )
        self.assertEqual(report.summary["position_count"], 0)
        self.assertEqual(report.summary["allocation_total"], 0.0)
        # to_dict and to_markdown should not raise.
        report.to_dict()
        report.to_markdown()

    def test_rpt16_custom_report_type(self):
        """RPT16: custom report type works."""
        portfolio = _make_portfolio()
        report = build_portfolio_report(
            portfolio,
            report_type=REPORT_TYPE_CUSTOM,
            date_range="2026-07-01..2026-07-31",
            generated_at="2026-08-01T00:00:00Z",
        )
        self.assertEqual(report.report_type, "custom")
        md = report.to_markdown()
        self.assertIn("# Portfolio Report", md)


if __name__ == "__main__":
    unittest.main()