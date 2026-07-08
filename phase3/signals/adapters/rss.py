"""RSSAdapter — turn RSS / Atom news items into news/headline/sentiment signals.

Input shape
-----------
``raw`` can be:

  1. A list of items, each item a dict with at least ``title``;
     optional fields: ``link``, ``pubDate`` (or ``pub_date`` /
     ``published``), ``description`` (or ``summary``), ``feed`` /
     ``source``.
  2. A single item dict.
  3. A path to a JSON file containing shape 1 or 2.

This is offline-only. Production callers may add an HTTP fetch
wrapper later, but the adapter itself never opens a socket.

Entity matching
---------------
We use a small built-in keyword → entity map. The Phase 3B scope
document names a few illustrative examples:

  - 公司: 台積電 / 2330 / TSMC / NVDA / NVIDIA / 廣達 / 2382 / ...
  - 產業: 半導體 / 晶片 / AI / GPU / 伺服器 / 金融 / 銀行 / 電子 ...
  - 其它標題 (no keyword match) → ``entity_type="news"``,
    ``entity_id = "news:" + sha1(link|title)[:12]``.

Each input item may produce multiple signals if multiple entity
matches occur (one signal per matched entity). Unmatched items
still emit ONE news signal with entity_type="news".

Sentiment
---------
Deterministic small Chinese + English lexicon. We count the
positive and negative keyword hits in the (title + description)
text, then map to a single sentiment score in ``[-1.0, +1.0]`` via
``(pos - neg) / max(pos + neg, 1)``. If both counts are zero the
score is 0.0 and the signal is "neutral".

Emitted signal types
--------------------
  - ``news_headline``  (per matched entity, value=1.0 if matched,
                        else unit=count for news-level rollup)
  - ``sentiment``      (per item, value in [-1, +1])

Why we emit news_headline even for unmatched items
--------------------------------------------------
The scorer wants at least *some* signal per news event so a sudden
headline spike can be detected. Skipping unmatched items would
silently lose them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from phase3.datamodel.signals import (
    Direction,
    Signal,
    SignalSource,
    make_signal_id,
)
from phase3.signals.adapters.base import SourceAdapter


# ---------------------------------------------------------------------------
# Entity / keyword maps
# ---------------------------------------------------------------------------

# company_name → code (entity_id = code). Codes win over names when both
# match the same item.
_COMPANY_MAP: dict[str, str] = {
    "台積電": "2330",
    "tsmc": "2330",
    "2330": "2330",
    "nvda": "NVDA",
    "nvidia": "NVDA",
    "輝達": "NVDA",
    "廣達": "2382",
    "2382": "2382",
    "聯發科": "2454",
    "2454": "2454",
    "鴻海": "2317",
    "2317": "2317",
    "台達電": "2308",
    "2308": "2308",
    "富邦金": "2881",
    "2881": "2881",
    "國泰金": "2882",
    "2882": "2882",
    "兆豐金": "2886",
    "2886": "2886",
}

# industry keywords → entity_id (just the keyword as both type and id,
# matching how the rest of Phase 3 already names industry entities).
_INDUSTRY_KEYWORDS: dict[str, str] = {
    "半導體": "半導體",
    "晶片": "半導體",
    "chip": "半導體",
    "semiconductor": "半導體",
    "ai": "AI",
    "人工智慧": "AI",
    "gpu": "AI",
    "伺服器": "伺服器",
    "server": "伺服器",
    "pcb": "PCB",
    "網通": "網通",
    "重電": "重電",
    "金融": "金融",
    "銀行": "金融",
    "bank": "金融",
    "高股息": "高股息",
    "etf": "高股息",
    "電子": "電子",
}

# Lexicon: simple zh + en keyword lists.
_POSITIVE_TERMS: tuple[str, ...] = (
    "上漲", "漲", "突破", "新高", "成長", "強勁", "看好", "利多", "優於預期",
    "創新高", "突破", "訂單", "需求強勁", "outperform", "beat", "surge", "rally",
    "gain", "strong", "growth", "expand", "boost", "record", "high",
)
_NEGATIVE_TERMS: tuple[str, ...] = (
    "下跌", "跌", "新低", "衰退", "虧損", "看淡", "利空", "不如預期",
    "下滑", "賣超", "砍單", "downgrade", "miss", "fall", "drop", "decline",
    "weak", "loss", "cut", "lower", "shrink", "concern", "risk",
)


# Match a keyword as a whole word/segment. We don't want a substring
# match for English ("AI" should not match "FAIL"); for Chinese we
# match exact substring (Chinese has no word boundaries).
def _match_zh(keyword: str, text: str) -> bool:
    return keyword in text


def _match_en_word(keyword: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def _match_company(keyword: str, text: str) -> bool:
    # 4-digit codes: match as substring (no false-positive risk for
    # ticker codes; they're all 4 digits).
    if re.fullmatch(r"\d{4}", keyword):
        return keyword in text
    # Otherwise match as word/segment.
    if any("\u4e00" <= c <= "\u9fff" for c in keyword):
        return keyword in text
    return _match_en_word(keyword, text)


def _match_industry(keyword: str, text: str) -> bool:
    return _match_company(keyword, text)


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------


@dataclass
class RSSAdaptResult:
    """Return value from :meth:`RSSAdapter.adapt_with_stats`."""

    signals: list[Signal] = field(default_factory=list)
    items_processed: int = 0
    items_skipped: int = 0
    entity_matched: int = 0
    entity_unmatched: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RSSAdapter(SourceAdapter):
    """Offline normalizer for RSS / Atom news items."""

    source_type = "rss"

    def adapt(self, raw: Any) -> list[Signal]:  # type: ignore[override]
        return self.adapt_with_stats(raw).signals

    def adapt_with_stats(self, raw: Any) -> RSSAdaptResult:
        result = RSSAdaptResult()
        items = self._load_items(raw, result)
        for item in items:
            self._emit_for_item(item, result)
        return result

    # ----- emission --------------------------------------------------------

    def _emit_for_item(self, item: Mapping[str, Any], result: RSSAdaptResult) -> None:
        title = str(item.get("title", "")).strip()
        if not title:
            result.warnings.append("item missing 'title'; skipping")
            result.items_skipped += 1
            return
        description = str(item.get("description", item.get("summary", "")) or "")
        link = str(item.get("link", item.get("url", "")) or "")
        pub_raw = item.get("pubDate", item.get("pub_date", item.get("published")))
        timestamp = self._parse_timestamp(pub_raw) or _utcnow()
        date_bucket = timestamp.date().isoformat()
        feed_name = str(item.get("feed", item.get("source", "rss")) or "rss")
        source_id = f"rss.{feed_name}.{self._item_hash(link, title)}"
        text_for_match = f"{title}\n{description}"
        # Sentiment computed once per item.
        score = self._score_sentiment(text_for_match)
        direction: Direction = "neutral"
        if score > 0.05:
            direction = "bullish"
        elif score < -0.05:
            direction = "bearish"
        # Entity match: collect (entity_type, entity_id) tuples, deduped.
        entities: list[tuple[str, str]] = []
        seen_ids: set[tuple[str, str]] = set()
        for kw, code in _COMPANY_MAP.items():
            if _match_company(kw, text_for_match):
                key = ("company", code)
                if key not in seen_ids:
                    entities.append(key)
                    seen_ids.add(key)
        for kw, ind_id in _INDUSTRY_KEYWORDS.items():
            if _match_industry(kw, text_for_match):
                key = ("industry", ind_id)
                if key not in seen_ids:
                    entities.append(key)
                    seen_ids.add(key)
        # If no entity match, emit a single "news" signal.
        if not entities:
            entities = [("news", f"news:{self._item_hash(link, title)}")]
            result.entity_unmatched += 1
        else:
            result.entity_matched += 1
        src_meta: dict[str, Any] = {
            "feed": feed_name,
            "link": link,
            "title": title,
        }
        # Emit one news_headline + one sentiment signal per matched entity.
        for entity_type, entity_id in entities:
            src = SignalSource(
                source_id=source_id,
                source_type="rss",
                ref=link or feed_name,
                fetched_at=_utcnow(),
                metadata=dict(src_meta),
            )
            # news_headline signal
            sid_h = make_signal_id(source_id, entity_id, "news_headline", date_bucket)
            headline_sig = Signal(
                signal_id=sid_h,
                entity_type=entity_type,
                entity_id=entity_id,
                signal_type="news_headline",
                value=1.0,
                unit="count",
                direction=direction,
                timestamp=timestamp,
                source=src,
                date_bucket=date_bucket,
                metadata={**src_meta, "entity_match": entity_id},
            )
            result.signals.append(headline_sig)
            # sentiment signal (only for company/industry matches to avoid
            # unbounded "news" entity inflation)
            if entity_type in ("company", "industry"):
                sid_s = make_signal_id(source_id, entity_id, "sentiment", date_bucket)
                sentiment_sig = Signal(
                    signal_id=sid_s,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    signal_type="sentiment",
                    value=score,
                    unit="score",
                    direction=direction,
                    timestamp=timestamp,
                    source=src,
                    date_bucket=date_bucket,
                    metadata={**src_meta, "sentiment_raw": score},
                )
                result.signals.append(sentiment_sig)
        result.items_processed += 1

    # ----- sentiment -------------------------------------------------------

    def _score_sentiment(self, text: str) -> float:
        if not text:
            return 0.0
        text_lc = text.lower()
        pos = 0
        neg = 0
        for term in _POSITIVE_TERMS:
            if any("\u4e00" <= c <= "\u9fff" for c in term):
                if term in text:
                    pos += 1
            else:
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text_lc):
                    pos += 1
        for term in _NEGATIVE_TERMS:
            if any("\u4e00" <= c <= "\u9fff" for c in term):
                if term in text:
                    neg += 1
            else:
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text_lc):
                    neg += 1
        total = pos + neg
        if total == 0:
            return 0.0
        return round((pos - neg) / total, 4)

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _item_hash(link: str, title: str) -> str:
        h = hashlib.sha1()
        h.update(link.encode("utf-8") if link else b"")
        h.update(b"|")
        h.update(title.encode("utf-8") if title else b"")
        return h.hexdigest()[:12]

    @staticmethod
    def _load_items(raw: Any, result: RSSAdaptResult) -> list[Mapping[str, Any]]:
        if isinstance(raw, Mapping):
            if "items" in raw and isinstance(raw["items"], list):
                return [it for it in raw["items"] if isinstance(it, Mapping)]
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [it for it in raw if isinstance(it, Mapping)]
        try:
            path = Path(os.fspath(raw))  # type: ignore[arg-type]
        except TypeError:
            result.warnings.append(
                f"unsupported raw type: {type(raw).__name__}"
            )
            return []
        if not path.exists():
            result.warnings.append(f"rss fixture path not found: {path}")
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            result.warnings.append(f"failed to load {path}: {exc}")
            return []
        if isinstance(data, list):
            return [it for it in data if isinstance(it, Mapping)]
        if isinstance(data, Mapping):
            if "items" in data and isinstance(data["items"], list):
                return [it for it in data["items"] if isinstance(it, Mapping)]
            return [data]
        return []

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["RSSAdapter", "RSSAdaptResult"]
