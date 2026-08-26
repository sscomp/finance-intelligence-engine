#!/usr/bin/env python3
"""
法人動向抓取模組
從 TWSE T86 API 累積近一個月（約 20 個交易日）的外資/投信買賣超
"""

import urllib.request
import json
import time
from datetime import datetime, timedelta

# Phase 2B Step 2 — format_shares / format_net moved to shared module.
# Local definitions below (the originals) have been removed; names are
# preserved by importing the shared implementations. Behavior is identical
# (verified by tests/test_format_helpers.py — 31/46 tests pin these two
# helpers, all 46/46 PASS post-extraction).
from reports.common.format import format_shares, format_net  # noqa: E402,F401

T86_URL = "https://www.twse.com.tw/fund/T86?response=json&date={date}&selectType=ALL"

def get_weekday_dates(days_back=35):
    """Generate list of YYYYMMDD strings for weekdays in the past N calendar days."""
    end = datetime.now()
    start = end - timedelta(days=days_back)
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return dates

def fetch_t86_daily(date_str, timeout=15):
    """Fetch T86 data for a single date. Returns dict of stock_code -> {foreign, prop, total}."""
    url = T86_URL.format(date=date_str)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode('utf-8'))
        
        if data.get('stat') != 'OK' or 'data' not in data:
            return None
        
        result = {}
        for row in data['data']:
            if len(row) < 19:
                continue
            code = row[0].strip()
            # row[4] = 外陸資買賣超(不含外資自營商)
            # row[7] = 外資自營商買賣超
            # row[10] = 投信買賣超
            # row[18] = 三大法人買賣超合計
            def parse_num(s):
                if not s:
                    return 0
                try:
                    return int(s.replace(',', '').replace(' ', ''))
                except:
                    return 0
            
            foreign = parse_num(row[4]) + parse_num(row[7])  # 外資(含自營商)
            prop = parse_num(row[10])  # 投信
            total = parse_num(row[18])  # 三大法人合計
            
            result[code] = {
                'foreign': foreign,
                'prop': prop,
                'total': total,
            }
        return result
    except Exception:
        return None

def accumulate_institutional_data(target_codes, days_back=35):
    """
    Accumulate institutional buy/sell data for target stocks over ~20 trading days.
    Returns dict of stock_code -> {foreign_net: int, prop_net: int, total_net: int, days_fetched: int}
    """
    dates = get_weekday_dates(days_back)
    
    # Initialize accumulators
    acc = {code: {'foreign': 0, 'prop': 0, 'total': 0, 'days': 0} for code in target_codes}
    
    successful_dates = 0
    
    for date_str in dates:
        daily_data = fetch_t86_daily(date_str)
        if daily_data is None:
            continue
        
        successful_dates += 1
        
        for code in target_codes:
            if code in daily_data:
                acc[code]['foreign'] += daily_data[code]['foreign']
                acc[code]['prop'] += daily_data[code]['prop']
                acc[code]['total'] += daily_data[code]['total']
                acc[code]['days'] += 1
        
        # Rate limit: be nice to TWSE
        time.sleep(0.3)
    
    # Add metadata
    for code in target_codes:
        acc[code]['trading_days_fetched'] = successful_dates
    
    return acc, successful_dates

# Test
if __name__ == "__main__":
    test_codes = ["2330", "2454", "2881", "2882", "2308"]
    print(f"Fetching institutional data for {len(test_codes)} stocks...")
    
    result, days = accumulate_institutional_data(test_codes, days_back=35)
    print(f"Successfully fetched {days} trading days\n")
    
    for code in test_codes:
        r = result[code]
        print(f"{code}:")
        print(f"  外資(近月): {format_net(r['foreign'])} ({r['foreign']:,d}股)")
        print(f"  投信(近月): {format_net(r['prop'])} ({r['prop']:,d}股)")
        print(f"  三大法人合計: {format_net(r['total'])}")
        print()