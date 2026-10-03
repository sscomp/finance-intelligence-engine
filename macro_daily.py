#!/usr/bin/env python3
"""
總體經濟晨報 — 每日開盤前派送
指標：Fed利率方向 / 美10Y公債殖利率 / 美元指數 / 台幣匯率 / VIX
資料來源：Yahoo Finance (yfinance)
"""

import sys
import json
from datetime import datetime, timedelta, timezone

# 台灣時區
TZ_TAIPEI = timezone(timedelta(hours=8))

def get_taipei_time():
    return datetime.now(TZ_TAIPEI)

def fetch_indicators(start_date=None, end_date=None):
    """Fetch macro indicators via yfinance.

    Legacy behavior (default): fetches the last 5 trading days via
    ``yf.Ticker().history(period="5d")`` and returns the latest close
    with day-over-day change.

    Historical range mode (T-2.1): when ``start_date`` and ``end_date``
    are provided as ISO ``YYYY-MM-DD`` strings, uses
    ``yf.Ticker().history(start=start_date, end=end_date)`` instead.
    In this mode, returns ALL trading days in the inclusive range
    [start_date, end_date) — yfinance end is exclusive, so the caller
    should pass end_date + 1 day to include end_date.
    Each result key maps to a list of ``{date, value}`` dicts under the
    ``history`` field, plus the latest snapshot under the legacy fields
    (``value``, ``date``, ``change``, ``pct``) for backward compat.

    Args:
        start_date: Optional ISO date string (YYYY-MM-DD). When provided
            with end_date, switches to historical range mode.
        end_date: Optional ISO date string (YYYY-MM-DD). Exclusive in
            yfinance semantics (caller should add 1 day to include
            the desired end date).

    Returns:
        dict: Same structure as legacy mode when no range args given.
        When range args given, each ticker key gains a ``history`` list
        of per-date {date, value} entries, sorted ascending by date.
    """
    import yfinance as yf

    tickers = {
        "US10Y": ("^TNX", "美國10年公債殖利率", "%"),
        "US2Y": ("2YY=F", "美國2年公債殖利率", "%"),
        "US13W": ("^IRX", "美國13週國庫券", "%"),
        "DXY": ("DX-Y.NYB", "美元指數", ""),
        "VIX": ("^VIX", "VIX恐慌指數", ""),
        "USDTWD": ("TWD=X", "台幣匯率(USD/TWD)", ""),
    }

    use_range = start_date is not None and end_date is not None

    results = {}
    for key, (ticker, name, unit) in tickers.items():
        try:
            t = yf.Ticker(ticker)
            if use_range:
                hist = t.history(start=start_date, end=end_date)
            else:
                hist = t.history(period="5d")
            if hist.empty:
                results[key] = {"name": name, "error": "NO_DATA", "unit": unit}
                continue

            # In range mode, build history list
            if use_range:
                history_list = []
                for idx, row in hist.iterrows():
                    history_list.append({
                        "date": str(idx.date()),
                        "value": float(row['Close']),
                    })
                results[key] = {
                    "name": name,
                    "unit": unit,
                    "history": history_list,
                    "count": len(history_list),
                }
                # Also populate legacy fields from the latest entry
                if history_list:
                    latest_entry = history_list[-1]
                    results[key]["value"] = latest_entry["value"]
                    results[key]["date"] = latest_entry["date"]
                    if len(history_list) >= 2:
                        prev = history_list[-2]
                        chg = latest_entry["value"] - prev["value"]
                        pct = (chg / prev["value"]) * 100 if prev["value"] else 0
                        results[key]["prev"] = prev["value"]
                        results[key]["change"] = chg
                        results[key]["pct"] = pct
                continue

            # Legacy mode (no range)
            latest = hist.iloc[-1]
            close = float(latest['Close'])

            if len(hist) >= 2:
                prev_close = float(hist.iloc[-2]['Close'])
                chg = close - prev_close
                pct = (chg / prev_close) * 100
                results[key] = {
                    "name": name,
                    "value": close,
                    "prev": prev_close,
                    "change": chg,
                    "pct": pct,
                    "unit": unit,
                    "date": str(latest.name.date()),
                }
            else:
                results[key] = {"name": name, "value": close, "unit": unit, "date": str(latest.name.date())}
        except Exception as e:
            results[key] = {"name": name, "error": str(e), "unit": unit}

    # 計算 10Y-2Y 利差
    if "US10Y" in results and "US2Y" in results and "value" in results["US10Y"] and "value" in results["US2Y"]:
        spread = results["US10Y"]["value"] - results["US2Y"]["value"]
        results["YIELD_SPREAD"] = {
            "name": "10Y-2Y 利差",
            "value": spread,
            "unit": "%",
        }
    
    return results

def assess_liquidity(data):
    """
    判斷資金環境：寬鬆 / 中性 / 緊縮
    基於多重指標綜合判斷
    """
    score = 0  # 正=寬鬆, 負=緊縮
    signals = []
    
    # VIX
    vix = data.get("VIX", {})
    if "value" in vix:
        v = vix["value"]
        if v < 15:
            score += 2
            signals.append(f"VIX {v:.1f}（極度樂觀）→ 資金充裕")
        elif v < 20:
            score += 1
            signals.append(f"VIX {v:.1f}（平穩）→ 市場無恐慌")
        elif v < 30:
            score -= 1
            signals.append(f"VIX {v:.1f}（偏高）→ 市場警戒")
        else:
            score -= 2
            signals.append(f"VIX {v:.1f}（恐慌）→ 資金避險")
    
    # 10Y 殖利率趨勢
    us10y = data.get("US10Y", {})
    if "change" in us10y:
        chg = us10y["change"]
        if chg < -0.05:
            score += 1
            signals.append(f"10Y殖利率下降 {chg:+.2f} → 利率預期走低（寬鬆）")
        elif chg > 0.05:
            score -= 1
            signals.append(f"10Y殖利率上升 {chg:+.2f} → 利率預期走高（緊縮）")
    
    # 美元指數
    dxy = data.get("DXY", {})
    if "change" in dxy:
        chg = dxy["change"]
        if chg < -0.3:
            score += 1
            signals.append(f"美元指數下跌 {chg:+.2f} → 美元走弱，資金外溢（有利台股）")
        elif chg > 0.3:
            score -= 1
            signals.append(f"美元指數上漲 {chg:+.2f} → 美元走強，資金回流美國（不利台股）")
    
    # 台幣匯率
    twd = data.get("USDTWD", {})
    if "change" in twd:
        chg = twd["change"]
        # 台幣升值(change<0) = 外資流入 = 有利
        if chg < -0.05:
            score += 1
            signals.append(f"台幣升值 {chg:+.3f} → 外資流入（有利台股）")
        elif chg > 0.05:
            score -= 1
            signals.append(f"台幣貶值 {chg:+.3f} → 外資流出（不利台股）")
    
    # 10Y-2Y 利差
    spread = data.get("YIELD_SPREAD", {})
    if "value" in spread:
        s = spread["value"]
        if s > 0:
            signals.append(f"10Y-2Y 利差 +{s:.2f}% → 正常（長期利率高於短期）")
            if s > 0.5:
                score += 1
        elif s < 0:
            score -= 2
            signals.append(f"10Y-2Y 利差 {s:.2f}% → ⚠️ 倒掛（經濟衰退預警）")
    
    # 綜合判斷
    if score >= 3:
        verdict = "🟢 資金寬鬆"
    elif score >= 1:
        verdict = "🟡 偏向寬鬆"
    elif score == 0:
        verdict = "⚪ 中性"
    elif score >= -2:
        verdict = "🟠 偏向緊縮"
    else:
        verdict = "🔴 資金緊縮"
    
    return verdict, signals, score

def format_report(data, verdict, signals, score):
    now = get_taipei_time()
    date_str = now.strftime("%Y/%m/%d (%a)")
    
    lines = []
    lines.append(f"📊 **總體經濟晨報**")
    lines.append(f"📅 {date_str} · 台股開盤前快覽")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    
    # Fed 利率方向
    us13w = data.get("US13W", {})
    us2y = data.get("US2Y", {})
    lines.append("🏦 **Fed 利率方向**")
    if "value" in us13w:
        lines.append(f"  13W國庫券: {us13w['value']:.2f}%")
    if "value" in us2y:
        lines.append(f"  2Y公債: {us2y['value']:.2f}%")
    spread = data.get("YIELD_SPREAD", {})
    if "value" in spread:
        s = spread["value"]
        if s > 0:
            lines.append(f"  10Y-2Y利差: +{s:.2f}% (正常)")
        else:
            lines.append(f"  10Y-2Y利差: {s:.2f}% ⚠️倒掛")
    lines.append("")
    
    # 五大指標表格
    lines.append("📈 **五大指標**")
    
    indicator_order = ["US10Y", "DXY", "USDTWD", "VIX"]
    for key in indicator_order:
        d = data.get(key, {})
        name = d.get("name", key)
        unit = d.get("unit", "")
        if "error" in d:
            lines.append(f"  {name}: 取得失敗")
            continue
        val = d.get("value", 0)
        if "change" in d:
            chg = d.get("change", 0)
            pct = d.get("pct", 0)
            arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "─")
            val_str = f"{val:.2f}{unit}" if unit else f"{val:.2f}"
            chg_str = f"{chg:+.2f}" if abs(chg) < 100 else f"{chg:+.1f}"
            lines.append(f"  {name}: {val_str} {arrow}{chg_str} ({pct:+.2f}%)")
        else:
            val_str = f"{val:.2f}{unit}" if unit else f"{val:.2f}"
            lines.append(f"  {name}: {val_str}")
    
    lines.append("")
    
    # 資金環境判斷
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💰 **資金環境判斷: {verdict}**")
    lines.append(f"  (綜合評分: {score:+d})")
    lines.append("")
    
    for s in signals:
        lines.append(f"  • {s}")
    
    lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📋 **對台股影響**")
    
    if score >= 1:
        lines.append(f"  → 資金面偏多，有利台股表現")
    elif score <= -2:
        lines.append(f"  → 資金面偏空，台股承壓")
    else:
        lines.append(f"  → 資金面中性，看個股表現")
    
    lines.append("")
    lines.append(f"⏱ 資料日期: {data.get('US10Y',{}).get('date','N/A')}")
    lines.append(f"來源: Yahoo Finance · 製作: M2")
    
    return "\n".join(lines)

def main():
    try:
        data = fetch_indicators()
        verdict, signals, score = assess_liquidity(data)
        report = format_report(data, verdict, signals, score)
        print(report)
        
        # 保存 JSON 供 debug
        now = get_taipei_time()
        # Phase 6.1 portability: log dir via central boundary (FIE_DATA_DIR > repo layout)
        import os
        try:
            from db import get_log_dir as _log_dir
            log_dir = _log_dir()
        except ImportError:
            log_dir = os.path.join(os.environ.get("FIE_DATA_DIR", os.getcwd()), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = f"{log_dir}/{now.strftime('%Y-%m-%d')}.json"
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump({"data": data, "verdict": verdict, "score": score, "signals": signals, "report": report}, f, ensure_ascii=False, indent=2)
        
        # 寫入 SQLite 歷史資料庫
        try:
            from db import save_macro_daily
            save_macro_daily(data, verdict, score, signals)
            print(f"✅ 已寫入歷史資料庫", file=sys.stderr)
        except Exception as db_err:
            print(f"⚠️ 歷史資料庫寫入失敗: {db_err}", file=sys.stderr)
        
        return 0
    except Exception as e:
        print(f"❌ 報告生成失敗: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())