# Phase 3 — Company Scoring

狀態：DESIGN ONLY
目標產出：`CompanyScore` in [-100, +100]，附 7 個維度子分數 + 完整 evidence
被誰消費：Research Graph、Report Renderer、Phase 4 之後的 portfolio

---

## 0. 一句話

Company Scorer 把 7 個維度（financial_quality / growth / profitability / valuation / momentum / risk / news_sentiment）的 weighted sum 收斂成一個 -100~100 的單一分數，每個維度都附 evidence 與信心值；同時會讀取 MacroContext 對分數做條件調整。

---

## 1. 七個維度

| Dim ID | 名稱 | 主要指標 | Default Weight |
|---|---|---|---|
| `financial_quality` | 財務品質 | ROE、ROA、debt/equity、current ratio、free cash flow | 0.20 |
| `growth` | 成長性 | 營收 YoY、EPS YoY、free cash flow 成長、guidance | 0.15 |
| `profitability` | 獲利能力 | 毛利率、營業利益率、淨利率、NIM（金融股） | 0.15 |
| `valuation` | 評價 | PE、PB、PEG、EV/EBITDA、dividend yield | 0.15 |
| `momentum` | 動能 | 1M/3M/6M 漲幅、距 52 週高點、相對大盤 | 0.10 |
| `risk` | 風險 | beta、debt ratio、earnings volatility、新聞風險事件 | 0.10 |
| `news_sentiment` | 新聞情緒 | RSS / 鉅亨網 標題情緒、事件嚴重度 | 0.15 |

**Default weights 加總 = 1.0。** 全部可在 `config/phase3/company_weights.yaml` 改。

---

## 2. 各維度公式

### 2.1 Financial Quality (20%)
```python
financial_quality_score = (
    0.30 * roe_score            # ROE > 25% = +60, >15% = +20, <8% = -40
    + 0.20 * roa_score          # ROA > 10% = +50, >5% = +20, <2% = -30
    + 0.20 * debt_equity_score  # D/E < 0.3 = +40, <1 = 0, >2 = -40
    + 0.15 * current_ratio_score
    + 0.15 * fcf_score          # FCF > 0 持續 = +30
)
```

### 2.2 Growth (15%)
```python
growth_score = (
    0.35 * revenue_yoy_score    # >20% = +60, >0 = +20, <-10% = -50
    + 0.35 * eps_yoy_score      # >30% = +60, >0 = +20, <-20% = -60
    + 0.20 * fcf_growth_score
    + 0.10 * guidance_score     # 法人預期上修 = +30
)
```

### 2.3 Profitability (15%)
```python
profitability_score = (
    0.4 * gross_margin_score    # >50% = +60, >30% = +20, <10% = -40
    + 0.3 * operating_margin_score
    + 0.3 * net_margin_score
)
# 金融股特化：自動偵測 sector="金融"，改用：
# profitability_score = 0.5 * nim_growth_score + 0.5 * interest_spread_score
```

### 2.4 Valuation (15%)
```python
valuation_score = (
    -0.4 * pe_score             # PE < 10 = +40, <20 = +20, >40 = -30
    + 0.3 * pb_score            # PB < 1 = +40, <3 = +20, >5 = -20
    + 0.2 * peg_score           # PEG < 1 = +30, <2 = 0, >3 = -20
    + 0.1 * dividend_yield_score # >4% = +30
)
```

### 2.5 Momentum (10%)
```python
momentum_score = (
    0.4 * price_momentum_1m     # 漲 = +, 跌 = -，幅度反映強度
    + 0.3 * price_momentum_3m
    + 0.2 * dist_from_52w_high   # 距高點 <5% = +30, <-30% = -30
    + 0.1 * relative_to_market   # vs ^TWII / ^GSPC
)
```

### 2.6 Risk (10%)
```python
risk_score = (
    -0.4 * beta_score           # beta > 1.5 = -30, <0.8 = +20
    - 0.3 * debt_ratio_score    # 高槓桿 = -30
    - 0.2 * earnings_volatility
    - 0.1 * risk_event_count    # 新聞中「訴訟/違約/裁員」事件數
)
# 註：risk 越高 → score 越低，所以是反向
```

### 2.7 News Sentiment (15%)
```python
news_sentiment_score = (
    0.6 * sentiment_score       # RSS 標題情緒分析（heuristic + LLM）
    + 0.4 * event_severity_score  # 事件嚴重度（裁員/收購/主管異動/訴訟）
)
# sentiment: bullish = +, bearish = -
# event_severity: 重大利多 = +60, 一般利多 = +20, 中性 = 0, 一般利空 = -20, 重大利空 = -60
```

---

## 3. 跨層調整（讀取 MacroContext）

Company Score 不是純粹的「公司 7 維加權」；它會讀 MacroContext 對最終分數做條件調整：

```python
def apply_macro_context(company_score, macro_ctx, company_sector):
    """根據總體環境與公司產業敏感度做調整。"""
    adjustment = 0.0

    # 科技股對 liquidity 敏感
    if company_sector in ["半導體", "AI", "電子"]:
        adjustment += macro_ctx.dimensions["liquidity"].score * 0.05  # 最多 ±5

    # 金融股對 rates 敏感
    if company_sector == "金融":
        adjustment += macro_ctx.dimensions["rates"].score * 0.08      # 最多 ±8

    # 出口股對 dxy (=liquidity dim) 敏感
    if company_sector in ["紡織", "工具機", "塑膠"]:
        adjustment += macro_ctx.dimensions["liquidity"].score * 0.04  # 最多 ±4

    # 所有股都對 geopolitics 有感
    adjustment += macro_ctx.dimensions["geopolitics"].score * 0.03   # 最多 ±3

    return company_score + adjustment
# 結果：科技金融混合 -10~+20 不等，視 macro 而定
```

`sector_sensitivity` 規則在 `config/phase3/sector_macro_sensitivity.yaml` 配置。

---

## 4. 完整 CompanyScore Data Model

```python
@dataclass(frozen=True)
class CompanyScore:
    code: str                              # "2330"
    name: str                              # "台積電"
    sector: str                            # "半導體"
    score: float                           # -100~100 (after macro adjustment)
    raw_score: float                       # -100~100 (before macro adjustment)
    confidence: float                      # 0~1
    dimensions: list[DimensionResult]      # 7 個
    macro_adjustment: float                # -100~+100 區間的調整量
    macro_context_hash: str | None         # 用哪一份 macro score 調整的
    overall_evidence: list[EvidenceItem]
    timestamp: datetime
    config_hash: str
    valid_until: datetime                  # TTL: company score 7 天有效

    def explain(self) -> str:
        """產出中文一行總結 + 7 維度 + macro 影響。"""
        ...

    def to_company_context(self) -> CompanyContext:
        """轉成 Research Graph 用的標準化物件。"""
        ...
```

### 4.1 Sub-Indicator 範例
```python
@dataclass(frozen=True)
class SubIndicatorResult:
    name: str                              # "roe"
    raw_value: float | None                # 0.362 (decimal ratio)
    raw_unit: str                          # "ratio"
    sub_score: float                       # -100~100
    transformation: str                    # "threshold" | "z_score" | "invert" | "range"
    threshold_legend: dict | None          # debug：哪個 threshold 命中
    source: str                            # "yfinance" | "t86" | "rss"
    source_ref: str                        # 原始 URL
    evidence: list[EvidenceItem]
```

---

## 5. 與現有 `assess_stock` 的關係

`company_monthly.py` 的 `assess_stock` 是 6 個 hardcoded 維度（ROE / revenue growth / earnings growth / PEG / PE / dividend yield）。Phase 3 不取代它，但**結構化**它：

- **雙軌期**（Phase 3A）：Phase 3 Company Scorer 跑出 7 維分數 + evidence，寫到 `intelligence.db`；現有 Telegram 月報照舊發。
- **過渡期**（Phase 3B）：現有 `format_stock_card` 改成讀 `CompanyScore`，但**同樣條件下必產出 byte-identical** 的 Telegram 訊息（驗證：拿 7 天資料比對）。
- **完全整合**（Phase 3C+）：Telegram 訊息最後多一段「🎯 綜合分數 +X」，以及「📋 解釋：ROE 優異 + 營收強勁 + 估值偏高」之類的短句。

驗證 gate：連跑 7 天，舊函式 verdict 與新 scorer 的前 5 維 weighted score 必須一致（±1 分以內容忍）。

---

## 6. Rating 對照（給 Telegram 顯示用）

```python
def score_to_rating(score: float) -> str:
    if score >= 60:  return "🌟 強烈買進"
    if score >= 30:  return "✅ 買進"
    if score >= 10:  return "🟡 中性偏多"
    if score >= -10: return "⚪ 中性"
    if score >= -30: return "🟠 減碼"
    if score >= -60: return "🔴 賣出"
    return "⚠️ 強烈賣出"
```

⚠️ **不對應到任何實際交易建議**。這只是表達「分數所屬區間」的視覺標記，後面會接完整 evidence 與解釋。

---

## 7. 與 Industry Scorer 的關係

Company Scorer 會讀 IndustryContext（產業分數）做產業相對強弱調整：

```python
def apply_industry_context(company_score, industry_ctx):
    """公司所屬產業如果強勢 +20，公司相對加分。"""
    if industry_ctx.score > 30:
        return company_score + 5
    if industry_ctx.score < -30:
        return company_score - 5
    return company_score
```

產業調整 5 分（範圍 -5~+5）。與 macro 調整**疊加**（不重複）。

---

## 8. Configuration 範例

```yaml
# config/phase3/company_weights.yaml
weights:
  financial_quality: 0.20
  growth: 0.15
  profitability: 0.15
  valuation: 0.15
  momentum: 0.10
  risk: 0.10
  news_sentiment: 0.15

# config/phase3/sector_macro_sensitivity.yaml
sector_sensitivity:
  半導體:    { liquidity: 0.05, rates: 0.02, geopolitics: 0.03 }
  AI:        { liquidity: 0.06, rates: 0.02, geopolitics: 0.04 }
  金融:      { rates: 0.08, liquidity: 0.03, monetary: 0.04 }
  電子:      { liquidity: 0.04, rates: 0.02, geopolitics: 0.03 }
  紡織:      { liquidity: 0.04, geopolitical: 0.04, rates: 0.02 }
  # ... 8 個產業預設，其他預設 0

# config/phase3/industry_adjustment.yaml
industry_relative_strength:
  enabled: true
  bonus_threshold: 30   # industry > 30 加 5
  penalty_threshold: -30
  adjustment_magnitude: 5
```

---

## 9. 單元測試要點

- 7 維度各做 table-driven 測試：固定 yfinance input → 固定 sub_score。
- 金融股偵測：sector="金融" 時自動用 NIM/spread 公式。
- MacroContext 調整：mock 一個 `+30 liquidity dim` → 科技股 +1.5、金融股 +0.9、其他 +0。
- IndustryContext 調整：mock 一個 `+40 industry` → company +5。
- confidence：缺 ROE（None）時 financial_quality 信心下降。
- `valid_until` 邏輯：7 天後 score 不可再用。
- `explain()` 必含 7 維度 + macro 影響 + industry 影響。

---

## 10. 風險與緩解

| 風險 | 緩解 |
|---|---|
| yfinance 欄位缺漏（小型股） | Sub-indicator None → 該子項不計入 weighted sum，weight 自動歸零重新分配 |
| 金融股 vs 非金融股公式混淆 | 啟動時 sector 偵測；sector map 在 `config/sector.yaml` 集中管理 |
| Macro 調整過度疊加 | 單一 macro dim 影響上限 5%、整體 macro 影響上限 20% |
| 動能分數在空頭市場全部變負 | 可在 config 設 momentum 預設 weight 為 0（空頭期手動關閉） |
| 新聞情緒 LLM hallucination | sentiment 預設啟用關鍵字+規則；LLM 標註是 opt-in |

---

_對應模組：`phase3/scoring/company.py`_
