"""InputBuilder  map Signals to scorer input bundles.

The builder is responsible for turning normalized Signals (emitted by adapters
and weighted by the SignalEngine) into the per-dimension dictionaries used by
Macro/Industry/Company scorers. Run 1 (Task 3) only shapes inputs and collects
provenance; the scoring orchestration and persistence layers will arrive in
later runs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from phase3.datamodel import COMPANY_DIMENSIONS, INDUSTRY_DIMENSIONS, MACRO_DIMENSIONS
from phase3.datamodel.signals import Signal
from phase3.pipeline import InputBundle, InputDimension


@dataclass
class InputBuilderConfig:
    """Optional configuration for InputBuilder."""

    macro_dimensions: tuple[str, ...] = MACRO_DIMENSIONS
    industry_dimensions: tuple[str, ...] = INDUSTRY_DIMENSIONS
    company_dimensions: tuple[str, ...] = COMPANY_DIMENSIONS


class InputBuilder:
    """Builds scorer input bundles from raw signals."""

    def __init__(self, config: InputBuilderConfig | None = None) -> None:
        self._cfg = config or InputBuilderConfig()

    def build_macro(self, signals: Iterable[Signal], entity_id: str, date_bucket: str) -> InputBundle:
        dims = self._init_dimensions(self._cfg.macro_dimensions)
        for sig in signals:
            self._assign_macro_signal(sig, dims)
        warnings = self._finalize_dimensions(dims)
        return InputBundle(
            scorer_type="macro",
            entity_type="macro",
            entity_id=entity_id,
            date_bucket=date_bucket,
            dimensions=dims,
            warnings=warnings,
        )

    def build_industry(
        self,
        signals: Iterable[Signal],
        industry_id: str,
        date_bucket: str,
    ) -> InputBundle:
        dims = self._init_dimensions(self._cfg.industry_dimensions)
        for sig in signals:
            self._assign_industry_signal(sig, dims)
        warnings = self._finalize_dimensions(dims)
        return InputBundle(
            scorer_type="industry",
            entity_type="industry",
            entity_id=industry_id,
            date_bucket=date_bucket,
            dimensions=dims,
            warnings=warnings,
        )

    def build_company(
        self,
        signals: Iterable[Signal],
        company_id: str,
        date_bucket: str,
    ) -> InputBundle:
        dims = self._init_dimensions(self._cfg.company_dimensions)
        for sig in signals:
            self._assign_company_signal(sig, dims)
        warnings = self._finalize_dimensions(dims)
        return InputBundle(
            scorer_type="company",
            entity_type="company",
            entity_id=company_id,
            date_bucket=date_bucket,
            dimensions=dims,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    def _init_dimensions(self, names: Iterable[str]) -> dict[str, InputDimension]:
        return {name: InputDimension() for name in names}

    def _assign_macro_signal(self, signal: Signal, dims: dict[str, InputDimension]) -> None:
        dim_map = {
            "gdp_yoy": "economic",
            "pmi": "economic",
            "unemployment_delta_pp": "economic",
            "retail_yoy": "economic",
            "fed_rate": "monetary",
            "dot_plot": "monetary",
            "cpi_yoy": "inflation",
            "core_cpi_yoy": "inflation",
            "yield_spread": "rates",
            "real_rate": "rates",
            "rate_vol": "rates",
            "vix": "liquidity",
            "dxy": "liquidity",
            "m2_yoy": "liquidity",
            "credit_spread": "liquidity",
            "geopolitical_event": "geopolitics",
        }
        dim = dim_map.get(signal.signal_type)
        if dim and dim in dims:
            self._append_value(dims[dim], signal.signal_type, signal.value, signal.signal_id)

    def _assign_industry_signal(self, signal: Signal, dims: dict[str, InputDimension]) -> None:
        dim_map = {
            "sector_relative_perf_5d": "rotation",
            "sector_relative_perf_20d": "rotation",
            "money_flow_proxy": "rotation",
            "rs_rating": "relative_strength",
            "industry_etf_momentum_1m": "relative_strength",
            "stock_breadth_pct": "relative_strength",
            "pmi_direction": "cyclicality",
            "book_to_bill": "cyclicality",
            "export_yoy": "cyclicality",
            "news_headline": "industry_news",
            "sentiment": "industry_news",
            "event_severity": "industry_news",
            "foreign_net": "capital_flow",
            "prop_net": "capital_flow",
        }
        dim = dim_map.get(signal.signal_type)
        if dim and dim in dims:
            self._append_value(dims[dim], signal.signal_type, signal.value, signal.signal_id)

    def _assign_company_signal(self, signal: Signal, dims: dict[str, InputDimension]) -> None:
        dim_map = {
            "roe": "financial_quality",
            "roa": "financial_quality",
            "debt_equity": "financial_quality",
            "current_ratio": "financial_quality",
            "fcf": "financial_quality",
            "revenue_growth": "growth",
            "earnings_growth": "growth",
            "fcf_growth": "growth",
            "guidance": "growth",
            "gross_margin": "profitability",
            "operating_margin": "profitability",
            "net_margin": "profitability",
            "nim_growth": "profitability",
            "interest_spread": "profitability",
            "pe_ratio": "valuation",
            "pb_ratio": "valuation",
            "peg_ratio": "valuation",
            "dividend_yield": "valuation",
            "price_momentum_1m": "momentum",
            "price_momentum_3m": "momentum",
            "dist_from_52w_high": "momentum",
            "relative_to_market": "momentum",
            "beta": "risk",
            "debt_ratio": "risk",
            "earnings_volatility": "risk",
            "risk_event_count": "risk",
            "news_headline": "news_sentiment",
            "sentiment": "news_sentiment",
            "event_severity": "news_sentiment",
        }
        dim = dim_map.get(signal.signal_type)
        if dim and dim in dims:
            self._append_value(dims[dim], signal.signal_type, signal.value, signal.signal_id)

    def _append_value(
        self,
        dimension: InputDimension,
        key: str,
        value: float,
        signal_id: str,
    ) -> None:
        # Keep the most recent value when multiple signals hit the same indicator.
        dimension.values[key] = value
        dimension.signal_ids.append(signal_id)

    def _finalize_dimensions(self, dims: dict[str, InputDimension]) -> list[str]:
        warnings: list[str] = []
        for name, dim in dims.items():
            if not dim.values:
                warnings.append(f"dimension {name} has no signals; defaulting to neutral inputs")
        return warnings


__all__ = ["InputBuilder", "InputBuilderConfig"]
