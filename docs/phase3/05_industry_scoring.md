# Phase 3 — Industry Scoring

狀態：DESIGN ONLY
目標產出：`IndustryScore` in [-100, +100]，附 6 個維度子分數 + 完整 evidence
被誰消費：Company Scorer（相對強弱）、Report Renderer（週報）、Research Graph

---

## 0. 一句話

Industry Scorer 把 6 個維度（rotation / relative_strength / cyclicality / macro_sensitivity / industry_news / capital_flow）的 weighted sum 收斂成一個 -100~100 的單一分數。**每個產業**（AI、半導體、伺服器、PCB、網通、重電、金融、高股息 + 未來新增的）都會跑這個 scorer。

---

## 1. 六個維度

| Dim ID | 名稱 | 主要指標 | Default Weight |
|---|---|---|---|
| `rotation` | 類股輪動 | 相對大盤 N 日漲跌、產業 ETF 資金流、Market internals | 0.15 |
| `relative_strength` | 相對強度 | vs 0050 / ^TWII / 同類股的 RS rating、20 日相對強度 | 0.20 |
| `cyclicality` | 週期性 | PMI 變化、半導體 BB 值、出口 YoY、存貨週轉 | 0.15 |
| `macro_sensitivity` | 總體敏感度 | 對應 MacroContext 中相關維度的反應（金融→rates、科技→liquidity） | 0.15 |
| `industry_news` | 產業新聞 | 過去 7 天新聞量、正負面比、關鍵事件 | 0.20 |
| `capital_flow` | 資金流 | 外資/投信 對該產業的近月買賣超（彙整成分產業層級） | 0.15 |

**Default weights 加總 = 1.0。** 全部可在 `config/phase3/industry_weights.yaml` 改。

---

## 2. 各維度公式

### 2.1 Rotation (15%)
```python
rotation_score = (
    0.4 * sector_relative_perf_5d    # 該產業 ETF 5 日 vs 0050
    + 0.3 * sector_relative_perf_20d # 20 日相對
    + 0.3 * money_flow_proxy          # 上漲家數/下跌家數 - 50%
)
# 範圍：±60 (相對大盤 5% = +60)
```

### 2.2 Relative Strength (20%)
```python
relative_strength_score = (
    0.5 * rs_rating                  # 0~99 percentile vs 所有產業
    + 0.3 * industry_etf_momentum_1m
    + 0.2 * stock_breadth_pct         # 該產業上漲家數 / 總家數
)
```

### 2.3 Cyclicality (15%)
```python
cyclicality_score = (
    0.4 * pmi_direction_score         # PMI 上升 = 景氣循環產業 +；下降 = 防禦產業 +
    + 0.3 * book_to_bill_score        # 半導體 BB > 1 = +40
    + 0.3 * export_yoy_score          # 出口 YoY > 10% = +40
)
```

### 2.4 Macro Sensitivity (15%)
```python
macro_sensitivity_score = (
    industry_macro_beta["liquidity"] * macro.liquidity_dim +
    industry_macro_beta["rates"] * macro.rates_dim +
    industry_macro_beta["monetary"] * macro.monetary_dim +
    industry_macro_beta["geopolitics"] * macro.geopolitics_dim
)
# industry_macro_beta 在 config/sector_macro_sensitivity.yaml（同 Company 用）
# 例：半導體 = {liquidity: 0.5, rates: 0.2, monetary: 0.1, geopolitics: 0.3}
```

### 2.5 Industry News (20%)
```python
industry_news_score = (
    0.5 * news_volume_change          # 本週新聞量 vs 上週；增加 = +20 (注意力)
    + 0.3 * sentiment_polarity        # 正面 - 負面，標準化
    + 0.2 * event_severity_aggregate  # 重大利多/利空事件加權
)
# 註：event_severity 用關鍵字 + 規則標註；LLM 標註是 opt-in
```

### 2.6 Capital Flow (15%)
```python
capital_flow_score = (
    0.5 * foreign_net_industry_total  # 外資對該產業近月累積
    + 0.5 * prop_net_industry_total   # 投信近月累積
)
# 標準化：對所有產業做 z-score，乘 30，clip 到 [-100, 100]
```

---

## 3. 資金流聚合（產業層級）

`capital_flow` 需要把 T86 個股資料聚合成產業層級：

```python
def aggregate_industry_flow(industry_id, stock_codes, t86_data):
    """把個股 T86 累積資料加總到產業。"""
    foreign_sum = sum(t86_data[code]["foreign_net"] for code in stock_codes)
    prop_sum = sum(t86_data[code]["prop_net"] for code in stock_codes)
    return {
        "foreign_net": foreign_sum,
        "prop_net": prop_sum,
        "stock_count": len(stock_codes),
        "stocks_covered": [c for c in stock_codes if c in t86_data],
    }
```

產業 ↔ 個股對照在 `config/phase3/industry_stocks.yaml`：
```yaml
industries:
  AI:        ["2330", "2382", "2376", "3017", "3231", "6669"]  # 台積/廣達/技嘉/奇鋐/緯穎/矽力
  半導體:    ["2330", "2303", "3711", "5347", "3034", "2454", "2379"]
  金融:      ["2881", "2882", "2886", "2891", "2884", "2885", "2892", "5880"]
  # ...
```

---

## 4. 完整 IndustryScore Data Model

```python
@dataclass(frozen=True)
class IndustryScore:
    industry_id: str                      # "AI" | "半導體" | "金融"
    industry_name: str                    # 同上（中文）
    score: float                          # -100~100
    confidence: float                     # 0~1
    dimensions: list[DimensionResult]     # 6 個
    constituent_count: int                # 該產業有幾支成分股
    macro_context_hash: str | None
    overall_evidence: list[EvidenceItem]
    timestamp: datetime
    config_hash: str
    valid_until: datetime                 # TTL: industry score 24h

    def explain(self) -> str:
        ...

    def to_industry_context(self) -> IndustryContext:
        ...
```

---

## 5. 與現有 `industry_weekly.py` 的關係

`industry_weekly.py` 目前的「本週被提到幾次」是純計數，沒有 score。Phase 3 不取代它：

- **雙軌期**（Phase 3A）：Industry Scorer 跑出 6 維分數 + evidence，寫到 `intelligence.db`；現有週報照舊發。
- **過渡期**（Phase 3B）：現有週報的「本週總覽」段落後面新增一行「🌡️ 產業分數 (強→弱)：AI +45 / 半導體 +30 / ...」。
- **完全整合**（Phase 3C+）：週報變成「以分數為主，新聞為輔」的結構。

過渡期 byte-identical 驗證：跑現有 industry_weekly.py 7 天，現有 count-based 段落必須 100% 一致（Phase 3 只新增段落，不改既有段落）。

---

## 6. 與 Company Scorer 的關係

Industry Scorer 必在 Company Scorer **之前**跑（Company 會讀 IndustryContext 做相對強弱調整）。

時序：
1. Macro Scorer 跑（每日一次，valid 24h）
2. Industry Scorer 跑（每日或每週，看 config）
3. Company Scorer 跑（用 MacroContext + IndustryContext 兩者調整）

### 6.1 產業相對強弱對個股的影響
```python
# 在 Company Scorer 內
industry_ctx = graph.get_industry_context(company.industry_id)
if industry_ctx.score > 30:
    company_score += 5   # 強勢產業 → 個股 +5
elif industry_ctx.score < -30:
    company_score -= 5   # 弱勢產業 → 個股 -5
```

---

## 7. Configuration 範例

```yaml
# config/phase3/industry_weights.yaml
weights:
  rotation: 0.15
  relative_strength: 0.20
  cyclicality: 0.15
  macro_sensitivity: 0.15
  industry_news: 0.20
  capital_flow: 0.15

# config/phase3/industry_stocks.yaml
industries:
  AI:
    name: "AI"
    stocks: ["2330", "2382", "2376", "3017", "3231", "6669"]
    keywords: ["AI", "人工智慧", "GPU", "HBM", "CoWoS"]
  半導體:
    name: "半導體"
    stocks: ["2330", "2303", "3711", "5347", "3034", "2454", "2379"]
    keywords: ["半導體", "晶圓", "封測", "IC設計"]
  金融:
    name: "金融"
    stocks: ["2881", "2882", "2886", "2891", "2884", "2885", "2892", "5880"]
    keywords: ["金融", "金控", "銀行", "壽險"]
  # ... 8 個產業
```

---

## 8. 單元測試要點

- 6 維度各做 table-driven 測試。
- 資金流聚合：mock 5 支個股 T86，驗證加總正確。
- 8 個產業預設都有 IndustryScore，不可漏（漏 = 警告，不 fail）。
- `valid_until`：24h 過期後不可再用。
- 與 Company Scorer 互動：industry +40 → company +5，industry -40 → company -5。
- 與 Macro Scorer 互動：半導體 macro_sensitivity = 0.5×liquidity + 0.2×rates + ...，公式正確。
- 計算效能：8 個產業全跑 < 5 秒（含 T86 抓取時間）。

---

## 9. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 個股歸類錯誤（同一支股被分到多產業） | stocks 清單以 `code` 為主；產業關鍵字僅供新聞分類用 |
| 資金流 z-score 對小型產業不穩 | 標準化用 percentile 而非 z-score；最少 3 支成分股才計算 |
| Industry news 維度被新聞雜訊主導 | 新聞來源限定 DIGITIMES / 財訊 / 鉅亨網；情緒標註預設關鍵字+規則 |
| Rotation 維度 5 日太短雜訊大 | 預設 20 日為主、5 日為輔；可調 |
| 與 Company Scorer 雙向耦合難 debug | Industry/Company 各自存 `macro_context_hash` 與 `industry_context_hash`，可獨立 replay |

---

_對應模組：`phase3/scoring/industry.py`_
