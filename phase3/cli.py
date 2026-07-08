"""Phase 3 CLI — demo and debugging interface for the Investment Intelligence Engine.

Subcommands:
  score-macro           Score a macro factor bundle (sample data).
  score-industry        Score an industry sector (sample data + macro context).
  score-company         Score a company (with optional cross-layer adjustment).
  aggregate-signals     Run the SignalEngine over a small sample signal set.
  graph-demo            Build a small sample research graph and print stats.
  graph-trace           Trace evidence from a node in a sample graph.

This CLI is for Phase 3A scaffold validation and developer demos only.
It does NOT touch production data sources, cron, or any Telegram sender.

Run:
  PYTHONPATH=/home/ubuntu/macro-report \\
    /home/ubuntu/macro-venv/bin/python /home/ubuntu/macro-report/phase3/cli.py score-macro
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.evidence import Evidence, make_evidence_id
from phase3.datamodel.signals import Signal, SignalSource
from phase3.graph import (
    EdgeType,
    EvidenceTracer,
    GraphEdge,
    GraphNode,
    GraphStore,
    NodeType,
)
from phase3.scoring import CompanyScorer, IndustryScorer, MacroScorer
from phase3.scoring.explain import explain_score
from phase3.signals import SignalAggregator, SignalEngine


# ---------- sample data ----------

SAMPLE_MACRO_INPUTS: dict[str, Any] = {
    "economic":    {"gdp_yoy": 0.025, "pmi": 51.5,
                    "unemployment_delta_pp": -0.1, "retail_yoy": 0.04},
    "monetary":    {"fed_action": "hold", "hawkish_dots": 10,
                    "total_dots": 18, "cb_unanimity": 0.6},
    "inflation":   {"cpi_yoy": 0.028, "core_cpi_yoy": 0.022},
    "rates":       {"yield_spread_pp": 0.4, "real_rate": 0.01, "rate_vol": 95.0},
    "liquidity":   {"vix": 18.0, "dxy": 102.0, "m2_yoy": 0.04,
                    "credit_spread_bp": 130.0},
    "geopolitics": {"events": ["trade_war", "election_risk"]},
}

SAMPLE_INDUSTRY_INPUTS_AI: dict[str, dict[str, float]] = {
    "rotation":          {"sector_relative_perf_5d": 0.03,
                          "sector_relative_perf_20d": 0.06,
                          "money_flow_proxy": 0.2},
    "relative_strength": {"rs_rating": 75.0,
                          "industry_etf_momentum_1m": 0.05,
                          "stock_breadth_pct": 0.6},
    "cyclicality":       {"pmi_direction": 1.5,
                          "book_to_bill": 1.05, "export_yoy": 0.07},
    "macro_sensitivity": {},
    "industry_news":     {"news_volume_change": 0.5,
                          "sentiment_polarity": 0.4,
                          "event_severity_aggregate": 30.0},
    "capital_flow":      {"foreign_net_industry_total": 1.5,
                          "prop_net_industry_total": 0.8},
}

SAMPLE_COMPANY_INPUTS_2330: dict[str, dict[str, float]] = {
    "financial_quality": {"roe": 0.30, "roa": 0.18, "debt_equity": 0.20,
                          "current_ratio": 2.5, "fcf": 1.0e12},
    "growth":            {"revenue_yoy": 0.22, "eps_yoy": 0.30,
                          "fcf_growth": 0.18, "guidance": 0.5},
    "profitability":     {"gross_margin": 0.55, "operating_margin": 0.40,
                          "net_margin": 0.32},
    "valuation":         {"pe": 22.0, "pb": 6.0, "peg": 1.4,
                          "dividend_yield": 0.02},
    "momentum":          {"price_momentum_1m": 0.05,
                          "price_momentum_3m": 0.12,
                          "dist_from_52w_high": -0.05,
                          "relative_to_market": 0.04},
    "risk":              {"beta": 1.1, "debt_ratio": 0.15,
                          "earnings_volatility": 0.20, "risk_event_count": 0},
    "news_sentiment":    {"sentiment": 0.4, "event_severity": 25.0},
}


# ---------- helpers ----------

def _build_sample_graph() -> GraphStore:
    s = GraphStore()
    nodes = [
        ("company:2330", NodeType.COMPANY, "台積電"),
        ("industry:半導體", NodeType.INDUSTRY, "半導體"),
        ("macro:FED_RATE", NodeType.MACRO_FACTOR, "Fed利率"),
        ("signal:2330:pe", NodeType.SIGNAL, "PE=22"),
        ("signal:2330:roe", NodeType.SIGNAL, "ROE=30%"),
        ("score:company:2330:2026-07-08", NodeType.SCORE, "+34.04"),
        ("score:macro:global:2026-07-08", NodeType.SCORE, "+4.68"),
        ("source:yfinance", NodeType.SOURCE, "yfinance"),
    ]
    for nid, ntype, label in nodes:
        s.add_node(GraphNode(node_id=nid, node_type=ntype, label=label))

    edges = [
        (EdgeType.MEMBER_OF, "company:2330", "industry:半導體"),
        (EdgeType.EXPOSED_TO, "company:2330", "macro:FED_RATE"),
        (EdgeType.CONTRIBUTES_TO, "signal:2330:pe",
         "score:company:2330:2026-07-08"),
        (EdgeType.CONTRIBUTES_TO, "signal:2330:roe",
         "score:company:2330:2026-07-08"),
        (EdgeType.GENERATED, "signal:2330:pe", "source:yfinance"),
        (EdgeType.GENERATED, "signal:2330:roe", "source:yfinance"),
        (EdgeType.CITES, "score:company:2330:2026-07-08", "source:yfinance"),
        (EdgeType.INFLUENCES, "score:macro:global:2026-07-08",
         "score:company:2330:2026-07-08"),
    ]
    for et, f, t in edges:
        s.add_edge(GraphEdge(edge_id="_", edge_type=et,
                             from_node_id=f, to_node_id=t))
    return s


def _build_sample_signals() -> list[Signal]:
    """Build a small sample signal set (no live fetch)."""
    now = datetime.now(timezone.utc)
    src_rss = SignalSource(source_id="rss.cnyes.2330-1",
                           source_type="rss", ref="cnyes")
    src_t86 = SignalSource(source_id="t86.2330-foreign",
                           source_type="t86", ref="t86")
    ev_news = Evidence(
        evidence_id=make_evidence_id("rss", "cnyes-2330-1", "0.6"),
        source_type="rss", source_ref="cnyes-2330-1",
        raw_value="0.6", description="台積電法說會正面",
        timestamp=now,
    )
    ev_flow = Evidence(
        evidence_id=make_evidence_id("t86", "2330-foreign", "1500"),
        source_type="t86", source_ref="2330-foreign",
        raw_value="1500", description="外資買超 1500 張",
        timestamp=now,
    )
    return [
        Signal(
            signal_id="sig-2330-news-1",
            entity_type="company", entity_id="2330",
            signal_type="news_headline",
            value=0.6, unit="score",
            direction="bullish",
            timestamp=now, source=src_rss,
            date_bucket=now.date().isoformat(),
            metadata={"evidence_id": ev_news.evidence_id,
                      "source_ref": ev_news.source_ref,
                      "description": ev_news.description},
        ),
        Signal(
            signal_id="sig-2330-flow-1",
            entity_type="company", entity_id="2330",
            signal_type="institutional_flow",
            value=1500.0, unit="zhang",
            direction="bullish",
            timestamp=now, source=src_t86,
            date_bucket=now.date().isoformat(),
            metadata={"evidence_id": ev_flow.evidence_id,
                      "source_ref": ev_flow.source_ref,
                      "description": ev_flow.description},
        ),
    ]


# ---------- subcommands ----------


# Feature flag for the Phase 3B persistence CLI. Default OFF: the
# `init-db` subcommand refuses to run unless this is truthy. This
# keeps a stray cron job or human from creating an intelligence.db
# by accident. We use the env-var convention that other Phase 3
# tooling already uses (PHASE3B_ENABLED).
PHASE3B_ENV_FLAG: str = "PHASE3B_ENABLED"


def _phase3b_enabled(args: argparse.Namespace) -> bool:
    """Return True if the user has explicitly opted in to Phase 3B.

    Priority: explicit ``--force`` flag, then the environment
    variable, then False. We never silently enable.
    """
    if getattr(args, "force", False):
        return True
    val = os.environ.get(PHASE3B_ENV_FLAG, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def cmd_init_db(args: argparse.Namespace) -> int:
    """Initialise the Phase 3B SQLite database (idempotent).

    Refuses to run unless ``PHASE3B_ENABLED=1`` (or ``--force``) is
    set. The default path is :data:`SQLiteGraphStore.DEFAULT_DB_PATH`
    but callers may override it. After apply we print the schema
    version and the quick_check result so the operator has visible
    confirmation.
    """
    if not _phase3b_enabled(args):
        print(
            f"refusing to run: Phase 3B persistence is gated behind "
            f"{PHASE3B_ENV_FLAG}=1 (or pass --force).",
            file=sys.stderr,
        )
        return 2

    # Late imports keep the rest of the CLI fast and let this module
    # import cleanly even if persistence is missing in some
    # deployments.
    from phase3.graph.sqlite_store import DEFAULT_DB_PATH, SQLiteGraphStore
    from phase3.persistence.migrations import MigrationManager
    from phase3.persistence.sqlite import quick_check
    from phase3.persistence import schema_v1

    target = getattr(args, "db_path", None) or DEFAULT_DB_PATH
    print(f"[init-db] target = {target}")
    # We pass ``auto_migrate=False`` here so we can capture the list
    # of migrations that were *newly* applied in this call. The
    # construction-time ensure_schema() also runs it, but we want
    # visibility for the operator, so we run it explicitly here and
    # own the result.
    with SQLiteGraphStore(target, auto_migrate=False) as store:
        mgr = MigrationManager(store._store, [schema_v1.build()])
        applied = mgr.apply()
        current = mgr.current_version()
        qc = quick_check(store.path)
    print(f"[init-db] current_version = {current}")
    if applied:
        print(f"[init-db] applied {len(applied)} migration(s): "
              f"{[m.version for m in applied]}")
    else:
        print("[init-db] no new migrations applied (schema up to date)")
    print(f"[init-db] quick_check = {qc}")
    return 0 if qc == "ok" else 1


def cmd_score_macro(_args: argparse.Namespace) -> int:
    sc = MacroScorer(config_hash="cli-macro")
    ms = sc.score_macro(SAMPLE_MACRO_INPUTS)
    exp = explain_score(ms.breakdown, top_n_dimensions=3)
    print("=== MacroScore (sample) ===")
    print(f"  score: {ms.score:+.2f}  confidence: {ms.confidence:.2f}")
    print(f"  summary: {exp['summary_zh']}")
    print()
    print("Dimensions:")
    for d in ms.dimensions:
        print(f"  - {d.name:<14} score={d.score:+7.2f} weight={d.weight:.2f} "
              f"conf={d.confidence:.2f}")
    return 0


def cmd_score_industry(_args: argparse.Namespace) -> int:
    ms = MacroScorer(config_hash="cli-macro")
    mctx = ms.score_macro(SAMPLE_MACRO_INPUTS)
    is_ = IndustryScorer(config_hash="cli-industry")
    isc = is_.score_industry(
        industry_id="AI", industry_name="AI",
        inputs=SAMPLE_INDUSTRY_INPUTS_AI,
        config={"macro_context": mctx.breakdown,
                "industry_macro_beta": {"liquidity": 0.5, "rates": 0.2,
                                        "monetary": 0.1, "geopolitics": 0.3}},
        constituent_count=6,
    )
    exp = explain_score(isc.breakdown, top_n_dimensions=3)
    print("=== IndustryScore(AI) (sample, with macro context) ===")
    print(f"  score: {isc.score:+.2f}  confidence: {isc.confidence:.2f}")
    print(f"  summary: {exp['summary_zh']}")
    print()
    print("Dimensions:")
    for d in isc.dimensions:
        print(f"  - {d.name:<20} score={d.score:+7.2f} weight={d.weight:.2f}")
    return 0


def cmd_score_company(_args: argparse.Namespace) -> int:
    ms = MacroScorer(config_hash="cli-macro")
    mctx = ms.score_macro(SAMPLE_MACRO_INPUTS)
    is_ = IndustryScorer(config_hash="cli-industry")
    isc = is_.score_industry(
        industry_id="半導體", industry_name="半導體",
        inputs=SAMPLE_INDUSTRY_INPUTS_AI,
        config={"macro_context": mctx.breakdown,
                "industry_macro_beta": {"liquidity": 0.5, "rates": 0.2}},
        constituent_count=7,
    )
    cs = CompanyScorer(sector_sensitivity={
        "半導體": {"liquidity": 0.05, "rates": 0.02, "geopolitics": 0.03},
    })
    cscore = cs.score_company(
        code="2330", name="台積電", sector="半導體",
        inputs=SAMPLE_COMPANY_INPUTS_2330,
        macro_context=mctx, industry_score=isc,
    )
    exp = explain_score(cscore.breakdown, top_n_dimensions=3)
    print("=== CompanyScore(2330 台積電) (sample, with cross-layer) ===")
    print(f"  raw_score:   {cscore.raw_score:+.2f}")
    print(f"  macro_adj:   {cscore.macro_adjustment:+.2f}")
    print(f"  industry_adj:{cscore.industry_adjustment:+.2f}")
    print(f"  final score: {cscore.score:+.2f}  conf: {cscore.confidence:.2f}")
    print(f"  summary: {exp['summary_zh']}")
    print()
    print("Dimensions:")
    for d in cscore.dimensions:
        print(f"  - {d.name:<20} score={d.score:+7.2f} weight={d.weight:.2f}")
    print()
    print(f"Cross-layer adjustments: {len(cscore.breakdown.cross_layer_adjustments)}")
    for a in cscore.breakdown.cross_layer_adjustments:
        print(f"  - {a.from_scorer} -> {a.to_scorer}: {a.adjustment:+.2f} ({a.reason})")
    return 0


def cmd_aggregate_signals(_args: argparse.Namespace) -> int:
    sigs = _build_sample_signals()
    engine = SignalEngine()
    aggregator = SignalAggregator()
    # Bucket by (entity_type, entity_id, signal_type) — sample has
    # distinct types so we get 2 separate buckets.
    by_bucket: dict[tuple[str, str, str], list] = {}
    for ws in engine.weight_all(sigs):
        key = (ws.signal.entity_type, ws.signal.entity_id, ws.signal.signal_type)
        by_bucket.setdefault(key, []).append(ws)
    print(f"=== Signal Aggregation (sample, {len(sigs)} signals) ===")
    for sig in sigs:
        print(f"  input: {sig.signal_id}  type={sig.signal_type}  "
              f"value={sig.value}  src={sig.source.source_id}")
    print()
    print(f"  weighted signals: {sum(len(v) for v in by_bucket.values())}  "
          f"buckets: {len(by_bucket)}")
    for key, ws_list in by_bucket.items():
        agg = aggregator.aggregate(ws_list)
        print(f"  bucket={key}  weighted_sum={agg.weighted_sum:+.3f}  "
              f"consensus={agg.direction_consensus}  "
              f"conf={agg.aggregate_confidence:.2f}")
    return 0


def cmd_graph_demo(_args: argparse.Namespace) -> int:
    s = _build_sample_graph()
    print("=== Graph Demo (sample research graph) ===")
    print(json.dumps(s.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_graph_trace(args: argparse.Namespace) -> int:
    s = _build_sample_graph()
    target = args.node or "score:company:2330:2026-07-08"
    tr = EvidenceTracer(s)
    chain = tr.trace(target, max_depth=args.max_depth,
                     direction=args.direction)
    print(f"=== EvidenceTrace from {target} "
          f"({args.direction}, max_depth={args.max_depth}) ===")
    print(chain.to_text())
    return 0


# ---------- argparse ----------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phase3.cli",
        description="Phase 3 Investment Intelligence Engine — CLI demo",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("score-macro", help="Score a sample macro factor bundle")
    p1.set_defaults(func=cmd_score_macro)

    p2 = sub.add_parser("score-industry", help="Score a sample industry sector")
    p2.set_defaults(func=cmd_score_industry)

    p3 = sub.add_parser("score-company", help="Score a sample company (with cross-layer)")
    p3.set_defaults(func=cmd_score_company)

    p4 = sub.add_parser("aggregate-signals",
                        help="Aggregate a small sample signal set")
    p4.set_defaults(func=cmd_aggregate_signals)

    p5 = sub.add_parser("graph-demo",
                        help="Build a sample graph and print stats")
    p5.set_defaults(func=cmd_graph_demo)

    p6 = sub.add_parser("graph-trace",
                        help="Trace evidence from a sample-graph node")
    p6.add_argument("--node", default="score:company:2330:2026-07-08",
                    help="Node id to trace from")
    p6.add_argument("--max-depth", type=int, default=5)
    p6.add_argument("--direction", choices=["upstream", "downstream"],
                    default="upstream")
    p6.set_defaults(func=cmd_graph_trace)

    # Phase 3B persistence subcommand. Gated by PHASE3B_ENABLED env
    # var (or --force) so it never runs by accident. The subcommand
    # is registered in argparse regardless of the flag — that way
    # `--help` documents it, and the flag is enforced at runtime
    # inside cmd_init_db. This means a Phase 3A user who doesn't
    # know about the flag sees the subcommand listed in --help but
    # gets a clear refusal when they try to run it.
    p7 = sub.add_parser(
        "init-db",
        help="Phase 3B: initialise the SQLite persistence database. "
             "Gated behind PHASE3B_ENABLED=1 or --force.",
    )
    p7.add_argument(
        "--db-path",
        default=None,
        help="Override the default database path "
             "(default: phase3/data/intelligence.db). The path is "
             "refused if it resolves to macro_history.db.",
    )
    p7.add_argument(
        "--force",
        action="store_true",
        help="Bypass the PHASE3B_ENABLED env-var gate. Use with care.",
    )
    p7.set_defaults(func=cmd_init_db)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
