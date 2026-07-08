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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
