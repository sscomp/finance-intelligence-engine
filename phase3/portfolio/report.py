"""Phase 5 M6 — Portfolio Reporting: Markdown + JSON report generation.

M6 ships the portfolio reporting layer. It transforms the outputs of M2–M5
(Portfolio, PortfolioDecision, ExecutionPlanResult, risk reports) into
human-readable **Markdown** and machine-readable **JSON** summary reports.

Design principles (mirrors M2 ``domain.py`` + M4 ``decision.py``)
----------------------------------------------------------------

1. **Immutable.** ``PortfolioReport`` and ``ReportSection`` are
   ``@dataclass(frozen=True)``. Mutation returns a new instance.

2. **Deterministic.** ``generated_at`` is injected as a string (ISO 8601)
   — the same determinism pattern as M4's ``PortfolioDecision`` and M5's
   ``ExecutionPlanResult``. No ``datetime.now()`` in production code.

3. **Pure functions.** ``build_portfolio_report`` is a pure function — no
   I/O, no global mutable state, no ``random.*``, no ``datetime.now()``.

4. **Dual output.** Each report produces both a Markdown string
   (``to_markdown()``) and a JSON-serializable dict (``to_dict()``). The
   JSON dict is the canonical form; Markdown is rendered from the same
   data.

5. **Output-only invariant (O4, inherited from M5).** This module MUST NOT
   contain network/execution-platform API calls (forbidden names checked
   by AST scan AG-M6-1 and the safety guard tripwire in TD10).

Boundary contract
-------------------

This module imports ONLY from the Python standard library and from
``phase3.portfolio.domain`` (M2), ``phase3.portfolio.decision`` (M4),
``phase3.portfolio.execution`` (M5), and optionally
``phase3.portfolio.risk`` (M3) for risk-report summaries. It MUST NOT
import ``sqlite3``, ``macro_history``, ``phase3.pipeline``,
``phase3.datamodel``, ``phase3.graph``, or any execution-platform /
network module (AST-enforced by TD10, the M6 safety guard test file
``tests/phase3/test_portfolio_safety_guards.py``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from phase3.portfolio.domain import Portfolio, Position, Weight
from phase3.portfolio.decision import PortfolioDecision
from phase3.portfolio.execution import ExecutionPlanResult

# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #

# Identifier pattern — identical rule to M2/M4 (non-empty, no whitespace,
# <= 128 chars). Duplicated to keep the boundary contract self-contained.
_IDENTIFIER_PATTERN = re.compile(r"^[^\s]{1,128}$")

# Maximum report section depth for nested rendering.
_MAX_SECTION_DEPTH = 10

# Report type constants — used by CLI and serialization.
REPORT_TYPE_DAILY = "daily"
REPORT_TYPE_WEEKLY = "weekly"
REPORT_TYPE_CUSTOM = "custom"
_VALID_REPORT_TYPES = frozenset({REPORT_TYPE_DAILY, REPORT_TYPE_WEEKLY, REPORT_TYPE_CUSTOM})


# --------------------------------------------------------------------------- #
# ReportSection DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReportSection:
    """A single section of a portfolio report.

    A report section has a ``title``, a ``content_type`` (``"text"``,
    ``"table"``, ``"list"``, ``"kv"``), and ``content`` (a list of strings
    for text/list, a list of row-dicts for table, or a list of key-value
    pairs for kv). Sections can optionally have sub-sections.

    Invariants:
    - ``title`` is a non-empty string.
    - ``content_type`` is one of the valid content types.
    - ``content`` is a tuple (immutable).
    - ``sub_sections`` is a tuple of ``ReportSection`` instances.
    """

    title: str
    content_type: str = "text"
    content: tuple[Any, ...] = field(default_factory=tuple)
    sub_sections: tuple["ReportSection", ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.title, str):
            raise ValueError(
                f"ReportSection.title must be a str; "
                f"got {type(self.title).__name__}"
            )
        stripped = self.title.strip()
        if not stripped:
            raise ValueError("ReportSection.title must be non-empty after stripping")
        if len(stripped) > 256:
            raise ValueError(
                f"ReportSection.title must be <= 256 chars; got {len(stripped)}"
            )
        if stripped != self.title:
            object.__setattr__(self, "title", stripped)

        if self.content_type not in ("text", "table", "list", "kv"):
            raise ValueError(
                f"ReportSection.content_type must be one of "
                f"'text', 'table', 'list', 'kv'; got {self.content_type!r}"
            )

        if not isinstance(self.content, tuple):
            object.__setattr__(self, "content", tuple(self.content))

        if not isinstance(self.sub_sections, tuple):
            object.__setattr__(
                self, "sub_sections", tuple(self.sub_sections)
            )
        for ss in self.sub_sections:
            if not isinstance(ss, ReportSection):
                raise ValueError(
                    f"ReportSection.sub_sections must contain "
                    f"ReportSection instances; got {type(ss).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict."""
        # Normalize content: kv tuples become [key, value] lists; table
        # dicts become dicts; strings stay strings.
        raw_content: list[Any] = []
        for item in self.content:
            if isinstance(item, (tuple, list)):
                raw_content.append(list(item))
            elif isinstance(item, dict):
                raw_content.append(dict(item))
            else:
                raw_content.append(item)
        return {
            "title": self.title,
            "content_type": self.content_type,
            "content": tuple(raw_content),
            "sub_sections": tuple(ss.to_dict() for ss in self.sub_sections),
        }

    def to_markdown(self, depth: int = 1) -> str:
        """Render this section as a Markdown string.

        Heading level is ``depth`` (1 = #, 2 = ##, etc.), clamped to
        ``_MAX_SECTION_DEPTH``.
        """
        h = "#" * min(max(depth, 1), _MAX_SECTION_DEPTH)
        lines: list[str] = [f"{h} {self.title}", ""]

        if self.content_type == "text":
            for line in self.content:
                lines.append(str(line))
            if self.content:
                lines.append("")
        elif self.content_type == "list":
            for item in self.content:
                lines.append(f"- {item}")
            if self.content:
                lines.append("")
        elif self.content_type == "kv":
            for kv in self.content:
                if isinstance(kv, (tuple, list)) and len(kv) >= 2:
                    lines.append(f"- **{kv[0]}**: {kv[1]}")
                else:
                    lines.append(f"- {kv}")
            if self.content:
                lines.append("")
        elif self.content_type == "table":
            if self.content:
                first = self.content[0]
                if isinstance(first, dict):
                    headers = list(first.keys())
                    lines.append("| " + " | ".join(headers) + " |")
                    lines.append("| " + " | ".join("---" for _ in headers) + " |")
                    for row in self.content:
                        vals = [str(row.get(h, "")) for h in headers]
                        lines.append("| " + " | ".join(vals) + " |")
                    lines.append("")

        for ss in self.sub_sections:
            lines.append(ss.to_markdown(depth + 1))

        return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# PortfolioReport DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PortfolioReport:
    """A complete portfolio report: metadata + sections + summary.

    This is the top-level output of ``build_portfolio_report``. It carries
    report metadata (type, date range, generated_at) and a tuple of
    ``ReportSection`` entities.

    Invariants:
    - ``report_id`` matches the identifier pattern.
    - ``report_type`` is one of the valid report types.
    - ``portfolio_id`` is a non-empty string.
    - ``date_range`` is a string (e.g. ``"2026-08-01"`` or
      ``"2026-08-01..2026-08-07"``).
    - ``generated_at`` is a string (ISO 8601, injected for determinism).
    - ``sections`` is a tuple of ``ReportSection`` instances.
    - ``summary`` is a dict (JSON-serializable summary statistics).
    """

    report_id: str
    report_type: str
    portfolio_id: str
    date_range: str
    generated_at: str = ""
    sections: tuple[ReportSection, ...] = field(default_factory=tuple)
    summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.report_id, str):
            raise ValueError(
                f"PortfolioReport.report_id must be a str; "
                f"got {type(self.report_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.report_id):
            raise ValueError(
                f"PortfolioReport.report_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.report_id!r}"
            )

        if not isinstance(self.report_type, str):
            raise ValueError(
                f"PortfolioReport.report_type must be a str; "
                f"got {type(self.report_type).__name__}"
            )
        if self.report_type not in _VALID_REPORT_TYPES:
            raise ValueError(
                f"PortfolioReport.report_type must be one of "
                f"{sorted(_VALID_REPORT_TYPES)}; got {self.report_type!r}"
            )

        if not isinstance(self.portfolio_id, str):
            raise ValueError(
                f"PortfolioReport.portfolio_id must be a str; "
                f"got {type(self.portfolio_id).__name__}"
            )
        if not self.portfolio_id.strip():
            raise ValueError(
                "PortfolioReport.portfolio_id must be non-empty"
            )

        if not isinstance(self.date_range, str):
            raise ValueError(
                f"PortfolioReport.date_range must be a str; "
                f"got {type(self.date_range).__name__}"
            )

        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"PortfolioReport.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        if not isinstance(self.sections, tuple):
            object.__setattr__(self, "sections", tuple(self.sections))
        for sec in self.sections:
            if not isinstance(sec, ReportSection):
                raise ValueError(
                    f"PortfolioReport.sections must contain "
                    f"ReportSection instances; got {type(sec).__name__}"
                )

        if not isinstance(self.summary, dict):
            raise ValueError(
                f"PortfolioReport.summary must be a dict; "
                f"got {type(self.summary).__name__}"
            )

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict.

        Keys are emitted in a fixed, code-defined order so that
        ``from_dict(to_dict(x)).to_dict() == to_dict(x)`` (byte-identical
        round-trip).
        """
        return {
            "report_id": self.report_id,
            "report_type": self.report_type,
            "portfolio_id": self.portfolio_id,
            "date_range": self.date_range,
            "generated_at": self.generated_at,
            "sections": tuple(s.to_dict() for s in self.sections),
            "summary": dict(self.summary),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortfolioReport":
        """Reconstruct a ``PortfolioReport`` from its ``to_dict()`` output."""
        raw_sections = data.get("sections", ())
        sections = tuple(
            ReportSection(
                title=s["title"],
                content_type=s.get("content_type", "text"),
                content=tuple(s.get("content", ())),
                sub_sections=tuple(
                    ReportSection(
                        title=ss["title"],
                        content_type=ss.get("content_type", "text"),
                        content=tuple(ss.get("content", ())),
                    )
                    for ss in s.get("sub_sections", ())
                ),
            )
            for s in raw_sections
        )
        return cls(
            report_id=data["report_id"],
            report_type=data["report_type"],
            portfolio_id=data["portfolio_id"],
            date_range=data.get("date_range", ""),
            generated_at=data.get("generated_at", ""),
            sections=sections,
            summary=dict(data.get("summary", {})),
        )

    # ------------------------------------------------------------------ #
    # Markdown rendering
    # ------------------------------------------------------------------ #

    def to_markdown(self) -> str:
        """Render the full report as a Markdown string.

        The output starts with a title header containing the report type
        and date range, followed by each section rendered at depth 1.
        """
        type_label = {
            REPORT_TYPE_DAILY: "Daily Portfolio Report",
            REPORT_TYPE_WEEKLY: "Weekly Portfolio Report",
            REPORT_TYPE_CUSTOM: "Portfolio Report",
        }.get(self.report_type, "Portfolio Report")

        lines: list[str] = [
            f"# {type_label}",
            "",
            f"- **Portfolio**: {self.portfolio_id}",
            f"- **Date Range**: {self.date_range}",
            f"- **Generated At**: {self.generated_at}",
            f"- **Report ID**: {self.report_id}",
            "",
        ]

        for section in self.sections:
            lines.append(section.to_markdown(depth=2))

        return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Report builder
# --------------------------------------------------------------------------- #


def build_portfolio_report(
    portfolio: Portfolio,
    decision: PortfolioDecision | None = None,
    execution_plan: ExecutionPlanResult | None = None,
    *,
    report_type: str = REPORT_TYPE_DAILY,
    date_range: str = "",
    generated_at: str = "",
    report_id: str = "",
    risk_summary: dict[str, Any] | None = None,
) -> PortfolioReport:
    """Build a ``PortfolioReport`` from portfolio-domain DTOs.

    This is a pure function. It does NOT perform any I/O, does NOT call
    any execution-platform API, and does NOT modify any external state.

    Args:
        portfolio: The current portfolio state (M2 domain model).
        decision: Optional ``PortfolioDecision`` (M4 target allocation).
        execution_plan: Optional ``ExecutionPlanResult`` (M5 suggested
            orders).
        report_type: One of ``"daily"``, ``"weekly"``, ``"custom"``.
        date_range: Date or date range string (e.g. ``"2026-08-08"`` or
            ``"2026-08-01..2026-08-07"``).
        generated_at: ISO 8601 timestamp for determinism.
        report_id: Optional report identifier. If empty, auto-generated
            as a deterministic hash of portfolio_id + date_range.
        risk_summary: Optional risk-summary dict (from M4's
            ``PortfolioDecision.risk_summary`` or M3 risk reports).

    Returns:
        A ``PortfolioReport`` with sections for portfolio summary,
        positions, decision (if provided), execution plan (if provided),
        and risk summary (if provided).
    """
    if report_type not in _VALID_REPORT_TYPES:
        raise ValueError(
            f"report_type must be one of {sorted(_VALID_REPORT_TYPES)}; "
            f"got {report_type!r}"
        )

    # Deterministic report_id if not provided.
    if not report_id:
        import hashlib
        raw = f"{portfolio.portfolio_id.value}:{date_range}:{report_type}"
        report_id = "rpt-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    if not _IDENTIFIER_PATTERN.match(report_id):
        raise ValueError(
            f"report_id must be non-empty, no whitespace, <= 128 chars; "
            f"got {report_id!r}"
        )

    sections: list[ReportSection] = []

    # Section 1: Portfolio Summary.
    summary_kv: list[tuple[str, str]] = [
        ("Portfolio ID", portfolio.portfolio_id.value),
        ("Portfolio Name", portfolio.name),
        ("Position Count", str(portfolio.position_count)),
        ("Allocation Total", f"{portfolio.allocation_total:.6f}"),
    ]
    sections.append(ReportSection(
        title="Portfolio Summary",
        content_type="kv",
        content=tuple(summary_kv),
    ))

    # Section 2: Positions.
    position_rows: list[dict[str, str]] = []
    for pos in portfolio.positions:
        position_rows.append({
            "Entity": pos.entity_id.value,
            "Position ID": pos.position_id.value,
            "Weight": f"{pos.weight.value:.6f}",
            "Quantity": str(pos.quantity.value),
        })
    sections.append(ReportSection(
        title="Current Positions",
        content_type="table",
        content=tuple(position_rows) if position_rows else (),
    ))

    # Section 3: Decision (if provided).
    if decision is not None:
        decision_kv: list[tuple[str, str]] = [
            ("Decision ID", decision.decision_id),
            ("Portfolio ID", decision.portfolio_id),
            ("Policy Rationale", decision.rationale[:200]),
            ("Generated At", decision.generated_at),
            ("Evidence Refs", str(len(decision.evidence_refs))),
        ]
        decision_sub_sections: list[ReportSection] = []

        # Target allocation table.
        alloc_rows: list[dict[str, str]] = []
        for pos in decision.allocation.positions:
            alloc_rows.append({
                "Entity": pos.entity_id.value,
                "Target Weight": f"{pos.weight.value:.6f}",
                "Quantity": str(pos.quantity.value),
            })
        if alloc_rows:
            decision_sub_sections.append(ReportSection(
                title="Target Allocation",
                content_type="table",
                content=tuple(alloc_rows),
            ))
        decision_sub_sections.append(ReportSection(
            title="Allocation Summary",
            content_type="kv",
            content=(
                ("Target Total", f"{decision.allocation.target_total:.6f}"),
                ("Position Count", str(decision.allocation.position_count)),
            ),
        ))

        sections.append(ReportSection(
            title="Portfolio Decision",
            content_type="kv",
            content=tuple(decision_kv),
            sub_sections=tuple(decision_sub_sections),
        ))

    # Section 4: Execution Plan (if provided).
    if execution_plan is not None:
        exec_kv: list[tuple[str, str]] = [
            ("Plan ID", execution_plan.plan_id),
            ("Generated At", execution_plan.generated_at),
            ("Total Orders", str(execution_plan.total_order_count)),
            ("Execution Queue", str(execution_plan.execution_queue.order_count)),
            ("Review Queue", str(execution_plan.review_queue.order_count)),
        ]
        exec_sub_sections: list[ReportSection] = []

        # Execution queue orders.
        exec_order_rows: list[dict[str, str]] = []
        for order in execution_plan.execution_queue.orders:
            exec_order_rows.append({
                "Entity": order.entity_id.value,
                "Action": order.action,
                "Delta": f"{order.delta_weight:+.6f}",
                "Priority": str(order.priority),
            })
        if exec_order_rows:
            exec_sub_sections.append(ReportSection(
                title="Execution Queue Orders",
                content_type="table",
                content=tuple(exec_order_rows),
            ))

        # Review queue orders.
        review_order_rows: list[dict[str, str]] = []
        for order in execution_plan.review_queue.orders:
            reasons = execution_plan.review_queue.review_reasons.get(
                order.order_id, []
            )
            reason_str = "; ".join(reasons) if reasons else ""
            review_order_rows.append({
                "Entity": order.entity_id.value,
                "Action": order.action,
                "Delta": f"{order.delta_weight:+.6f}",
                "Priority": str(order.priority),
                "Reasons": reason_str,
            })
        if review_order_rows:
            exec_sub_sections.append(ReportSection(
                title="Review Queue Orders",
                content_type="table",
                content=tuple(review_order_rows),
            ))

        sections.append(ReportSection(
            title="Execution Plan",
            content_type="kv",
            content=tuple(exec_kv),
            sub_sections=tuple(exec_sub_sections),
        ))

    # Section 5: Risk Summary (if provided).
    effective_risk = risk_summary
    if effective_risk is None and decision is not None:
        effective_risk = decision.risk_summary or None
    if effective_risk is not None and effective_risk:
        risk_kv: list[tuple[str, str]] = []
        for key in sorted(effective_risk.keys()):
            val = effective_risk[key]
            if isinstance(val, (int, float)):
                risk_kv.append((key, f"{val:.6f}"))
            else:
                risk_kv.append((key, str(val)))
        sections.append(ReportSection(
            title="Risk Summary",
            content_type="kv",
            content=tuple(risk_kv),
        ))

    # Build summary statistics.
    summary: dict[str, Any] = {
        "portfolio_id": portfolio.portfolio_id.value,
        "position_count": portfolio.position_count,
        "allocation_total": portfolio.allocation_total,
        "has_decision": decision is not None,
        "has_execution_plan": execution_plan is not None,
        "execution_orders": (
            execution_plan.execution_queue.order_count
            if execution_plan is not None
            else 0
        ),
        "review_orders": (
            execution_plan.review_queue.order_count
            if execution_plan is not None
            else 0
        ),
    }
    if decision is not None:
        summary["decision_id"] = decision.decision_id
        summary["target_total"] = decision.allocation.target_total
    if effective_risk is not None and effective_risk:
        summary["risk_keys"] = sorted(effective_risk.keys())

    return PortfolioReport(
        report_id=report_id,
        report_type=report_type,
        portfolio_id=portfolio.portfolio_id.value,
        date_range=date_range,
        generated_at=generated_at,
        sections=tuple(sections),
        summary=summary,
    )