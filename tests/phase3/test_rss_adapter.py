"""Tests for the RSSAdapter.

Contract:
* Each item must have a non-empty ``title``; missing → skipped.
* Each item emits one ``news_headline`` Signal.
* Company/industry-matched items ALSO emit one ``sentiment`` Signal.
* Unmatched items emit ONE ``news_headline`` signal with
  ``entity_type="news"`` and ``entity_id="news:<sha1[:12]>"``.
* Multiple entity matches per item → multiple signals (one per entity).
* Sentiment score is in [-1, +1], determined by a small zh+en lexicon.
* Direction: score > 0.05 → bullish, < -0.05 → bearish, else neutral.
* signal_id is deterministic via make_signal_id.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from phase3.datamodel.signals import Signal, make_signal_id
from phase3.signals.adapters.rss import RSSAdapter


def _item(title: str = "headline", description: str = "",
          link: str = "https://x/y", pub: str = "2026-07-08T10:00:00+00:00",
          feed: str = "cnyes") -> dict:
    return {
        "title": title,
        "description": description,
        "link": link,
        "pubDate": pub,
        "feed": feed,
    }


class RSSAdapterBasicTests(unittest.TestCase):
    def test_company_match_emits_two_signals(self) -> None:
        a = RSSAdapter()
        item = _item(title="台積電 2330 法說會", description="outperform")
        sigs = a.adapt([item])
        # 1 news_headline + 1 sentiment = 2 signals
        self.assertEqual(len(sigs), 2)
        types = sorted(s.signal_type for s in sigs)
        self.assertEqual(types, ["news_headline", "sentiment"])

    def test_industry_match_emits_two_signals(self) -> None:
        a = RSSAdapter()
        item = _item(title="半導體產業展望樂觀", description="")
        sigs = a.adapt([item])
        self.assertEqual(len(sigs), 2)
        headline = next(s for s in sigs if s.signal_type == "news_headline")
        self.assertEqual(headline.entity_type, "industry")
        self.assertEqual(headline.entity_id, "半導體")

    def test_unmatched_item_emits_only_one_news_signal(self) -> None:
        a = RSSAdapter()
        item = _item(title="天氣預報今日多雲", description="")
        sigs = a.adapt([item])
        # No sentiment for unmatched "news" entity to avoid unbounded growth
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s.entity_type, "news")
        self.assertEqual(s.entity_id, s.entity_id)  # hash
        self.assertTrue(s.entity_id.startswith("news:"))
        self.assertEqual(s.signal_type, "news_headline")

    def test_multiple_entity_matches_each_emit_signals(self) -> None:
        a = RSSAdapter()
        # matches both 台積電 (company) and 半導體 (industry)
        item = _item(title="台積電在半導體領先地位", description="")
        sigs = a.adapt([item])
        # 2 entities × 2 signal types (headline + sentiment) = 4
        self.assertEqual(len(sigs), 4)
        entities = {(s.entity_type, s.entity_id) for s in sigs}
        self.assertIn(("company", "2330"), entities)
        self.assertIn(("industry", "半導體"), entities)

    def test_missing_title_skipped(self) -> None:
        a = RSSAdapter()
        result = a.adapt_with_stats([{"title": ""}])
        self.assertEqual(len(result.signals), 0)
        self.assertEqual(result.items_skipped, 1)

    def test_bullish_sentiment_for_positive_words(self) -> None:
        a = RSSAdapter()
        item = _item(title="台積電 2330 創新高", description="強勁成長 out perform")
        sigs = a.adapt([item])
        sentiment = next(s for s in sigs if s.signal_type == "sentiment")
        self.assertGreater(sentiment.value, 0.0)
        self.assertEqual(sentiment.direction, "bullish")

    def test_bearish_sentiment_for_negative_words(self) -> None:
        a = RSSAdapter()
        item = _item(title="台積電 2330 下跌", description="虧損衰退 weak decline")
        sigs = a.adapt([item])
        sentiment = next(s for s in sigs if s.signal_type == "sentiment")
        self.assertLess(sentiment.value, 0.0)
        self.assertEqual(sentiment.direction, "bearish")

    def test_neutral_when_no_sentiment_words(self) -> None:
        a = RSSAdapter()
        item = _item(title="台積電 2330 公告", description="")
        sigs = a.adapt([item])
        sentiment = next(s for s in sigs if s.signal_type == "sentiment")
        self.assertEqual(sentiment.value, 0.0)
        self.assertEqual(sentiment.direction, "neutral")

    def test_signal_id_deterministic(self) -> None:
        a = RSSAdapter()
        item = _item(title="台積電 2330 公告", link="https://x/y1")
        sigs = a.adapt([item])
        for s in sigs:
            expected = make_signal_id(
                s.source.source_id, s.entity_id, s.signal_type, s.date_bucket
            )
            self.assertEqual(s.signal_id, expected)

    def test_unmatched_count_increments(self) -> None:
        a = RSSAdapter()
        result = a.adapt_with_stats([
            _item(title="天氣", description=""),
            _item(title="台積電 2330 法說", description=""),
        ])
        self.assertEqual(result.entity_matched, 1)
        self.assertEqual(result.entity_unmatched, 1)


class RSSAdapterInputShapeTests(unittest.TestCase):
    def test_accepts_list_of_items(self) -> None:
        a = RSSAdapter()
        result = a.adapt_with_stats([
            _item(title="台積電 2330 公告"),
            _item(title="天氣預報"),
        ])
        self.assertEqual(result.items_processed, 2)

    def test_accepts_dict_with_items_key(self) -> None:
        a = RSSAdapter()
        result = a.adapt_with_stats({
            "items": [_item(title="台積電 2330 公告")],
        })
        self.assertEqual(result.items_processed, 1)

    def test_loads_from_json_file(self) -> None:
        fd, p = tempfile.mkstemp(suffix=".json")
        import os
        os.close(fd)
        try:
            Path(p).write_text(json.dumps([
                _item(title="台積電 2330 公告"),
                _item(title="天氣預報"),
            ]))
            a = RSSAdapter()
            result = a.adapt_with_stats(Path(p))
            self.assertEqual(result.items_processed, 2)
        finally:
            Path(p).unlink()

    def test_missing_path_warns_no_raise(self) -> None:
        a = RSSAdapter()
        result = a.adapt_with_stats(Path("/no/such/rss.json"))
        self.assertEqual(len(result.signals), 0)
        self.assertTrue(any("not found" in w for w in result.warnings))

    def test_tickers_match_as_substrings(self) -> None:
        """4-digit codes match as substrings, English words as whole words."""
        a = RSSAdapter()
        # The 4-digit code '2330' inside a longer string should still match
        item = _item(title="新聞 12330 相關")  # contains 2330 as substring
        result = a.adapt_with_stats([item])
        # 2330 is a substring of 12330 → should match
        self.assertEqual(result.entity_matched, 1)

    def test_ai_english_whole_word_match(self) -> None:
        """The English keyword 'AI' should match as a whole word, not in 'FAIL'."""
        a = RSSAdapter()
        # 'AI' alone should match (link=pos)
        positive = _item(title="AI chip demand strong", link="https://x/pos")
        # 'FAIL' contains 'AI' as a substring but not as a whole word
        negative = _item(title="chip demand will FAIL", link="https://x/neg")
        result = a.adapt_with_stats([positive, negative])
        # Count unique items that produced an AI headline.
        # Each matched item emits 1 news_headline + 1 sentiment signal,
        # so we look at the news_headline signals only.
        ai_headline_links = {
            s.metadata.get("link")
            for s in result.signals
            if s.entity_id == "AI" and s.entity_type == "industry"
            and s.signal_type == "news_headline"
        }
        # Only the positive item (link=pos) should match.
        self.assertEqual(ai_headline_links, {"https://x/pos"})

    def test_determinism(self) -> None:
        a = RSSAdapter()
        item = _item(title="台積電 2330 公告", link="https://x/y1")
        s1 = a.adapt([item])
        s2 = a.adapt([item])
        self.assertEqual(
            [s.signal_id for s in s1],
            [s.signal_id for s in s2],
        )


if __name__ == "__main__":
    unittest.main()
