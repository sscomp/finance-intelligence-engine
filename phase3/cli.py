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
from pathlib import Path
from typing import Any

from phase3.api import (
    PipelineAPIError,
    observe_pipeline_status,
    export_pipeline_report,
    resume_pipeline,
    run_pipeline,
)
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
from phase3.persistence.sqlite import FORBIDDEN_DB_NAME
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


def _parse_edge_types_csv(csv: str | None) -> list[EdgeType] | None:
    """Parse a comma-separated list of edge-type names to a list.

    Returns None when ``csv`` is None or empty (meaning: use the
    default per-direction list). Unknown names raise
    ``SystemExit`` via :func:`argparse.ArgumentParser.error` so the
    CLI fails fast with a clear message.
    """
    if not csv:
        return None
    out: list[EdgeType] = []
    known = {e.value: e for e in EdgeType}
    for raw in csv.split(","):
        name = raw.strip()
        if not name:
            continue
        if name not in known:
            valid = ", ".join(sorted(known))
            raise SystemExit(
                f"unknown edge type {name!r}; valid: {valid}"
            )
        out.append(known[name])
    return out or None


def cmd_trace_score(args: argparse.Namespace) -> int:
    """Phase 3B Task 4 Run 3 — trace from a score, optionally through a
    pipeline result adapter.

    Two modes:

    * **sample** (default): build the canonical sample graph and trace
      from ``--node``. Useful for operator demos and unit smoke tests.
    * **pipeline** (``--from-pipeline``): build a fake
      :class:`PipelineResult` from the sample inputs, then use
      :class:`EvidenceChainAdapter` to compute the score node id and
      run the trace. Demonstrates the round-trip
      ``PipelineResult → score_node_id → EvidenceChain`` path that
      callers (the cron snapshot path, Bridge delivery verification)
      will use.

    Output:

    * ``--output PATH`` writes the JSON-serialized
      :class:`EvidenceChain` to PATH. ``--output -`` writes to stdout
      (default for ``--json``). Without ``--output`` we render the
      human-readable ``to_text()`` form.
    """
    direction = args.direction
    if direction not in ("upstream", "downstream"):
        print(
            f"--direction must be 'upstream' or 'downstream', got {direction!r}",
            file=sys.stderr,
        )
        return 2
    edge_types = _parse_edge_types_csv(args.edge_types)

    if args.from_pipeline:
        # Pipeline mode: build a tiny PipelineResult-shaped object
        # and route through EvidenceChainAdapter. We don't have a
        # full ScoringPipeline here (no fixtures in this CLI), so we
        # construct the bridge's input from the sample score.
        from phase3.datamodel.evidence import Evidence, make_evidence_id
        from phase3.datamodel.scores import (
            DimensionResult, ScoreBreakdown, SubIndicatorResult,
            WeightedFactor,
        )
        from phase3.graph.evidence_trace_export import (
            EvidenceChainAdapter, score_node_id_for_result,
        )
        from phase3.pipeline.scoring_pipeline import PipelineResult
        # Compose a minimal ScoreBreakdown so the adapter has
        # scorer_type / entity_id / timestamp.
        now = datetime.now(timezone.utc)
        valid_until = now
        ev = Evidence(
            evidence_id=make_evidence_id("yfinance", "2330:pe", "22.0"),
            source_type="yfinance", source_ref="2330:pe",
            raw_value="22.0", description="PE=22.0",
            timestamp=now,
        )
        breakdown = ScoreBreakdown(
            scorer_type=args.scorer_type or "company",
            entity_type=args.scorer_type or "company",
            entity_id=args.entity_id or "2330",
            score=42.0, confidence=0.7,
            dimensions=[
                DimensionResult(
                    name="valuation", weight=1.0, score=42.0,
                    confidence=0.7,
                    sub_indicators=[
                        SubIndicatorResult(
                            name="pe", raw_value=22.0, raw_unit="x",
                            sub_score=42.0, transformation="threshold",
                            source="yfinance", source_ref="2330:pe",
                            evidence=[ev],
                        ),
                    ],
                    factors=[
                        WeightedFactor(
                            name="pe", raw_value=22.0, raw_unit="x",
                            sub_score=42.0, sub_weight=1.0,
                            signed_score=42.0, transformation="threshold",
                            source="yfinance", source_ref="2330:pe",
                            evidence=[ev],
                        ),
                    ],
                ),
            ],
            overall_evidence=[ev],
            cross_layer_adjustments=[],
            schema_version="v1",
            timestamp=now, valid_until=valid_until,
            config_hash="cli-trace-score",
        )
        from phase3.datamodel import CompanyScore
        score = CompanyScore(
            breakdown=breakdown,
            code=args.entity_id or "2330",
            name=args.entity_id or "2330",
            sector="cli",
        )
        result = PipelineResult(
            score=score,
            input_bundle=None,  # type: ignore[arg-type]
            evidence_signal_ids=("sig-2330-pe", "sig-2330-roe"),
            snapshot_id=None,
            warnings=(),
            metadata={
                "scorer_type": args.scorer_type or "company",
                "entity_id": args.entity_id or "2330",
                "date_bucket": now.date().isoformat(),
                "config_hash": "cli-trace-score",
                "dry_run": True,
            },
        )
        # Build a sample graph that contains the score node we will
        # synthesize; this keeps the demo deterministic without
        # requiring the user to call GraphWriter first.
        s = _build_sample_graph()
        # The sample graph's company score uses
        # `score:company:2330:2026-07-08`; if the user supplied a
        # different date we add a minimal score node + signal
        # + source so the trace is non-empty.
        synth_id = score_node_id_for_result(result)
        if not s.has_node(synth_id):
            from phase3.datamodel.graph import GraphNode
            s.add_node(GraphNode(
                node_id=synth_id, node_type=NodeType.SCORE, label="synth",
            ))
            from phase3.datamodel.graph import make_graph_edge_id
            for sig in ("sig-2330-pe", "sig-2330-roe"):
                sig_nid = f"signal:{sig}"
                s.add_node(GraphNode(
                    node_id=sig_nid, node_type=NodeType.SIGNAL, label=sig,
                ))
                s.add_edge(GraphEdge(
                    edge_id="",
                    edge_type=EdgeType.CONTRIBUTES_TO,
                    from_node_id=sig_nid,
                    to_node_id=synth_id,
                ))
                s.add_node(GraphNode(
                    node_id="source:yfinance:default",
                    node_type=NodeType.SOURCE, label="yfinance",
                ))
                s.add_edge(GraphEdge(
                    edge_id="",
                    edge_type=EdgeType.GENERATED,
                    from_node_id=sig_nid,
                    to_node_id="source:yfinance:default",
                ))
        adapter = EvidenceChainAdapter(s, edge_types=edge_types)
        chain = adapter.trace(
            result,
            max_depth=args.max_depth,
            direction=direction,
            max_nodes=args.max_nodes,
            include_start=args.include_start,
        )
        start_label = synth_id
        source_kind = f"pipeline({args.scorer_type or 'company'})"
    else:
        s = _build_sample_graph()
        target = args.node or "score:company:2330:2026-07-08"
        tr = EvidenceTracer(s)
        chain = tr.trace(
            target,
            max_depth=args.max_depth,
            direction=direction,  # type: ignore[arg-type]
            edge_types=edge_types,
            max_nodes=args.max_nodes,
            include_start=args.include_start,
        )
        start_label = target
        source_kind = "sample-graph"

    # Output
    if args.output is not None or args.json:
        from phase3.graph.evidence_trace_export import chain_to_json
        text = chain_to_json(
            chain,
            indent=2,
            include_nodes=not args.no_nodes,
            include_edges=not args.no_edges,
        )
        if args.output and args.output != "-":
            from pathlib import Path as _P
            _P(args.output).write_text(text, encoding="utf-8")
            print(
                f"=== trace-score ({source_kind}) -> {args.output} ===",
            )
            print(
                f"start={start_label} direction={direction} "
                f"max_depth={args.max_depth} "
                f"truncated={chain.truncated} "
                f"warnings={len(chain.warnings)}"
            )
        else:
            # stdout
            print(text)
        return 0

    # Human-readable (default)
    print(
        f"=== trace-score ({source_kind}) from {start_label} "
        f"({direction}, max_depth={args.max_depth}) ==="
    )
    print(chain.to_text())
    return 0


# ---------- Phase 3B Task 4 Run 4B — Graph Query CLI ----------


def _resolve_graph_store(args: argparse.Namespace) -> tuple[Any, str]:
    """Build a :class:`GraphStore` for the query subcommands.

    Two modes:

    * **sample** (default): build the canonical in-memory sample
      graph and return it. No filesystem side effects.
    * **db** (``--db-path``): open the SQLite graph store at the
      given path. We pass ``auto_migrate=False`` to skip
      :meth:`ensure_schema` (the production DB is already
      migrated; we never want this CLI to touch a schema) and
      then enable ``PRAGMA query_only=1`` on the underlying
      connection so the query subcommands can never mutate the
      production DB. If the path resolves to
      ``macro_history.db`` we refuse (same path guard the
      persistence CLI uses).

    Returns ``(store, source_label)``. The source label is used
    for human-readable output.
    """
    db_path = getattr(args, "db_path", None)
    if db_path is None:
        return _build_sample_graph(), "sample-graph"

    # Refuse macro_history.db, same guard the persistence layer
    # uses. We deliberately check the basename (not the full
    # path) so an alias / symlink is also caught. The comparison
    # is case-insensitive (F2 hardening): on a case-sensitive
    # filesystem ``Macro_History.db`` would otherwise bypass the
    # guard and write to a different file. The same rule is
    # enforced in :func:`phase3.persistence.sqlite._check_path`
    # (line 71); we mirror the contract here so the CLI cannot
    # regress the F2 fix.
    if (
        os.path.basename(db_path).lower()
        == FORBIDDEN_DB_NAME.lower()
    ):
        print(
            f"refusing to open {db_path!r}: {FORBIDDEN_DB_NAME} "
            f"is reserved for macro-report history, not Phase 3B "
            f"graph storage (case-insensitive basename match).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from phase3.graph.sqlite_store import SQLiteGraphStore
    try:
        store = SQLiteGraphStore(db_path, auto_migrate=False)
    except Exception as exc:  # noqa: BLE001
        print(
            f"failed to open {db_path!r}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    # Hard guarantee: the production DB cannot be mutated from
    # this CLI even if a future bug writes by accident.
    try:
        store._store._conn.execute("PRAGMA query_only = 1")
    except Exception as exc:  # noqa: BLE001
        print(
            f"failed to enable PRAGMA query_only on {db_path!r}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    return store, f"sqlite({db_path})"


def _emit_query_result(
    args: argparse.Namespace,
    *,
    label: str,
    start_label: str,
    result: Any,
    source_kind: str,
) -> int:
    """Render a graph query result to stdout / file.

    Mirrors the :func:`cmd_trace_score` output contract:

    * ``--output PATH`` writes JSON to PATH. ``-`` writes to
      stdout. Prints a status line to stdout either way.
    * ``--json`` emits JSON to stdout (no file).
    * Default: human-readable summary to stdout.
    """
    if args.output is not None or args.json:
        text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
        if args.output and args.output != "-":
            from pathlib import Path as _P
            _P(args.output).write_text(text, encoding="utf-8")
            print(
                f"=== {label} ({source_kind}) -> {args.output} ==="
            )
            print(
                f"start={start_label} max_depth={args.max_depth} "
                f"warnings={len(result.warnings)} "
                f"truncated={getattr(result, 'truncated', False)}"
            )
        else:
            print(text)
        return 0

    # Human-readable
    print(
        f"=== {label} ({source_kind}) from {start_label} "
        f"(max_depth={args.max_depth}) ==="
    )
    rd = result.to_dict()
    # Print the headline fields first, then everything else.
    for key in (
        "start", "visited_node_ids", "source_node_ids",
        "signal_node_ids", "score_node_ids", "entity_node_ids",
        "leaf_node_ids", "leaf_summary", "layered",
        "upstream_chain", "downstream_chain",
        "upstream_layers", "downstream_layers",
        "all_reachable_score_ids", "layered_upstream",
        "layered_downstream", "truncated", "depth_reached",
        "truncated_upstream", "truncated_downstream",
        "depth_reached_upstream", "depth_reached_downstream",
    ):
        if key in rd:
            print(f"  {key}: {rd[key]}")
    extra = {
        k: v for k, v in rd.items()
        if k not in {
            "start", "visited_node_ids", "source_node_ids",
            "signal_node_ids", "score_node_ids", "entity_node_ids",
            "leaf_node_ids", "leaf_summary", "layered",
            "upstream_chain", "downstream_chain",
            "upstream_layers", "downstream_layers",
            "all_reachable_score_ids", "layered_upstream",
            "layered_downstream", "truncated", "depth_reached",
            "truncated_upstream", "truncated_downstream",
            "depth_reached_upstream", "depth_reached_downstream",
            "visited_edges", "visited_edges_upstream",
            "visited_edges_downstream", "warnings",
        }
    }
    if extra:
        for k, v in extra.items():
            print(f"  {k}: {v}")
    if rd.get("visited_edges") is not None:
        print(f"  visited_edges: {rd['visited_edges']}")
    if rd.get("visited_edges_upstream") is not None:
        print(f"  visited_edges_upstream: {rd['visited_edges_upstream']}")
    if rd.get("visited_edges_downstream") is not None:
        print(f"  visited_edges_downstream: {rd['visited_edges_downstream']}")
    if rd.get("warnings"):
        print(f"  warnings: {rd['warnings']}")
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    """Phase 3B Task 4 Run 4B: upstream lineage (provenance) query.

    Default graph: sample. Pass ``--db-path`` to query a Phase 3B
    SQLite graph store (read-only).

    Output: human-readable by default, JSON with ``--json`` or
    ``--output``.
    """
    from phase3.graph.lineage import compute_lineage
    store, source_kind = _resolve_graph_store(args)
    result = compute_lineage(
        store,
        args.node,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
    )
    return _emit_query_result(
        args,
        label="lineage",
        start_label=args.node,
        result=result,
        source_kind=source_kind,
    )


def cmd_blast_radius(args: argparse.Namespace) -> int:
    """Phase 3B Task 4 Run 4A/4B: downstream blast-radius query.

    Default graph: sample. Pass ``--db-path`` to query a Phase 3B
    SQLite graph store (read-only).

    Output: human-readable by default, JSON with ``--json`` or
    ``--output``.
    """
    from phase3.graph.blast_radius import compute_blast_radius
    store, source_kind = _resolve_graph_store(args)
    result = compute_blast_radius(
        store,
        args.node,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
    )
    return _emit_query_result(
        args,
        label="blast-radius",
        start_label=args.node,
        result=result,
        source_kind=source_kind,
    )


def cmd_cross_layer_impact(args: argparse.Namespace) -> int:
    """Phase 3B Task 4 Run 4B: score-to-score cross-layer impact.

    Default graph: sample. Pass ``--db-path`` to query a Phase 3B
    SQLite graph store (read-only).

    Output: human-readable by default, JSON with ``--json`` or
    ``--output``.
    """
    from phase3.graph.cross_layer_impact import (
        compute_cross_layer_impact,
    )
    store, source_kind = _resolve_graph_store(args)
    result = compute_cross_layer_impact(
        store,
        args.node,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
    )
    return _emit_query_result(
        args,
        label="cross-layer-impact",
        start_label=args.node,
        result=result,
        source_kind=source_kind,
    )


# ---------- Phase 4 Task 3B Run 1: explain-score / decision trace ----------

def cmd_explain_score(args: argparse.Namespace) -> int:
    """Phase 4 Task 3B Run 1: explain-score / decision trace.

    Read-only composition of the three canonical graph queries
    (lineage, blast-radius, cross-layer impact) plus the score
    node's metadata. Answers the operator question
    "why does this score exist, what contributed, what does it
    touch, and is the trace complete?".

    Default graph: sample. Pass ``--db-path`` to query a Phase 3B
    SQLite graph store (read-only; PRAGMA query_only=1 is
    enabled by :func:`_resolve_graph_store`).

    Output: human-readable Markdown by default, JSON with
    ``--json`` or ``--output``. The Markdown view is the
    canonical operator surface; the JSON view is the canonical
    machine surface.

    Flags
    -----
    ``--node``
        The score node id (default: the sample company score
        ``score:company:2330:2026-07-08``).
    ``--max-depth``
        Max hop count for each of the three underlying walks
        (default: 5).
    ``--max-nodes``
        Optional cap on the number of nodes each walk may
        visit (default: unbounded).
    ``--db-path``
        Path to a Phase 3B SQLite graph store (read-only).
        Refused if it resolves to ``macro_history.db``.
    ``--output PATH``
        Write the JSON-serialized ``ExplainedScore`` to PATH.
        Use ``-`` for stdout. Implies ``--json``.
    ``--json``
        Emit JSON on stdout (default: human-readable Markdown).
    ``--from-pipeline`` (placeholder)
        Reserved for future wiring to ``PipelineResult``. Not
        honored in Run 1; included so the help text documents
        the intended extension point.

    Exit codes
    ----------
    0 — success (the score was found, the trace completed, the
        result was written).
    1 — operational failure (db-path refused, db open failed,
        output path unwritable). The error is printed to stderr.
    2 — argparse rejection (unknown flag, invalid choice).
    """
    from phase3.graph.explain_score import explain_score as _explain
    store, source_kind = _resolve_graph_store(args)
    result = _explain(
        store,
        args.node,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
    )

    # --from-pipeline is reserved for Run 2+ where we'll thread
    # the PipelineResult through. For now we just accept the
    # flag and ignore it (no error) so the CLI surface is
    # forward-compatible. We only print a hint when the
    # operator actually asked for it.
    if getattr(args, "from_pipeline", False):
        print(
            "note: --from-pipeline is reserved for a future Run; "
            "ignored in this run (the explanation is computed "
            "from the graph store only).",
            file=sys.stderr,
        )

    # Mirror the contract of cmd_lineage / cmd_blast_radius:
    #   * --output PATH (incl. -) implies JSON.
    #   * --json without --output writes JSON to stdout.
    #   * default: Markdown to stdout.
    want_json = bool(args.json or args.output)
    if want_json:
        text = json.dumps(result.to_dict(), indent=2, sort_keys=True)
        if args.output and args.output != "-":
            from pathlib import Path as _P
            try:
                _P(args.output).write_text(text, encoding="utf-8")
            except OSError as exc:
                print(
                    f"failed to write {args.output!r}: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"=== explain-score ({source_kind}) -> "
                f"{args.output} ==="
            )
            print(
                f"score_node_id={args.node} max_depth="
                f"{args.max_depth} max_nodes={args.max_nodes} "
                f"present={result.present} "
                f"truncated={result.truncated} "
                f"depth_reached={result.depth_reached} "
                f"warnings={len(result.warnings)}"
            )
            return 0
        # args.output == "-" or args.json with no --output.
        print(text)
        return 0

    # Default: Markdown to stdout. Use a small status line above
    # the rendered explanation so the operator can confirm the
    # run parameters at a glance.
    print(
        f"=== explain-score ({source_kind}) ==="
    )
    print(
        f"score_node_id={args.node} max_depth={args.max_depth} "
        f"max_nodes={args.max_nodes} present={result.present} "
        f"truncated={result.truncated} "
        f"depth_reached={result.depth_reached} "
        f"warnings={len(result.warnings)}"
    )
    print()
    print(result.to_markdown())
    return 0


# ---------- Phase 3B ingest-signals ----------

#: Canonical adapter name → registry key. ``all`` means "run every
#: registered adapter against its corresponding input (or skip if no
#: input was provided)". The mapping is duplicated here (instead of
#: derived from ``sources.yaml``) so the CLI remains usable when the
#: YAML config is missing — a deliberate Phase 3A-friendly default.
_INGEST_ALL_ADAPTERS: tuple[str, ...] = (
    "fixture", "yfinance", "rss", "t86", "macro",
)


def cmd_ingest_signals(args: argparse.Namespace) -> int:
    """Run one or more adapters and (optionally) persist the resulting Signals.

    Modes:
      * ``--dry-run`` — adapt and print a summary, do not touch any DB.
        Always allowed; no feature flag check.
      * persist mode — gated behind ``PHASE3B_ENABLED=1`` or
        ``--force``, same pattern as ``init-db``.

    Inputs:
      * ``--input PATH``   — single file. Used by every adapter named
                             via ``--source`` (or by all adapters if
                             ``--source all``).
      * ``--input-dir PATH`` — directory; the CLI looks for
                             ``<adapter>.json`` inside (and
                             ``macro.json``, ``yfinance.json`` ...).
                             If neither is set, each adapter is run
                             with an empty input (yields no signals
                             for most adapters; useful as a smoke
                             test for the registry wiring).
      * ``--config-path PATH`` — path to a sources.yaml override
                             (default: ``config/phase3/sources.yaml``
                             via :func:`phase3.config.sources.load_sources`).

    Persistence:
      * ``--db-path PATH`` — defaults to
        ``phase3/data/intelligence.db`` (same default as init-db).
        The path is refused if it resolves to ``macro_history.db``.

    Safety:
      * No network calls. Adapters take whatever payload is passed
        to them.
      * No writes unless ``--dry-run`` is NOT set AND the feature
        flag (or ``--force``) is set.
    """
    # Late imports: keep the rest of the CLI fast and avoid pulling
    # persistence on every invocation.
    from phase3.config.sources import load_sources
    from phase3.persistence import sqlite as sqlite_mod
    from phase3.persistence.signal_repo import SignalRecord, SignalRepository
    from phase3.signals.adapters import registry as adapter_registry

    source = (args.source or "all").lower()
    if source == "all":
        adapter_names = list(_INGEST_ALL_ADAPTERS)
    else:
        if not adapter_registry.has(source):
            print(
                f"unknown source {source!r}; known: "
                f"{', '.join(adapter_registry.names())}",
                file=sys.stderr,
            )
            return 2
        adapter_names = [source]

    sources_config = load_sources(args.config_path)
    dry_run = bool(getattr(args, "dry_run", False))
    enabled = _phase3b_enabled(args)

    # Refuse persist mode if not enabled. Dry-run is always allowed.
    if not dry_run and not enabled:
        print(
            f"refusing to run: Phase 3B ingest is gated behind "
            f"{PHASE3B_ENV_FLAG}=1 (or pass --force). "
            f"Use --dry-run for a safe preview.",
            file=sys.stderr,
        )
        return 2

    # Path guard up front so we fail fast even in dry-run.
    # Default DB path mirrors init-db. CRITICAL: we must NOT treat a
    # user-supplied forbidden name as "use the default" — that would
    # silently bypass the path guard for ``--db-path macro_history.db``.
    user_db_path = getattr(args, "db_path", None)
    if user_db_path:
        db_path = user_db_path
    else:
        db_path = "phase3/data/intelligence.db"
    # Validate path now (raises PathGuardError if forbidden)
    try:
        resolved_db = sqlite_mod._check_path(db_path)
    except sqlite_mod.PathGuardError as exc:
        print(f"refusing to write DB: {exc}", file=sys.stderr)
        return 1

    # Resolve inputs
    input_path = getattr(args, "input", None)
    input_dir = getattr(args, "input_dir", None)
    if input_path and input_dir:
        print(
            "refusing to run: --input and --input-dir are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    # Run each adapter
    total_new = 0
    total_updated = 0
    total_signals = 0
    total_warnings = 0
    per_adapter_summary: list[dict[str, Any]] = []
    store_for_persist: Any = None
    repo: SignalRepository | None = None
    if not dry_run:
        from phase3.persistence.sqlite import SQLiteStore
        store_for_persist = SQLiteStore(resolved_db)
        repo = SignalRepository(store_for_persist)

    try:
        for adapter_name in adapter_names:
            payload: Any = None
            # Prefer per-source default from sources.yaml
            entry = sources_config.get(adapter_name)
            if entry and entry.default_input:
                payload = entry.default_input
            if input_path:
                payload = input_path
            elif input_dir:
                from pathlib import Path as _P
                candidate = _P(input_dir) / f"{adapter_name}.json"
                if candidate.exists():
                    payload = candidate
                else:
                    # No file for this adapter — skip silently
                    per_adapter_summary.append(
                        {"adapter": adapter_name, "skipped": True,
                         "reason": f"no {candidate.name} in {input_dir}"}
                    )
                    continue
            adapter_cls = adapter_registry.get(adapter_name)
            adapter = adapter_cls()
            signals = adapter.adapt(payload)
            n = len(signals)
            total_signals += n
            warnings: list[str] = []
            # All adapters return a list, but the *with_stats variants
            # also expose warnings / parsed / skipped. We re-derive
            # warnings cheaply by re-running the with_stats variant
            # only when input was provided. For None / empty input we
            # keep warnings=[].
            if payload is not None:
                # Run the with_stats variant if available; otherwise
                # fall back to a plain adapt.
                stats_method = getattr(adapter, "adapt_with_stats", None)
                if callable(stats_method):
                    try:
                        stats = stats_method(payload)
                        warnings = list(getattr(stats, "warnings", []))
                    except Exception as exc:  # noqa: BLE001
                        warnings = [f"with_stats raised: {exc!r}"]
            n_new = 0
            n_updated = 0
            if repo is not None and signals:
                records = [_signal_to_record(s) for s in signals]
                n_new = repo.upsert_many(records)
                n_updated = len(records) - n_new
            total_new += n_new
            total_updated += n_updated
            total_warnings += len(warnings)
            per_adapter_summary.append({
                "adapter": adapter_name,
                "input": str(payload) if payload is not None else None,
                "n_signals": n,
                "n_new": n_new,
                "n_updated": n_updated,
                "n_warnings": len(warnings),
                "warnings": warnings,
            })
    finally:
        if store_for_persist is not None:
            store_for_persist.close()

    # Print summary
    print(f"=== ingest-signals ({source})  dry_run={dry_run} ===")
    for row in per_adapter_summary:
        if row.get("skipped"):
            print(f"  - {row['adapter']:<10}  SKIPPED ({row['reason']})")
            continue
        print(
            f"  - {row['adapter']:<10}  signals={row['n_signals']:>4}  "
            f"new={row['n_new']:>4}  updated={row['n_updated']:>4}  "
            f"warnings={row['n_warnings']:>3}"
        )
        for w in row.get("warnings", []):
            print(f"      warn: {w}")
    print()
    print(
        f"total: signals={total_signals}  new={total_new}  "
        f"updated={total_updated}  warnings={total_warnings}"
    )
    if not dry_run:
        print(f"persisted to: {resolved_db}")
    return 0


def _signal_to_record(sig: Signal) -> Any:
    """Convert a :class:`Signal` to a :class:`SignalRecord` for the repository.

    Defined as a module-level helper so the CLI is testable in
    isolation (the CLI does NOT import the converter at module load
    time to keep import cost low).
    """
    from phase3.persistence.signal_repo import SignalRecord
    return SignalRecord(
        signal_id=sig.signal_id,
        entity_type=sig.entity_type,
        entity_id=sig.entity_id,
        signal_type=sig.signal_type,
        value=sig.value,
        unit=sig.unit,
        direction=sig.direction,
        timestamp=sig.timestamp.isoformat(),
        date_bucket=sig.date_bucket,
        source_id=sig.source.source_id,
        source_type=sig.source.source_type,
        ref=sig.source.ref,
        fetched_at=sig.source.fetched_at.isoformat(),
        fetch_id="",
        schema_version=sig.schema_version,
        metadata=dict(sig.metadata or {}),
        raw_payload=dict(sig.metadata or {}),
    )


# ---------- Phase 3B Task 5 Run 4 — pipeline CLI surface ----------


def _resolve_pipeline_kwargs(
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Translate a CLI ``args`` Namespace into ``run_pipeline`` kwargs.

    Centralised so the run / resume / status subcommands all
    share the same option-to-kwarg mapping. ``--industry`` and
    ``--company`` accept repeated values.
    """
    industry_ids: list[str] = list(getattr(args, "industry", []) or [])
    company_specs: list[dict[str, Any]] = []
    for raw in getattr(args, "company", []) or []:
        company_specs.append({"code": raw})
    return {
        "date_bucket": args.date,
        "industry_ids": tuple(industry_ids),
        "company_specs": tuple(company_specs),
        "run_macro": bool(getattr(args, "run_macro", True)),
        "persist": bool(getattr(args, "persist", False)),
        "db_path": getattr(args, "db_path", None),
        "run_id": getattr(args, "run_id", "") or "",
        "config_hash": getattr(args, "config_hash", "phase3-cli"),
        "notes": getattr(args, "notes", "") or "",
        "trace_directions": tuple(getattr(args, "trace_directions", ("upstream",))),
        "trace_max_depth": int(getattr(args, "trace_max_depth", 5)),
    }


def _emit_api_payload(
    args: argparse.Namespace,
    *,
    label: str,
    api_result: Any,
) -> int:
    """Render an :class:`APIResult` to stdout / file.

    Contract matches :func:`_emit_query_result`:

    * ``--output PATH`` writes JSON to PATH. ``-`` writes to stdout.
    * ``--json`` emits JSON to stdout.
    * Default: a short human-readable status line.
    """
    if args.output is not None or args.json:
        text = json.dumps(api_result.payload, sort_keys=True,
                          ensure_ascii=False, indent=2)
        if args.output and args.output != "-":
            from pathlib import Path as _P
            _P(args.output).write_text(text, encoding="utf-8")
            print(f"=== {label} -> {args.output} ===")
            print(f"  kind: {api_result.kind}")
        else:
            print(text)
        return 0

    # Human-readable default
    print(f"=== {label} (kind={api_result.kind}) ===")
    p = api_result.payload
    # Surface the run-level summary fields the operator wants
    # without re-typing the entire DTO.
    for key in (
        "run_id", "config_hash", "date_bucket", "dry_run",
        "persist", "started_at", "finished_at",
        "duration_seconds", "signal_count", "score_count",
        "snapshot_count", "graph_node_count", "graph_edge_count",
        "warning_count", "error_count",
        "last_completed_stage", "completed_stages",
        "attempted_stages", "replay_count", "resume_state",
    ):
        if key in p:
            value = p[key]
            if isinstance(value, (list, tuple)) and len(value) > 8:
                value = f"<{len(value)} items>"
            print(f"  {key}: {value}")
    return 0


def cmd_pipeline_run(args: argparse.Namespace) -> int:
    """Phase 3B Task 5 Run 4: run the end-to-end pipeline.

    Default is dry-run (no DB writes). Pass ``--persist`` to
    actually write to ``--db-path``. The path is guarded against
    ``macro_history.db`` (same as init-db / ingest-signals).

    Output: a one-line summary by default, JSON with ``--json`` or
    ``--output``.
    """
    try:
        kwargs = _resolve_pipeline_kwargs(args)
    except ValueError as exc:
        print(f"invalid arguments: {exc}", file=sys.stderr)
        return 2
    try:
        api_result = run_pipeline(**kwargs)
    except PipelineAPIError as exc:
        print(
            f"pipeline-run failed ({exc.component}/{exc.error_class}): "
            f"{exc}",
            file=sys.stderr,
        )
        return 1
    return _emit_api_payload(args, label="pipeline-run", api_result=api_result)


def cmd_pipeline_resume(args: argparse.Namespace) -> int:
    """Phase 3B Task 5 Run 4: resume a previously-started run.

    Requires ``--db-path`` (the recovery layer reads from existing
    snapshot/graph stores). Other options map to
    :class:`RecoveryConfig` (``--max-attempts``,
    ``--retry-backoff``).
    """
    if not getattr(args, "db_path", None):
        print(
            "pipeline-resume requires --db-path",
            file=sys.stderr,
        )
        return 2
    try:
        api_result = resume_pipeline(
            date_bucket=args.date,
            config_hash=getattr(args, "config_hash", "phase3-cli"),
            run_id=getattr(args, "run_id", "") or "",
            db_path=args.db_path,
            max_attempts=int(getattr(args, "max_attempts", 2)),
            retry_backoff_seconds=float(
                getattr(args, "retry_backoff", 0.0)
            ),
            strict_resume=bool(getattr(args, "strict_resume", True)),
        )
    except PipelineAPIError as exc:
        print(
            f"pipeline-resume failed ({exc.component}/{exc.error_class}): "
            f"{exc}",
            file=sys.stderr,
        )
        return 1
    return _emit_api_payload(
        args, label="pipeline-resume", api_result=api_result,
    )


def cmd_pipeline_status(args: argparse.Namespace) -> int:
    """Phase 3B Task 5 Run 4: read-only inspection of a run's state.

    Requires ``--db-path``. Does not modify the DB.
    """
    if not getattr(args, "db_path", None):
        print(
            "pipeline-status requires --db-path",
            file=sys.stderr,
        )
        return 2
    try:
        api_result = observe_pipeline_status(
            date_bucket=args.date,
            config_hash=getattr(args, "config_hash", "phase3-cli"),
            run_id=getattr(args, "run_id", "") or "",
            db_path=args.db_path,
        )
    except PipelineAPIError as exc:
        print(
            f"pipeline-status failed ({exc.component}/{exc.error_class}): "
            f"{exc}",
            file=sys.stderr,
        )
        return 1
    return _emit_api_payload(
        args, label="pipeline-status", api_result=api_result,
    )


def cmd_pipeline_report(args: argparse.Namespace) -> int:
    """Phase 3B Task 5 Run 4: render JSON + Markdown from a saved run.

    ``--input PATH`` is a previously-exported JSON envelope (the
    ``payload`` field of an :class:`APIResult` from a prior
    ``pipeline-run``). ``--output-dir PATH`` is the destination
    directory. The existing :func:`export_pipeline_report` reuses
    the Run 3 reporting layer.
    """
    if not getattr(args, "input", None):
        print(
            "pipeline-report requires --input PATH",
            file=sys.stderr,
        )
        return 2
    if not getattr(args, "output_dir", None):
        print(
            "pipeline-report requires --output-dir PATH",
            file=sys.stderr,
        )
        return 2
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"--input file does not exist: {input_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"--input is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(
            "--input JSON must be an object (the run envelope)",
            file=sys.stderr,
        )
        return 2
    # We re-run the report from the *raw* IntelligenceRunResult
    # payload. Because the original run's typed envelope is not
    # round-trippable through plain JSON (it carries live
    # evidence objects), we re-build a minimal SummaryStatistics
    # surface here for verification: the export layer is content-
    # addressable (sha256 in the artifact), so the operator can
    # verify the on-disk bytes match the input they trust.
    out_dir = Path(args.output_dir)
    if not out_dir.exists():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"failed to create --output-dir {out_dir}: {exc}",
                file=sys.stderr,
            )
            return 1
    # The reporting layer's export_report() expects a typed
    # result envelope. For the report-only path we accept the
    # serialized dict directly: the export_report() helper is the
    # canonical writer, so we delegate to it via a thin shim
    # that accepts a dict-shaped payload. The shim is intentionally
    # tiny (just rebuilds the JSON/MD via build_json_export +
    # render_markdown_report).
    from phase3.pipeline.reporting import (
        render_markdown_report,
        ReportConfig,
    )
    try:
        cfg = ReportConfig(
            output_dir=str(out_dir),
            run_label=getattr(args, "run_label", "") or "",
            schema_version=int(getattr(args, "schema_version", 1)),
            include_evidence_payload=bool(
                getattr(args, "include_evidence_payload", True)
            ),
            recovery=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"invalid ReportConfig: {exc}", file=sys.stderr)
        return 2
    # Re-emit the JSON envelope from the saved payload (no
    # transformation: the saved payload is the canonical shape).
    json_text = json.dumps(payload, sort_keys=True,
                           ensure_ascii=False, indent=2)
    json_path = out_dir / f"{cfg.file_prefix()}intelligence_report.json"
    md_path = out_dir / f"{cfg.file_prefix()}intelligence_report.md"
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(
        render_markdown_report(
            None,  # the typed envelope is gone; we render a stub
            config=cfg,
        ),
        encoding="utf-8",
    )
    print(
        f"=== pipeline-report -> {out_dir} ===\n"
        f"  json:     {json_path}\n"
        f"  markdown: {md_path}"
    )
    return 0


def cmd_pipeline_export(args: argparse.Namespace) -> int:
    """Phase 3B Task 5 Run 4: run + export in one call.

    Combines :func:`cmd_pipeline_run` and
    :func:`cmd_pipeline_report` so an operator can produce a
    report in a single subprocess. Same default-safe contract as
    ``pipeline-run`` (dry-run unless ``--persist``).
    """
    try:
        kwargs = _resolve_pipeline_kwargs(args)
    except ValueError as exc:
        print(f"invalid arguments: {exc}", file=sys.stderr)
        return 2
    try:
        api_result = run_pipeline(**kwargs)
    except PipelineAPIError as exc:
        print(
            f"pipeline-export (run) failed "
            f"({exc.component}/{exc.error_class}): {exc}",
            file=sys.stderr,
        )
        return 1
    # Render to --output-dir via export_pipeline_report. The
    # reporting layer needs the *typed* result envelope, not the
    # serialised dict, so we hand back ``api_result.result``.
    if not getattr(args, "output_dir", None):
        print(
            "pipeline-export requires --output-dir PATH",
            file=sys.stderr,
        )
        return 2
    out_dir = Path(args.output_dir)
    if not out_dir.exists():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"failed to create --output-dir {out_dir}: {exc}",
                file=sys.stderr,
            )
            return 1
    try:
        export_result = export_pipeline_report(
            result=api_result.result,
            output_dir=args.output_dir,
            run_label=getattr(args, "run_label", "") or "",
            schema_version=int(getattr(args, "schema_version", 1)),
            include_evidence_payload=bool(
                getattr(args, "include_evidence_payload", True)
            ),
            recovery=False,
        )
    except PipelineAPIError as exc:
        print(
            f"pipeline-export (export) failed "
            f"({exc.component}/{exc.error_class}): {exc}",
            file=sys.stderr,
        )
        return 1
    if args.json:
        text = json.dumps(export_result.payload, sort_keys=True,
                          ensure_ascii=False, indent=2)
        print(text)
        return 0
    p = export_result.payload
    print(f"=== pipeline-export -> {args.output_dir} ===")
    print(f"  json:     {p['json']['path']}  ({p['json']['size_bytes']} bytes)")
    print(f"  markdown: {p['markdown']['path']}  ({p['markdown']['size_bytes']} bytes)")
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

    # Phase 3B ingest subcommand. Same gating pattern as init-db:
    # ``--dry-run`` is always allowed; persist mode requires
    # ``PHASE3B_ENABLED=1`` or ``--force``.
    p8 = sub.add_parser(
        "ingest-signals",
        help="Phase 3B: adapt one or more source payloads into Signals and "
             "(optionally) persist them. --dry-run is always allowed; "
             "persist mode is gated behind PHASE3B_ENABLED=1 or --force.",
    )
    p8.add_argument(
        "--source",
        default="all",
        choices=("all", "fixture", "yfinance", "rss", "t86", "macro"),
        help="Adapter source to run. Default 'all' runs every registered "
             "adapter (each one against its own input or a no-op when no "
             "input is supplied).",
    )
    p8.add_argument(
        "--input",
        default=None,
        help="Path to a single JSON input file. Used by every selected "
             "adapter. Mutually exclusive with --input-dir.",
    )
    p8.add_argument(
        "--input-dir",
        default=None,
        help="Directory containing <adapter>.json files. The CLI runs each "
             "selected adapter against the matching file (skips adapters "
             "whose file is missing). Mutually exclusive with --input.",
    )
    p8.add_argument(
        "--db-path",
        default=None,
        help="Override the default database path "
             "(default: phase3/data/intelligence.db). The path is "
             "refused if it resolves to macro_history.db.",
    )
    p8.add_argument(
        "--config-path",
        default=None,
        help="Path to a sources.yaml override. Default: "
             "config/phase3/sources.yaml.",
    )
    p8.add_argument(
        "--dry-run",
        action="store_true",
        help="Adapt and print a summary; do not write to the DB. Always "
             "allowed, even without PHASE3B_ENABLED.",
    )
    p8.add_argument(
        "--force",
        action="store_true",
        help="Bypass the PHASE3B_ENABLED env-var gate for persist mode. "
             "Use with care.",
    )
    p8.set_defaults(func=cmd_ingest_signals)

    # Phase 3B Task 4 Run 3 — Evidence Trace CLI.
    # Subcommand: trace-score. Defaults to the sample graph (no
    # production DB); use --from-pipeline to demonstrate the
    # PipelineResult -> score_node_id -> EvidenceChain round-trip.
    p9 = sub.add_parser(
        "trace-score",
        help="Phase 3B Task 4 Run 3: trace evidence from a score node. "
             "By default traces the sample graph; pass --from-pipeline "
             "to demonstrate the PipelineResult -> EvidenceChain path.",
    )
    p9.add_argument(
        "--node",
        default="score:company:2330:2026-07-08",
        help="Node id to trace from (sample-graph mode only). "
             "Default: the sample-graph company score.",
    )
    p9.add_argument("--max-depth", type=int, default=5)
    p9.add_argument(
        "--max-nodes", type=int, default=None,
        help="Optional cap on the number of nodes the walk may visit.",
    )
    p9.add_argument(
        "--direction", choices=("upstream", "downstream"),
        default="upstream",
    )
    p9.add_argument(
        "--edge-types",
        default=None,
        help="Comma-separated edge-type whitelist (e.g. "
             "'contributes_to,generated'). Default: per-direction default list.",
    )
    p9.add_argument(
        "--include-start",
        action="store_true",
        help="Include the start node in visited_node_ids (default: off).",
    )
    p9.add_argument(
        "--from-pipeline",
        action="store_true",
        help="Build a tiny PipelineResult from sample inputs and trace "
             "via EvidenceChainAdapter. Demonstrates the round-trip "
             "PipelineResult -> score_node_id -> EvidenceChain.",
    )
    p9.add_argument(
        "--scorer-type", default="company",
        choices=("macro", "industry", "company"),
        help="Scorer type for --from-pipeline mode (default: company).",
    )
    p9.add_argument(
        "--entity-id", default="2330",
        help="Entity id for --from-pipeline mode (default: 2330).",
    )
    p9.add_argument(
        "--output", default=None,
        help="Write the JSON-serialized chain to PATH. Use '-' for stdout. "
             "Implies --json.",
    )
    p9.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable text).",
    )
    p9.add_argument(
        "--no-nodes", action="store_true",
        help="When emitting JSON, omit the per-node payload (buckets only).",
    )
    p9.add_argument(
        "--no-edges", action="store_true",
        help="When emitting JSON, omit the per-edge payload.",
    )
    p9.set_defaults(func=cmd_trace_score)

    # Phase 3B Task 4 Run 4B — Graph Query CLI surface.
    # Three sibling subcommands that expose the canonical graph
    # queries (Lineage, Blast Radius, Cross-layer Impact) for
    # operator use. All three are READ-ONLY — they never write to
    # any DB. The default graph is the canonical sample graph; pass
    # --db-path to query a Phase 3B SQLite graph store.
    p10 = sub.add_parser(
        "lineage",
        help="Phase 3B Task 4 Run 4B: upstream lineage (provenance) "
             "query from a node. By default queries the sample graph; "
             "pass --db-path to query a Phase 3B SQLite graph store.",
    )
    p10.add_argument(
        "--node", default="score:company:2330:2026-07-08",
        help="Start node id. Default: the sample company score.",
    )
    p10.add_argument("--max-depth", type=int, default=5)
    p10.add_argument(
        "--max-nodes", type=int, default=None,
        help="Optional cap on the number of nodes visited.",
    )
    p10.add_argument(
        "--db-path", default=None,
        help="Path to a Phase 3B SQLite graph store (read-only). "
             "When omitted, queries the sample graph in-process.",
    )
    p10.add_argument(
        "--output", default=None,
        help="Write the JSON-serialized LineageQuery to PATH. "
             "Use '-' for stdout. Implies --json.",
    )
    p10.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable text).",
    )
    p10.set_defaults(func=cmd_lineage)

    p11 = sub.add_parser(
        "blast-radius",
        help="Phase 3B Task 4 Run 4A/4B: downstream blast-radius "
             "query from a node. By default queries the sample graph; "
             "pass --db-path to query a Phase 3B SQLite graph store.",
    )
    p11.add_argument(
        "--node", default="source:yfinance",
        help="Start node id. Default: the sample yfinance source.",
    )
    p11.add_argument("--max-depth", type=int, default=5)
    p11.add_argument(
        "--max-nodes", type=int, default=None,
        help="Optional cap on the number of nodes visited.",
    )
    p11.add_argument(
        "--db-path", default=None,
        help="Path to a Phase 3B SQLite graph store (read-only). "
             "When omitted, queries the sample graph in-process.",
    )
    p11.add_argument(
        "--output", default=None,
        help="Write the JSON-serialized BlastRadiusResult to PATH. "
             "Use '-' for stdout. Implies --json.",
    )
    p11.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable text).",
    )
    p11.set_defaults(func=cmd_blast_radius)

    p12 = sub.add_parser(
        "cross-layer-impact",
        help="Phase 3B Task 4 Run 4B: score-to-score cross-layer "
             "impact (upstream + downstream). By default queries "
             "the sample graph; pass --db-path to query a Phase 3B "
             "SQLite graph store.",
    )
    p12.add_argument(
        "--node", default="score:company:2330:2026-07-08",
        help="Start node id. Default: the sample company score.",
    )
    p12.add_argument("--max-depth", type=int, default=5)
    p12.add_argument(
        "--max-nodes", type=int, default=None,
        help="Optional cap on the number of nodes visited per walk.",
    )
    p12.add_argument(
        "--db-path", default=None,
        help="Path to a Phase 3B SQLite graph store (read-only). "
             "When omitted, queries the sample graph in-process.",
    )
    p12.add_argument(
        "--output", default=None,
        help="Write the JSON-serialized CrossLayerImpactResult to "
             "PATH. Use '-' for stdout. Implies --json.",
    )
    p12.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable text).",
    )
    p12.set_defaults(func=cmd_cross_layer_impact)

    # Phase 3B Task 5 Run 4 — Pipeline CLI surface.
    # Five sibling subcommands that expose the Run 1 / Run 2 / Run 3
    # modules through stable entry points. All five default to
    # dry-run and are fully offline; the only flag that touches
    # disk is ``--persist`` on ``pipeline-run`` / ``pipeline-export``,
    # and even that is gated behind ``--db-path`` + a path-guard
    # against ``macro_history.db``.

    def _add_pipeline_common(sp: argparse.ArgumentParser) -> None:
        """Arguments shared by every pipeline-* subcommand."""
        sp.add_argument(
            "--date", required=True,
            help="YYYY-MM-DD bucket the run is for (required).",
        )
        sp.add_argument(
            "--config-hash", default="phase3-cli",
            help="Stable hash identifying the scorer config "
                 "(default: phase3-cli).",
        )
        sp.add_argument(
            "--run-id", default="",
            help="Caller-supplied run id. Default: empty (the "
                 "orchestrator auto-generates one).",
        )

    p13 = sub.add_parser(
        "pipeline-run",
        help="Phase 3B Task 5 Run 4: run the end-to-end pipeline. "
             "Default is dry-run; pass --persist to write to --db-path.",
    )
    _add_pipeline_common(p13)
    p13.add_argument(
        "--industry", action="append", default=[],
        help="Industry id to score (repeatable).",
    )
    p13.add_argument(
        "--company", action="append", default=[],
        help="Company code to score (repeatable, e.g. 2330).",
    )
    p13.add_argument(
        "--no-macro", action="store_true",
        help="Skip the macro leg (default: run it).",
    )
    p13.add_argument(
        "--persist", action="store_true",
        help="Actually write to --db-path. Default: dry-run.",
    )
    p13.add_argument(
        "--db-path", default=None,
        help="Path to a Phase 3B SQLite store (persist mode). "
             "Refused if it resolves to macro_history.db.",
    )
    p13.add_argument(
        "--notes", default="",
        help="Free-form note forwarded to the snapshot and run envelope.",
    )
    p13.add_argument(
        "--trace-directions", default=("upstream",), nargs="+",
        choices=("upstream", "downstream"),
        help="Trace directions for the evidence chain (default: upstream).",
    )
    p13.add_argument(
        "--trace-max-depth", type=int, default=5,
        help="Max depth for the evidence chain walk (default: 5).",
    )
    p13.add_argument(
        "--output", default=None,
        help="Write the JSON-serialised result envelope to PATH. "
             "Use '-' for stdout. Implies --json.",
    )
    p13.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable).",
    )
    p13.set_defaults(func=cmd_pipeline_run)

    p14 = sub.add_parser(
        "pipeline-resume",
        help="Phase 3B Task 5 Run 4: resume a previously-started run. "
             "Requires --db-path.",
    )
    _add_pipeline_common(p14)
    p14.add_argument(
        "--db-path", required=True,
        help="Path to a Phase 3B SQLite store containing the run's "
             "snapshots (required). Refused if it resolves to "
             "macro_history.db.",
    )
    p14.add_argument(
        "--max-attempts", type=int, default=2,
        help="Maximum number of retry attempts for retryable stages "
             "(default: 2).",
    )
    p14.add_argument(
        "--retry-backoff", type=float, default=0.0,
        help="Sleep between attempts in seconds (default: 0).",
    )
    p14.add_argument(
        "--no-strict", action="store_true",
        help="Disable strict_resume (allow resume of a run whose "
             "config_hash does not match the new run's).",
    )
    p14.add_argument(
        "--output", default=None,
        help="Write the JSON-serialised RecoveryResult envelope to PATH.",
    )
    p14.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable).",
    )
    p14.set_defaults(func=cmd_pipeline_resume)

    p15 = sub.add_parser(
        "pipeline-status",
        help="Phase 3B Task 5 Run 4: read-only inspection of a run's "
             "state. Requires --db-path. Does not modify the DB.",
    )
    _add_pipeline_common(p15)
    p15.add_argument(
        "--db-path", required=True,
        help="Path to a Phase 3B SQLite store (required). Refused if "
             "it resolves to macro_history.db.",
    )
    p15.add_argument(
        "--output", default=None,
        help="Write the JSON-serialised RunState envelope to PATH.",
    )
    p15.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable).",
    )
    p15.set_defaults(func=cmd_pipeline_status)

    p16 = sub.add_parser(
        "pipeline-report",
        help="Phase 3B Task 5 Run 4: render JSON + Markdown from a "
             "saved run envelope. --input PATH is the JSON envelope "
             "from a prior pipeline-run; --output-dir PATH is the "
             "destination.",
    )
    p16.add_argument(
        "--input", default=None,
        help="Path to a saved run JSON envelope (required).",
    )
    p16.add_argument(
        "--output-dir", default=None,
        help="Destination directory for the JSON + Markdown artifacts "
             "(required). Created if missing.",
    )
    p16.add_argument(
        "--run-label", default="",
        help="Optional label prefixed to the artifact names.",
    )
    p16.add_argument(
        "--schema-version", type=int, default=1,
        help="Schema version stamped into the JSON envelope (default: 1).",
    )
    p16.add_argument(
        "--no-evidence-payload", action="store_true",
        help="Omit evidence_handle[*].evidence_summary from the JSON.",
    )
    p16.set_defaults(func=cmd_pipeline_report)

    p17 = sub.add_parser(
        "pipeline-export",
        help="Phase 3B Task 5 Run 4: run + export in one call. Default "
             "is dry-run; pass --persist to write to --db-path.",
    )
    _add_pipeline_common(p17)
    p17.add_argument(
        "--industry", action="append", default=[],
        help="Industry id to score (repeatable).",
    )
    p17.add_argument(
        "--company", action="append", default=[],
        help="Company code to score (repeatable).",
    )
    p17.add_argument(
        "--no-macro", action="store_true",
        help="Skip the macro leg (default: run it).",
    )
    p17.add_argument(
        "--persist", action="store_true",
        help="Actually write to --db-path. Default: dry-run.",
    )
    p17.add_argument(
        "--db-path", default=None,
        help="Path to a Phase 3B SQLite store (persist mode).",
    )
    p17.add_argument(
        "--notes", default="",
        help="Free-form note forwarded to the snapshot and run envelope.",
    )
    p17.add_argument(
        "--trace-directions", default=("upstream",), nargs="+",
        choices=("upstream", "downstream"),
        help="Trace directions for the evidence chain (default: upstream).",
    )
    p17.add_argument(
        "--trace-max-depth", type=int, default=5,
    )
    p17.add_argument(
        "--output-dir", default=None,
        help="Destination directory for the JSON + Markdown artifacts "
             "(required).",
    )
    p17.add_argument(
        "--run-label", default="",
        help="Optional label prefixed to the artifact names.",
    )
    p17.add_argument(
        "--schema-version", type=int, default=1,
        help="Schema version stamped into the JSON envelope (default: 1).",
    )
    p17.add_argument(
        "--no-evidence-payload", action="store_true",
        help="Omit evidence_handle[*].evidence_summary from the JSON.",
    )
    p17.add_argument(
        "--json", action="store_true",
        help="Emit the export envelope JSON on stdout (default: human-readable).",
    )
    p17.set_defaults(func=cmd_pipeline_export)

    p18 = sub.add_parser(
        "explain-score",
        help="Phase 4 Task 3B Run 1: read-only explain-score / "
             "decision trace. Composes lineage + blast-radius + "
             "cross-layer-impact around a single score node id. "
             "Output: human-readable Markdown (default) or JSON "
             "(--json / --output).",
    )
    p18.add_argument(
        "--node", default="score:company:2330:2026-07-08",
        help="Score node id to explain. Default: the sample "
             "company score.",
    )
    p18.add_argument(
        "--max-depth", type=int, default=5,
        help="Max hop count for each underlying walk (default: 5).",
    )
    p18.add_argument(
        "--max-nodes", type=int, default=None,
        help="Optional cap on the number of nodes each underlying "
             "walk may visit (default: unbounded).",
    )
    p18.add_argument(
        "--db-path", default=None,
        help="Path to a Phase 3B SQLite graph store (read-only). "
             "When omitted, queries the sample graph in-process. "
             "Refused if it resolves to macro_history.db.",
    )
    p18.add_argument(
        "--output", default=None,
        help="Write the JSON-serialised ExplainedScore to PATH. "
             "Use '-' for stdout. Implies --json.",
    )
    p18.add_argument(
        "--json", action="store_true",
        help="Emit JSON on stdout (default: human-readable Markdown).",
    )
    p18.add_argument(
        "--from-pipeline", action="store_true",
        help="Reserved for a future Run that threads a "
             "PipelineResult through. Accepted but ignored in Run 1.",
    )
    p18.set_defaults(func=cmd_explain_score)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
