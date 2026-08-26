#!/usr/bin/env python3
"""
產業趨勢週報 — 每週一次
從 DIGITIMES RSS + 財訊 RSS 抓取本週新聞
按 8 大產業分類，搭配產業鏈思維分析
"""

import sys
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TZ_TAIPEI = timezone(timedelta(hours=8))
CONFIG_PATH = "/home/ubuntu/macro-report/industry_config.json"
LOG_DIR = "/home/ubuntu/macro-report/logs"

def get_taipei_now():
    return datetime.now(TZ_TAIPEI)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def fetch_rss(url, timeout=15):
    """Fetch and parse an RSS feed, return list of items."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'})
        resp = urllib.request.urlopen(req, timeout=timeout)
        content = resp.read().decode('utf-8', errors='replace')
        root = ET.fromstring(content)
        items = []
        for item in root.findall('.//item'):
            title = item.find('title')
            link = item.find('link')
            pub = item.find('pubDate')
            desc = item.find('description')
            
            title_text = title.text if title is not None and title.text else ""
            link_text = link.text if link is not None and link.text else ""
            pub_text = pub.text if pub is not None and pub.text else ""
            desc_text = ""
            if desc is not None and desc.text:
                # Strip HTML tags
                desc_text = re.sub(r'<[^>]+>', '', desc.text)[:200]
            
            # Parse date
            pub_date = None
            try:
                pub_date = parsedate_to_datetime(pub_text)
            except:
                pass
            
            items.append({
                "title": title_text.strip(),
                "link": link_text.strip(),
                "pub_date": pub_date,
                "pub_text": pub_text,
                "desc": desc_text.strip(),
            })
        return items
    except Exception as e:
        return []

def filter_by_date(items, days=7):
    """Filter items from last N days."""
    now = get_taipei_now()
    cutoff = now - timedelta(days=days)
    result = []
    for item in items:
        if item["pub_date"] is not None:
            # Make naive datetime timezone-aware
            pd = item["pub_date"]
            if pd.tzinfo is None:
                pd = pd.replace(tzinfo=TZ_TAIPEI)
            if pd >= cutoff:
                result.append(item)
        else:
            # No date — include it (might be recent)
            result.append(item)
    return result

def match_industry(title, desc, keywords):
    """Check if a news item matches any keyword."""
    text = (title + " " + desc).lower()
    for kw in keywords:
        if kw.lower() in text:
            return kw
    return None

def collect_news(config, days=7):
    """Collect news from all sources, categorize by industry."""
    all_news = {}  # industry -> list of news items
    industry_stats = {}  # industry -> count
    
    for industry, cfg in config.items():
        keywords = cfg.get("keywords", [])
        feeds = cfg.get("digitimes_feeds", [])
        extra_feeds = cfg.get("extra_feeds", [])
        all_news[industry] = []
        
        # Fetch from DIGITIMES feeds
        seen_titles = set()
        for feed_url in feeds:
            items = fetch_rss(feed_url)
            items = filter_by_date(items, days)
            for item in items:
                if item["title"] in seen_titles:
                    continue
                matched_kw = match_industry(item["title"], item["desc"], keywords)
                if matched_kw:
                    seen_titles.add(item["title"])
                    item["matched_keyword"] = matched_kw
                    item["source"] = "DIGITIMES"
                    all_news[industry].append(item)
        
        # Fetch from extra feeds (e.g. 鉅亨網 for 金融/高股息)
        for feed_url in extra_feeds:
            items = fetch_rss(feed_url)
            items = filter_by_date(items, days)
            for item in items:
                if item["title"] in seen_titles:
                    continue
                matched_kw = match_industry(item["title"], item["desc"], keywords)
                if matched_kw:
                    seen_titles.add(item["title"])
                    item["matched_keyword"] = matched_kw
                    item["source"] = "鉅亨網"
                    all_news[industry].append(item)
        
        industry_stats[industry] = len(all_news[industry])
    
    # Fetch from 財訊 (general feed, categorize by keywords)
    wealth_items = fetch_rss("https://www.wealth.com.tw/rss")
    wealth_items = filter_by_date(wealth_items, days)
    
    for item in wealth_items:
        for industry, cfg in config.items():
            keywords = cfg.get("keywords", [])
            matched_kw = match_industry(item["title"], item["desc"], keywords)
            if matched_kw:
                # Avoid duplicates
                existing_titles = {n["title"] for n in all_news[industry]}
                if item["title"] not in existing_titles:
                    item["matched_keyword"] = matched_kw
                    item["source"] = "財訊"
                    all_news[industry].append(item)
    
    # Update stats
    for industry in all_news:
        industry_stats[industry] = len(all_news[industry])
    
    return all_news, industry_stats

def analyze_supply_chain_impact(news_items, supply_chain):
    """
    Analyze which supply chain stocks are mentioned in news.
    Returns dict of stock_name -> list of related news.
    """
    impacts = {}
    for stock_name, stock_code in supply_chain.items():
        related = []
        for news in news_items:
            text = (news["title"] + " " + news.get("desc", "")).lower()
            # Check if stock name or code is mentioned
            if stock_name.lower() in text or stock_code in text:
                related.append(news["title"])
        if related:
            impacts[stock_name] = {
                "code": stock_code,
                "news_count": len(related),
                "headlines": related[:3],  # Top 3 headlines
            }
    return impacts

def format_weekly_report(all_news, industry_stats, config):
    now = get_taipei_now()
    date_str = now.strftime("%Y/%m/%d")
    week_ago = (now - timedelta(days=7)).strftime("%m/%d")
    
    lines = []
    lines.append(f"🏭 **產業趨勢週報**")
    lines.append(f"📅 {week_ago} ~ {date_str}")
    lines.append(f"🔍 來源：DIGITIMES · 財訊 · 鉅亨網")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    
    # Summary
    total_news = sum(industry_stats.values())
    lines.append(f"📊 **本週總覽**：{total_news} 則產業新聞")
    
    # Sort industries by news count
    sorted_industries = sorted(industry_stats.items(), key=lambda x: x[1], reverse=True)
    for ind, count in sorted_industries:
        if count > 0:
            lines.append(f"  {ind}: {count} 則")
    lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    
    # Per-industry detail
    for industry, news_items in all_news.items():
        if not news_items:
            continue
        
        cfg = config[industry]
        supply_chain = cfg.get("supply_chain", {})
        
        lines.append(f"📌 **{industry}** ({len(news_items)} 則)")
        lines.append("")
        
        # Top 5 news headlines
        sorted_news = sorted(news_items, key=lambda x: x.get("pub_date") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for i, news in enumerate(sorted_news[:5], 1):
            source_tag = f"[{news.get('source','DIGITIMES')}]"
            title = news["title"][:70]
            lines.append(f"  {i}. {title}")
            lines.append(f"     {source_tag} · matched: {news.get('matched_keyword','')}")
        
        # Supply chain impact
        impacts = analyze_supply_chain_impact(news_items, supply_chain)
        if impacts:
            lines.append("")
            lines.append(f"  🔗 **產業鏈受惠股**：「誰會因此賺錢？」")
            sorted_impacts = sorted(impacts.items(), key=lambda x: x[1]["news_count"], reverse=True)
            for stock_name, data in sorted_impacts[:6]:
                lines.append(f"     • {stock_name} ({data['code']}) — {data['news_count']} 則相關")
                if data["headlines"]:
                    lines.append(f"       ↳ {data['headlines'][0][:50]}")
        
        lines.append("")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
    
    # Investment mindset reminder
    lines.append(f"🧠 **本週產業鏈思維提示**")
    lines.append(f"  看到新聞先想：「誰會因此賺錢？」")
    lines.append(f"  不要只看表面受益者，往上游下游推")
    lines.append(f"  例：NVIDIA 新 GPU → 台積電(代工) → 廣達(組裝) → 奇鋐(散熱) → 雙鴻(導熱)")
    lines.append("")
    lines.append(f"⏱ 製作: M2 · {date_str}")
    
    return "\n".join(lines)

def main():
    try:
        config = load_config()
        print(f"📡 抓取本週產業新聞...", file=sys.stderr)
        all_news, industry_stats = collect_news(config, days=7)
        print(f"✅ 抓取完成", file=sys.stderr)
        
        report = format_weekly_report(all_news, industry_stats, config)
        print(report)
        
        # Save log
        os.makedirs(LOG_DIR, exist_ok=True)
        now = get_taipei_now()
        log_file = f"{LOG_DIR}/industry-{now.strftime('%Y-%m-%d')}.json"
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump({
                "date": now.isoformat(),
                "industry_stats": industry_stats,
                "all_news_count": {k: len(v) for k, v in all_news.items()},
            }, f, ensure_ascii=False, indent=2)
        
        return 0
    except Exception as e:
        print(f"❌ 週報生成失敗: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())