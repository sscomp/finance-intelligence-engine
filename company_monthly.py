#!/usr/bin/env python3
"""
公司研究月報 — 台灣50成分股基本面分析
每月 12 號派送，配合月營收公布後的完整數據

指標：
  EPS / ROE / 毛利率 / 現金股利 / 本益比
  + 營收成長率 / 盈餘成長率 / 股價淨值比 / 股利支付率
  + 52週高低點 / 距高點跌幅

分組呈現：
  半導體 / 電子 / 金融 / 傳產 / 其他
"""

import sys
import json
import os
from datetime import datetime, timedelta, timezone

# Phase 2B Step 2 — format_net_shares moved to shared module.
# Local definition below (the original) has been removed; the name is
# preserved by importing the shared implementation. Behavior is identical
# (verified by tests/test_format_helpers.py — 14/46 tests pin this helper,
# all 46/46 PASS post-extraction).
from reports.common.format import format_net_shares  # noqa: E402,F401

TZ_TAIPEI = timezone(timedelta(hours=8))

# Phase 6.1 portability: paths via the central configuration boundary
# (FIE_CONFIG_DIR / FIE_DATA_DIR) instead of the hard-coded
# /home/ubuntu/macro-report layout.
try:
    from phase3.paths import config_dir as _fie_config_dir, data_dir as _fie_data_dir
except ImportError:  # pragma: no cover - phase3 不在 sys.path 時退回 CWD 版面
    def _fie_config_dir():
        return os.path.join(os.environ.get("FIE_CONFIG_DIR", os.getcwd()))
    def _fie_data_dir():
        return os.path.join(os.environ.get("FIE_DATA_DIR", os.getcwd()))

CONFIG_PATH = os.path.join(_fie_config_dir(), "taiwan50_config.json")
LOG_DIR = os.path.join(_fie_data_dir(), "logs")

def get_taipei_now():
    return datetime.now(TZ_TAIPEI)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_num(val, decimals=2, suffix=""):
    """Safely format a number."""
    if val is None or val == "N/A":
        return "N/A"
    try:
        v = float(val)
        if suffix == "%":
            # Ratio like 0.3621 -> 36.21%
            return f"{v*100:.{decimals}f}%"
        elif suffix == "pct":
            # Already in percentage units like -8.37 -> -8.37%
            return f"{v:.{decimals}f}%"
        return f"{v:.{decimals}f}{suffix}"
    except:
        return "N/A"

def fetch_stock_fundamentals(code):
    """Fetch fundamental data for a single stock via yfinance."""
    import yfinance as yf
    
    sym = f"{code}.TW"
    try:
        t = yf.Ticker(sym)
        info = t.info
        
        price = info.get("currentPrice", info.get("regularMarketPrice"))
        high_52 = info.get("fiftyTwoWeekHigh")
        dist_from_high = None
        if price and high_52:
            try:
                dist_from_high = (price - high_52) / high_52 * 100
            except:
                pass
        
        result = {
            "code": code,
            "name": "",
            "sector": "",
            "price": price,
            "prev_close": info.get("regularMarketPreviousClose"),
            "dist_from_high": dist_from_high,
            "eps_ttm": info.get("epsTrailingTwelveMonths"),
            "eps_forward": info.get("forwardEps"),
            "pe_trailing": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "roe": info.get("returnOnEquity"),
            "roa": info.get("returnOnAssets"),
            "gross_margin": info.get("grossMargins"),
            "operating_margin": info.get("operatingMargins"),
            "profit_margin": info.get("profitMargins"),
            "dividend_rate": info.get("trailingAnnualDividendRate"),
            "dividend_yield": info.get("trailingAnnualDividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "pb_ratio": info.get("priceToBook"),
            "book_value": info.get("bookValue"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "market_cap": info.get("marketCap"),
            "beta": info.get("beta"),
            "target_mean": info.get("targetMeanPrice"),
            "peg_ratio": info.get("pegRatio"),
            "error": None,
        }
        
        # === 金融股特殊指標 ===
        # 金融股不適合用毛利率，改用 NIM 成長率、利差、ROA
        qi = t.quarterly_income_stmt
        if qi is not None and not qi.empty:
            # NIM 成長率 (YoY): Net Interest Income 最新季 vs 4季前
            if "Net Interest Income" in qi.index:
                nim_vals = qi.loc["Net Interest Income"]
                if len(nim_vals) >= 5:
                    nim_latest = nim_vals.iloc[0]
                    nim_yoy = nim_vals.iloc[4]
                    if nim_yoy and nim_yoy != 0:
                        result["nim_growth"] = (nim_latest - nim_yoy) / abs(nim_yoy) * 100
            
            # 利差代理: (Interest Income - Interest Expense) / Interest Income
            if "Interest Income" in qi.index and "Interest Expense" in qi.index:
                ii = qi.loc["Interest Income"].iloc[0]
                ie = qi.loc["Interest Expense"].iloc[0]
                if ii and ie and ii != 0:
                    result["interest_spread"] = (ii - ie) / ii * 100
        
        return result
    except Exception as e:
        return {
            "code": code,
            "name": "",
            "sector": "",
            "error": str(e),
        }

def fetch_all_stocks(config):
    """Fetch fundamentals for all constituents."""
    constituents = config["constituents"]
    results = []
    
    total = len(constituents)
    for i, stock in enumerate(constituents):
        code = stock["code"]
        name = stock["name"]
        sector = stock["sector"]
        
        print(f"  [{i+1}/{total}] {code} {name}...", file=sys.stderr)
        
        data = fetch_stock_fundamentals(code)
        data["name"] = name
        data["sector"] = sector
        
        # Calculate distance from 52-week high
        if data.get("price") and data.get("52w_high"):
            try:
                data["dist_from_high"] = (data["price"] - data["52w_high"]) / data["52w_high"] * 100
            except:
                data["dist_from_high"] = None
        
        results.append(data)
    
    return results

def assess_stock(data):
    """
    Quick assessment of a stock based on fundamentals.
    Returns a rating and notes.
    """
    if data.get("error"):
        return "❓", ["數據缺失"]
    
    score = 0
    notes = []
    
    # ROE
    roe = data.get("roe")
    if roe is not None:
        roe_pct = roe * 100
        if roe_pct > 25:
            score += 2
            notes.append(f"ROE {roe_pct:.1f}% 優異")
        elif roe_pct > 15:
            score += 1
            notes.append(f"ROE {roe_pct:.1f}% 良好")
        elif roe_pct < 8:
            score -= 1
            notes.append(f"ROE {roe_pct:.1f}% 偏低")
    
    # 營收成長
    rev_g = data.get("revenue_growth")
    if rev_g is not None:
        rev_pct = rev_g * 100
        if rev_pct > 20:
            score += 2
            notes.append(f"營收成長 +{rev_pct:.1f}% 強勁")
        elif rev_pct > 0:
            score += 1
            notes.append(f"營收成長 +{rev_pct:.1f}%")
        elif rev_pct < -10:
            score -= 2
            notes.append(f"營收衰退 {rev_pct:.1f}%")
        elif rev_pct < 0:
            score -= 1
            notes.append(f"營收微減 {rev_pct:.1f}%")
    
    # 盈餘成長
    earn_g = data.get("earnings_growth")
    if earn_g is not None:
        earn_pct = earn_g * 100
        if earn_pct > 30:
            score += 2
            notes.append(f"盈餘成長 +{earn_pct:.1f}%")
        elif earn_pct > 0:
            score += 1
        elif earn_pct < -20:
            score -= 2
            notes.append(f"盈餘衰退 {earn_pct:.1f}%")
    
    # 本益比合理性（用 PEG）
    peg = data.get("peg_ratio")
    pe = data.get("pe_trailing")
    if peg is not None and peg < 1.0:
        score += 1
        notes.append(f"PEG {peg:.2f} 低估可能")
    elif pe is not None and pe > 50:
        score -= 1
        notes.append(f"本益比 {pe:.1f} 偏高")
    
    # 殖利率
    dy = data.get("dividend_yield")
    if dy is not None:
        dy_pct = dy * 100
        if dy_pct > 4:
            score += 1
            notes.append(f"殖利率 {dy_pct:.1f}% 高息")
        elif dy_pct < 1 and pe is not None and pe > 20:
            notes.append(f"殖利率 {dy_pct:.1f}% 偏低")
    
    # 毛利率 (非金融股) / NIM 成長率 (金融股)
    is_financial = data.get("sector") == "金融"
    if is_financial:
        nim_g = data.get("nim_growth")
        if nim_g is not None:
            if nim_g > 20:
                score += 2
                notes.append(f"NIM成長 +{nim_g:.1f}% 擴張強")
            elif nim_g > 5:
                score += 1
                notes.append(f"NIM成長 +{nim_g:.1f}%")
            elif nim_g < -5:
                score -= 1
                notes.append(f"NIM成長 {nim_g:.1f}% 縮窄")
        # 利差
        spread = data.get("interest_spread")
        if spread is not None:
            if spread > 60:
                notes.append(f"利差 {spread:.1f}% 穩健")
            elif spread < 30:
                notes.append(f"利差 {spread:.1f}% 偏窄")
    else:
        gm = data.get("gross_margin")
        if gm is not None:
            gm_pct = gm * 100
            if gm_pct > 50:
                notes.append(f"毛利率 {gm_pct:.1f}% 高壁壘")
            elif gm_pct < 15:
                notes.append(f"毛利率 {gm_pct:.1f}% 低利潤")
    
    # Rating
    if score >= 5:
        rating = "🌟"
    elif score >= 3:
        rating = "✅"
    elif score >= 1:
        rating = "🟡"
    elif score >= -1:
        rating = "⚪"
    elif score >= -3:
        rating = "🟠"
    else:
        rating = "⚠️"
    
    return rating, notes

def format_stock_card(data, inst_data=None):
    """Format a single stock's data into a compact card.
    
    Args:
        data: stock fundamentals dict
        inst_data: institutional investor data dict (foreign/prop net buy/sell), optional
    """
    name = data.get("name", "?")
    code = data.get("code", "?")
    sector = data.get("sector", "")
    
    if data.get("error"):
        return f"  {code} {name}: ❌ 數據缺失"
    
    rating, notes = assess_stock(data)
    
    price = safe_num(data.get("price"), 0, "")
    pe = safe_num(data.get("pe_trailing"), 1)
    eps = safe_num(data.get("eps_ttm"), 2)
    roe = safe_num(data.get("roe"), 1, "%")
    div_rate = safe_num(data.get("dividend_rate"), 2)
    div_yield = safe_num(data.get("dividend_yield"), 2, "%")
    rev_g = safe_num(data.get("revenue_growth"), 1, "%")
    earn_g = safe_num(data.get("earnings_growth"), 1, "%")
    pb = safe_num(data.get("pb_ratio"), 2)
    dist_high = safe_num(data.get("dist_from_high"), 1, "pct")
    target = safe_num(data.get("target_mean"), 0)
    
    is_financial = sector == "金融"
    
    lines = []
    lines.append(f"  {rating} **{code} {name}** — 收盤 ${price}")
    
    if is_financial:
        # 金融股：用 NIM 成長率、利差、ROA 取代毛利率
        nim_g = safe_num(data.get("nim_growth"), 1, "pct")
        spread = safe_num(data.get("interest_spread"), 1, "pct")
        roa = safe_num(data.get("roa"), 2, "%")
        lines.append(f"     PE:{pe} | EPS:{eps} | ROE:{roe} | ROA:{roa}")
        lines.append(f"     NIM成長:{nim_g} | 利差:{spread} | 股利:{div_rate}(殖利率{div_yield})")
    else:
        # 非金融股：正常顯示毛利率
        gm = safe_num(data.get("gross_margin"), 1, "%")
        lines.append(f"     PE:{pe} | EPS:{eps} | ROE:{roe} | 毛利:{gm}")
        lines.append(f"     股利:{div_rate} (殖利率{div_yield}) | PB:{pb} | 距高點:{dist_high}")
    
    lines.append(f"     營收成長:{rev_g} | 盈餘成長:{earn_g} | 目標價:{target}")
    
    # 法人動向
    if inst_data:
        foreign = inst_data.get("foreign", 0)
        prop = inst_data.get("prop", 0)
        days = inst_data.get("trading_days_fetched", 0)
        foreign_str = format_net_shares(foreign)
        prop_str = format_net_shares(prop)
        lines.append(f"     🏦 外資近月:{foreign_str} | 投信近月:{prop_str} ({days}個交易日)")
    
    if notes:
        lines.append(f"     💡 {' · '.join(notes[:3])}")
    
    return "\n".join(lines)

def format_monthly_report(all_data, config, inst_data=None):
    now = get_taipei_now()
    date_str = now.strftime("%Y/%m/%d")
    
    # Group by sector
    sectors = {}
    for d in all_data:
        sec = d.get("sector", "其他")
        if sec not in sectors:
            sectors[sec] = []
        sectors[sec].append(d)
    
    # Sector order
    sector_order = ["半導體", "電子", "金融", "傳產", "其他"]
    
    lines = []
    lines.append(f"🏢 **公司研究月報**")
    lines.append(f"📅 {date_str} · {config['etf_name']}({config['etf']})成分股")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    
    # Summary
    total = len(all_data)
    errors = sum(1 for d in all_data if d.get("error"))
    success = total - errors
    
    # Top performers
    valid = [d for d in all_data if not d.get("error")]
    
    # Sort by revenue growth
    by_rev = sorted(valid, key=lambda x: x.get("revenue_growth") or -999, reverse=True)
    top_growth = [d for d in by_rev if d.get("revenue_growth") and d["revenue_growth"] > 0.1][:5]
    
    # Sort by dividend yield
    by_yield = sorted(valid, key=lambda x: x.get("dividend_yield") or -999, reverse=True)
    top_yield = [d for d in by_yield if d.get("dividend_yield") and d["dividend_yield"] > 0.02][:5]
    
    # Sort by PE (cheapest)
    by_pe = sorted(valid, key=lambda x: x.get("pe_trailing") or 9999)[:5]
    
    # Closest to 52-week high (smallest negative dist_from_high)
    by_dist = sorted(valid, key=lambda x: x.get("dist_from_high") if x.get("dist_from_high") is not None else -999, reverse=True)
    near_high = [d for d in by_dist if d.get("dist_from_high") is not None and d["dist_from_high"] > -5][:3]
    
    # Farthest from 52-week high (potential value)
    far_high = sorted(valid, key=lambda x: x.get("dist_from_high") or 0)[:5]
    
    lines.append(f"📊 **月報總覽**：{success} 檔成功 / {errors} 檔缺失")
    lines.append("")
    
    # Highlight: Top revenue growth
    if top_growth:
        lines.append(f"🚀 **營收成長 Top 5**")
        for d in top_growth:
            r, notes = assess_stock(d)
            rev = safe_num(d.get("revenue_growth"), 1, "%")
            lines.append(f"  {r} {d['code']} {d['name']} — 營收成長 {rev}")
        lines.append("")
    
    # Highlight: High dividend
    if top_yield:
        lines.append(f"💰 **高殖利率 Top 5**")
        for d in top_yield:
            r, notes = assess_stock(d)
            dy = safe_num(d.get("dividend_yield"), 2, "%")
            div = safe_num(d.get("dividend_rate"), 2)
            pe = safe_num(d.get("pe_trailing"), 1)
            lines.append(f"  {r} {d['code']} {d['name']} — 殖利率 {dy} (股利{div}, PE {pe})")
        lines.append("")
    
    # Highlight: Low PE (potential value)
    if by_pe:
        lines.append(f"📉 **本益比最低 Top 5（可能價值股）**")
        for d in by_pe:
            if d.get("pe_trailing") and d["pe_trailing"] > 0:
                r, notes = assess_stock(d)
                pe = safe_num(d.get("pe_trailing"), 1)
                eps = safe_num(d.get("eps_ttm"), 2)
                lines.append(f"  {r} {d['code']} {d['name']} — PE {pe} (EPS {eps})")
        lines.append("")
    
    # Highlight: Near 52-week high (momentum)
    if near_high:
        lines.append(f"📈 **逼近52週高點（動能強）**")
        for d in near_high:
            r, notes = assess_stock(d)
            dist = safe_num(d.get("dist_from_high"), 1, "pct")
            price = safe_num(d.get("price"), 0)
            lines.append(f"  {r} {d['code']} {d['name']} — ${price} (距高點 {dist})")
        lines.append("")
    
    # Highlight: Far from 52-week high (potential buy)
    far_valid = [d for d in far_high if d.get("dist_from_high") and d["dist_from_high"] < -15]
    if far_valid:
        lines.append(f"🔍 **距52週高點 -15% 以上（潛在買點）**")
        for d in far_valid[:5]:
            r, notes = assess_stock(d)
            dist = safe_num(d.get("dist_from_high"), 1, "pct")
            price = safe_num(d.get("price"), 0)
            lines.append(f"  {r} {d['code']} {d['name']} — ${price} ({dist})")
        lines.append("")
    
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    
    # Detail by sector
    for sector in sector_order:
        stocks = sectors.get(sector, [])
        if not stocks:
            continue
        
        lines.append(f"📌 **{sector}** ({len(stocks)} 檔)")
        lines.append("")
        
        for d in stocks:
            # Get institutional data for this stock
            stock_inst = None
            if inst_data and d.get("code") in inst_data:
                stock_inst = inst_data[d["code"]]
            card = format_stock_card(d, stock_inst)
            lines.append(card)
            lines.append("")
        
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
    
    # === 法人動向排行榜 ===
    if inst_data:
        lines.append(f"🏦 **法人動向排行（近一個月）**")
        lines.append("")
        
        # Parse inst_data into list for sorting
        inst_list = []
        for code, v in inst_data.items():
            if not isinstance(v, dict):
                continue
            inst_list.append({
                "code": code,
                "foreign": v.get("foreign", 0),
                "prop": v.get("prop", 0),
                "total": v.get("total", 0),
            })
        
        # Find stock names
        name_map = {d["code"]: d["name"] for d in all_data if d.get("code")}
        for item in inst_list:
            item["name"] = name_map.get(item["code"], "")
        
        # Top foreign net buy
        by_foreign = sorted(inst_list, key=lambda x: x["foreign"], reverse=True)
        top_foreign_buy = [x for x in by_foreign if x["foreign"] > 0][:5]
        if top_foreign_buy:
            lines.append(f"🟢 **外資買超 Top 5**")
            for x in top_foreign_buy:
                lines.append(f"  {x['code']} {x['name']} — {format_net_shares(x['foreign'])}")
            lines.append("")
        
        # Top foreign net sell (most negative first)
        top_foreign_sell = sorted([x for x in inst_list if x["foreign"] < 0], key=lambda x: x["foreign"])[:5]
        if top_foreign_sell:
            lines.append(f"🔴 **外資賣超 Top 5**")
            for x in top_foreign_sell:
                lines.append(f"  {x['code']} {x['name']} — {format_net_shares(x['foreign'])}")
            lines.append("")
        
        # Top prop net buy
        by_prop = sorted(inst_list, key=lambda x: x["prop"], reverse=True)
        top_prop_buy = [x for x in by_prop if x["prop"] > 0][:5]
        if top_prop_buy:
            lines.append(f"🔵 **投信買超 Top 5**")
            for x in top_prop_buy:
                lines.append(f"  {x['code']} {x['name']} — {format_net_shares(x['prop'])}")
            lines.append("")
        
        # Top prop net sell (most negative first)
        top_prop_sell = sorted([x for x in inst_list if x["prop"] < 0], key=lambda x: x["prop"])[:5]
        if top_prop_sell:
            lines.append(f"🟣 **投信賣超 Top 5**")
            for x in top_prop_sell:
                lines.append(f"  {x['code']} {x['name']} — {format_net_shares(x['prop'])}")
            lines.append("")
        
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
    
    # Footer
    lines.append(f"📖 **看報告的思路**")
    lines.append(f"  1. 先看 EPS+ROE → 公司賺錢能力")
    lines.append(f"  2. 毛利率(金融股看NIM+利差) → 護城河深淺")
    lines.append(f"  3. 本益比+殖利率 → 現在買划不划算")
    lines.append(f"  4. 營收/盈餘成長 → 未來動能")
    lines.append(f"  5. 距52週高點 → 是追高還是撿便宜")
    lines.append(f"  6. 法人動向 → 基本面好+法人在買=最強組合")
    lines.append("")
    lines.append(f"⏱ 製作: M2 · {date_str}")
    lines.append(f"來源: Yahoo Finance + TWSE T86")
    
    return "\n".join(lines)

def main():
    try:
        config = load_config()
        print(f"📡 抓取台灣50成分股基本面...", file=sys.stderr)
        all_data = fetch_all_stocks(config)
        print(f"✅ 基本面抓取完成", file=sys.stderr)
        
        # 抓取法人動向
        print(f"🏦 抓取法人動向資料（近一個月）...", file=sys.stderr)
        try:
            from institutional import accumulate_institutional_data
            target_codes = [s["code"] for s in config["constituents"]]
            inst_data, days_fetched = accumulate_institutional_data(target_codes, days_back=35)
            print(f"✅ 法人動向抓取完成（{days_fetched}個交易日）", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ 法人動向抓取失敗: {e}", file=sys.stderr)
            inst_data = None
        
        report = format_monthly_report(all_data, config, inst_data)
        print(report)
        
        # 寫入 SQLite 歷史資料庫（個股基本面 + 法人動向 snapshot）
        try:
            from db import save_stock_monthly, save_institutional_snapshot
            for d in all_data:
                if not d.get("error"):
                    save_stock_monthly(d["code"], d["name"], d["sector"], d)
            if inst_data:
                days_fetched = 0
                for code, v in inst_data.items():
                    if isinstance(v, dict):
                        save_institutional_snapshot(
                            code, v.get("foreign", 0), v.get("prop", 0), v.get("total", 0), v.get("trading_days_fetched", 0)
                        )
                        days_fetched = v.get("trading_days_fetched", 0)
            print(f"✅ 已寫入歷史資料庫（{sum(1 for d in all_data if not d.get('error'))} 檔個股 + 法人動向）", file=sys.stderr)
        except Exception as db_err:
            print(f"⚠️ 歷史資料庫寫入失敗: {db_err}", file=sys.stderr)
        
        # Save log
        os.makedirs(LOG_DIR, exist_ok=True)
        now = get_taipei_now()
        log_file = f"{LOG_DIR}/company-{now.strftime('%Y-%m-%d')}.json"
        with open(log_file, "w", encoding="utf-8") as f:
            # Strip non-serializable
            clean_data = []
            for d in all_data:
                cd = {k: v for k, v in d.items() if v is None or isinstance(v, (str, int, float, bool))}
                clean_data.append(cd)
            json.dump({
                "date": now.isoformat(),
                "stock_count": len(all_data),
                "success_count": sum(1 for d in all_data if not d.get("error")),
            }, f, ensure_ascii=False, indent=2)
        
        return 0
    except Exception as e:
        print(f"❌ 月報生成失敗: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())