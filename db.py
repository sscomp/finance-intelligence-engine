#!/usr/bin/env python3
"""
歷史資料庫模組 — SQLite
儲存每日總體指標、每月個股基本面、法人動向
讓未來可以算「外資連續買超天數」「NIM 季度趨勢圖」「本益比歷史百分位」

使用方式：
  from db import save_macro_daily, save_stock_monthly, save_institutional_daily

  save_macro_daily(data, verdict, score)           # macro_daily.py 呼叫
  save_stock_monthly(code, name, sector, data)      # company_monthly.py 呼叫
  save_institutional_daily(date_str, daily_data)   # institutional.py 呼叫
  get_foreign_streak(code, days=20)                # 查連續買超天數
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from phase3.paths import macro_history_db_path, data_dir
except ImportError:  # db.py 可獨立於 phase3 使用（無 phase3 於 sys.path 時）
    macro_history_db_path = None
    data_dir = None

TZ_TAIPEI = timezone(timedelta(hours=8))

# Phase 6.1 portability: production DB path is now derived from the central
# configuration boundary (FIE_DB_PATH > FIE_DATA_DIR > project root) instead
# of the historical hard-coded /home/ubuntu/macro-report absolute path.
# The DB_PATH module attribute is kept (callers/tests patch it) — the default
# is computed by the same rule for every importer.
if macro_history_db_path is not None:
    _DEFAULT_DB_PATH = str(macro_history_db_path())
else:
    _data_base = os.environ.get("FIE_DATA_DIR") or os.getcwd()
    _DEFAULT_DB_PATH = os.path.join(_data_base, "macro_history.db")

DB_PATH = _DEFAULT_DB_PATH


def get_log_dir():
    """Runtime log directory (FIE_DATA_DIR/logs); falls back to CWD."""
    if data_dir is not None:
        return str(data_dir() / "logs")
    _data_base = os.environ.get("FIE_DATA_DIR") or os.getcwd()
    return os.path.join(_data_base, "logs")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create tables if not exist."""
    conn = get_db()
    c = conn.cursor()
    
    # 每日總體指標
    c.execute("""
        CREATE TABLE IF NOT EXISTS macro_daily (
            date TEXT PRIMARY KEY,
            us10y REAL, us2y REAL, us13w REAL,
            dxy REAL, vix REAL, usdtwd REAL,
            yield_spread REAL,
            score INTEGER,
            verdict TEXT,
            signals_json TEXT,
            created_at TEXT
        )
    """)
    
    # 每月個股基本面
    c.execute("""
        CREATE TABLE IF NOT EXISTS stock_monthly (
            date TEXT,
            code TEXT,
            name TEXT,
            sector TEXT,
            price REAL,
            eps_ttm REAL, pe_trailing REAL, pe_forward REAL,
            roe REAL, roa REAL,
            gross_margin REAL, operating_margin REAL, profit_margin REAL,
            dividend_rate REAL, dividend_yield REAL, payout_ratio REAL,
            pb_ratio REAL, revenue_growth REAL, earnings_growth REAL,
            nim_growth REAL, interest_spread REAL,
            high_52 REAL, low_52 REAL, dist_from_high REAL,
            target_mean REAL, peg_ratio REAL, market_cap REAL,
            PRIMARY KEY (date, code)
        )
    """)
    
    # 法人動向（每日累積值 snapshot）
    c.execute("""
        CREATE TABLE IF NOT EXISTS institutional_daily (
            date TEXT,
            code TEXT,
            foreign_net INTEGER,
            prop_net INTEGER,
            total_net INTEGER,
            trading_days INTEGER,
            PRIMARY KEY (date, code)
        )
    """)
    
    conn.commit()
    conn.close()

def save_macro_daily(data, verdict, score, signals):
    """Save daily macro indicators to DB."""
    init_db()
    conn = get_db()
    c = conn.cursor()
    
    import json
    now = datetime.now(TZ_TAIPEI).isoformat()
    
    # Use the date from US10Y (market date), fallback to today
    date_str = data.get("US10Y", {}).get("date")
    if not date_str:
        date_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    
    c.execute("""
        INSERT OR REPLACE INTO macro_daily 
        (date, us10y, us2y, us13w, dxy, vix, usdtwd, yield_spread, score, verdict, signals_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date_str,
        data.get("US10Y", {}).get("value"),
        data.get("US2Y", {}).get("value"),
        data.get("US13W", {}).get("value"),
        data.get("DXY", {}).get("value"),
        data.get("VIX", {}).get("value"),
        data.get("USDTWD", {}).get("value"),
        data.get("YIELD_SPREAD", {}).get("value"),
        score,
        verdict,
        json.dumps(signals, ensure_ascii=False),
        now,
    ))
    conn.commit()
    conn.close()

def save_stock_monthly(code, name, sector, data):
    """Save monthly stock fundamentals to DB."""
    init_db()
    conn = get_db()
    c = conn.cursor()
    
    date_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    
    c.execute("""
        INSERT OR REPLACE INTO stock_monthly
        (date, code, name, sector, price, eps_ttm, pe_trailing, pe_forward,
         roe, roa, gross_margin, operating_margin, profit_margin,
         dividend_rate, dividend_yield, payout_ratio, pb_ratio,
         revenue_growth, earnings_growth, nim_growth, interest_spread,
         high_52, low_52, dist_from_high, target_mean, peg_ratio, market_cap)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        date_str, code, name, sector,
        data.get("price"), data.get("eps_ttm"), data.get("pe_trailing"), data.get("pe_forward"),
        data.get("roe"), data.get("roa"), data.get("gross_margin"), data.get("operating_margin"), data.get("profit_margin"),
        data.get("dividend_rate"), data.get("dividend_yield"), data.get("payout_ratio"), data.get("pb_ratio"),
        data.get("revenue_growth"), data.get("earnings_growth"),
        data.get("nim_growth"), data.get("interest_spread"),
        data.get("52w_high"), data.get("52w_low"), data.get("dist_from_high"),
        data.get("target_mean"), data.get("peg_ratio"), data.get("market_cap"),
    ))
    conn.commit()
    conn.close()

def save_institutional_snapshot(code, foreign_net, prop_net, total_net, trading_days):
    """Save institutional data snapshot (cumulative for the period)."""
    init_db()
    conn = get_db()
    c = conn.cursor()
    
    date_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    
    c.execute("""
        INSERT OR REPLACE INTO institutional_daily
        (date, code, foreign_net, prop_net, total_net, trading_days)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (date_str, code, foreign_net, prop_net, total_net, trading_days))
    conn.commit()
    conn.close()

# === 查詢函數 ===

def get_foreign_streak(code, days=60):
    """查詢某檔股票外資連續買超/賣超天數（基於每日 snapshot 差值）。
    
    注意：institutional_daily 存的是「累積近月」值，
    所以連續天數 = snapshot 遞增的天數（每天新增買超 = 累積值增加）。
    
    Returns: (streak_days, direction) — 正=連續買超, 負=連續賣超
    """
    init_db()
    conn = get_db()
    c = conn.cursor()
    
    rows = c.execute("""
        SELECT date, foreign_net FROM institutional_daily
        WHERE code = ? AND date >= date('now', ?)
        ORDER BY date DESC
    """, (code, f"-{days} days")).fetchall()
    
    if len(rows) < 2:
        return 0, "N/A"
    
    # 計算每日 delta（最新 - 前一天）
    streak = 0
    direction = "N/A"
    for i in range(len(rows) - 1):
        delta = rows[i]["foreign_net"] - rows[i + 1]["foreign_net"]
        if i == 0:
            direction = "買超" if delta > 0 else ("賣超" if delta < 0 else "持平")
        if direction == "買超" and delta > 0:
            streak += 1
        elif direction == "賣超" and delta < 0:
            streak += 1
        else:
            break
    
    return streak, direction

def get_pe_percentile(code, months=12):
    """查詢某檔股票本益比歷史百分位。"""
    init_db()
    conn = get_db()
    c = conn.cursor()
    
    rows = c.execute("""
        SELECT pe_trailing FROM stock_monthly
        WHERE code = ? AND pe_trailing IS NOT NULL AND pe_trailing > 0
        AND date >= date('now', ?)
        ORDER BY date DESC
    """, (code, f"-{months * 30} days")).fetchall()
    
    if len(rows) < 3:
        return None
    
    pes = [r["pe_trailing"] for r in rows]
    current = pes[0]
    sorted_pes = sorted(pes)
    rank = sum(1 for p in sorted_pes if p <= current)
    percentile = rank / len(sorted_pes) * 100
    
    return {
        "current_pe": current,
        "percentile": percentile,
        "min_pe": min(pes),
        "max_pe": max(pes),
        "data_points": len(pes),
    }

def get_macro_history(days=30):
    """取得近 N 天總體指標歷史。"""
    init_db()
    conn = get_db()
    c = conn.cursor()
    
    rows = c.execute("""
        SELECT * FROM macro_daily
        WHERE date >= date('now', ?)
        ORDER BY date DESC
    """, (f"-{days} days",)).fetchall()
    
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print("✅ DB initialized at", DB_PATH)
    
    # Show table stats
    conn = get_db()
    c = conn.cursor()
    for table in ["macro_daily", "stock_monthly", "institutional_daily"]:
        count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    conn.close()