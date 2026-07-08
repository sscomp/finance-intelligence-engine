# Phase 3 — Macro Scoring

狀態：DESIGN ONLY
目標產出：`MacroScore` in [-100, +100]，附 6 個維度子分數 + 完整 evidence
被誰消費：Industry Scorer（cross-layer）、Company Scorer（conditional adjustment）、Report Renderer

---

## 0. 一句話

Macro Scorer 把 6 個維度（economic / monetary / inflation / rates / liquidity / geopolitics）各自的 weighted sum 收斂成一個 -100~100 的單一分數，每個維度都附 evidence 與信心值。

---

## 1. 六個維度（DIMENSIONS）

| Dim ID | 名稱 | 主要指標 | Source | Default Weight |
|---|---|---|---|---|
| `economic` | 經濟指標 | GDP growth、PMI、就業、零售銷售 | yfinance ^DJI / ^GSPC / 政府公告；fallback 鉅亨網 | 0.20 |
| `monetary` | 貨幣政策 | Fed funds rate、ECB/DOT、BoJ、央行聲明 | yfinance ^IRX / ^FVX / ^TNX；新聞 RSS | 0.20 |
| `inflation` | 通膨 | CPI、PPI、core CPI、PCE | yfinance TIP/TBT/TLT 推估 + 鉅亨網 | 0.15 |
| `rates` | 利率結構 | 短中長期利差、yield curve 斜率 | yfinance ^IRX/^FVX/^TNX/^TYX | 0.20 |
| `liquidity` | 流動性 | M2、VIX、美元指數、信用利差 | yfinance ^VIX / DX-Y.NYB；TWSE T86 | 0.15 |
| `geopolitics` | 地緣政治 | 戰爭、制裁、貿易戰、選舉、政策變動 | RSS（財訊/鉅亨網/DIGITIMES）關鍵字 + LLM 標註 | 0.10 |

**Default weights 加總 = 1.0。** 全部可在 `config/phase3/macro_weights.yaml` 改。

---

## 2. 各維度公式

### 2.1 Economic (20%)
```python
economic_score = (
    0.4 * gdp_growth_score        # GDP YoY: >3% = +60, >1% = +20, 0% = 0, <-1% = -60
    + 0.3 * pmi_score              # PMI: >55 = +60, >50 = +20, <45 = -60
    + 0.2 * employment_score       # Unemployment delta: 下降 = +40, 上升 >0.5pp = -40
    + 0.1 * retail_score           # Retail sales YoY: >5% = +40, <-2% = -40
)
# normalize to [-100, 100]
```

### 2.2 Monetary (20%)
```python
monetary_score = (
    0.5 * fed_funds_score          # 升息 = -40, 降息 = +40, 持平 = 0 (相對預期)
    + 0.3 * dot_plot_score         # 升息預期偏鷹 = -30
    + 0.2 * central_bank_unanimity # 央行一致寬鬆 = +20
)
```

### 2.3 Inflation (15%)
```python
inflation_score = (
    -0.6 * cpi_score               # CPI 過高 = bearish (-)，回到目標 = bullish (+)
    + 0.4 * core_inflation_score   # core 通膨 = 更穩健訊號
)
# 註：inflation 對「股價」是反向的，但對「通膨受益股」是正向；Phase 3 採股價觀點
```

### 2.4 Rates (20%)
```python
rates_score = (
    0.5 * yield_curve_score        # 倒掛 = bearish -60, 正常 = +20, 陡峭 = +40
    + 0.3 * real_rate_score        # 實質利率上升 = -30
    + 0.2 * rate_volatility_score  # 利率波動率 = -20
)
```

### 2.5 Liquidity (15%)
```python
liquidity_score = (
    -0.4 * vix_score               # VIX 上升 = -50
    - 0.3 * dxy_score              # 美元強 = 新興市場 -30
    + 0.3 * m2_growth_score        # M2 上升 = +30
    + 0.2 * credit_spread_score    # 信用利差擴大 = -30
)
```

### 2.6 Geopolitics (10%)
```python
geopolitics_score = (
    geopolitical_event_score       # RSS 標註事件嚴重度 × 距離台灣的相關性
    # 事件類型：sanc­tion / war / election / trade_dispute / policy_shift
    # 每類有 base impact: war -80, sanction -50, election 0~±30, trade_dispute -40
)
```

---

## 3. 維度內子指標計算細節

### 3.1 Score 標準化規則
每個子指標 (sub-indicator) 都計算一個 raw value（例如 `cpi_yoy = 3.2%`），然後映射到 **[-100, 100]** 的 sub-score：

| 類型 | 公式 |
|---|---|
| **threshold** (大部分指標) | piecewise linear: `< 嚴重壞值` 給 -100, `< 警戒值` 給 -60, `< 中性` 給 0, `< 警戒好值` 給 +60, `>= 極好` 給 +100 |
| **z-score** (vs 歷史) | `sub_score = clip(z * 30, -100, 100)` (3σ 為上限) |
| **invert** (通膨、利率) | `sub_score = -1 * threshold_score(raw_value)` |
| **range** (VIX) | `0-15 = +60, 15-25 = 0, 25-35 = -60, >35 = -100` |

所有 sub-score 計算都是**純函式**，無副作用，可在 unit test 中用 table-driven 測試。

### 3.2 信心值 (confidence) 計算
```python
dimension_confidence = (
    0.4 * data_completeness   # 缺指標 → 信心下降
    + 0.3 * source_quality    # 多源確認 → 上升
    + 0.2 * recency            # 新鮮資料 → 上升
    + 0.1 * consensus          # 多子指標同方向 → 上升
)
```

---

## 4. 收斂到最終 MacroScore

```python
final_score = sum(
    dimension_score * dimension_weight
    for dim in [economic, monetary, inflation, rates, liquidity, geopolitics]
)
# 已經是 [-100, 100]（因為每個維度都 normalize 過）
```

### 4.1 信心調整
```python
final_score_with_confidence = (
    final_score * macro_overall_confidence
)
# 例：score = +40 但 confidence = 0.5 → adjusted = +20
# 信心值會在 ScoreResult.confidence 欄位，給下游參考
```

### 4.2 跨層傳遞
MacroScore 會被存到一個 shared context object，Industry/Company Scorer 讀取：

```python
@dataclass(frozen=True)
class MacroContext:
    score: float                          # -100~100
    confidence: float                     # 0~1
    dimensions: dict[str, DimensionResult]  # 6 個維度各自結果
    evidence: list[EvidenceItem]
    timestamp: datetime
    config_hash: str

    def adjustment_factor(self, company_sector: str) -> float:
        """給定公司產業，回傳 macro 對該產業的調整係數（-10%~+10%）。"""
        # 例：金融股對 rates 敏感 → rates_dim.score * 0.1
        # 例：科技股對 liquidity 敏感 → liquidity_dim.score * 0.1
        ...
```

---

## 5. 完整 MacroScore Data Model

```python
@dataclass(frozen=True)
class DimensionResult:
    name: str                              # "monetary"
    score: float                           # -100~100
    sub_indicators: list[SubIndicatorResult]
    weight: float                          # 該維度在 macro 中的權重
    confidence: float                      # 0~1
    evidence: list[EvidenceItem]


@dataclass(frozen=True)
class SubIndicatorResult:
    name: str                              # "fed_funds_rate"
    raw_value: float | str | None          # 5.25 (%) | "above consensus" | None
    raw_unit: str                          # "%" | "bps" | "z-score" | "text"
    sub_score: float                       # -100~100
    transformation: str                    # "threshold" | "z_score" | "invert" | "range"
    threshold_legend: dict | None          # debug 用：哪個 threshold 命中
    evidence: list[EvidenceItem]


@dataclass(frozen=True)
class MacroScore:
    score: float                           # -100~100
    confidence: float                      # 0~1
    dimensions: list[DimensionResult]      # 6 個
    overall_evidence: list[EvidenceItem]   # top-N across dimensions
    timestamp: datetime
    config_hash: str
    valid_until: datetime                  # TTL（例：macro score 24h 內有效）

    def explain(self) -> str:
        """產出中文一行總結 + 各維度短句。"""
        ...

    def to_macro_context(self) -> MacroContext:
        """轉成跨層傳遞物件。"""
        ...
```

---

## 6. 解釋範例（鼎鼎在 Telegram 上看到的）

```
🌐 **總體環境評分: +28 / 100**（信心 0.78）
  📈 經濟 (+45)    — PMI 54.2 擴張、就業穩健
  💰 貨幣 (-15)    — Fed 升息 1 碼，市場已預期
  📊 通膨 (-20)    — CPI 3.2% 略高於目標
  📉 利率 (+40)    — 殖利率曲線正常化、實質利率穩
  💧 流動性 (+35)  — VIX 14.5 低檔、M2 溫和成長
  🌍 地緣 (-5)     — 區域衝突未擴大、政策不確定性中等

🎯 總體判斷：寬鬆偏多，風險情緒有利台股
🔍 信心 0.78 — 主要受通膨與地緣不確定性壓制
```

每一行都是從 `DimensionResult.explain()` 來的，沒有自由文字生成（避免 LLM hallucination）。

---

## 7. 與現有 `liquidity_score` 的關係

現有 `macro_daily.py` 算的 `liquidity_score` 是本維度子集（只看 VIX / DXY / 美元 / 利差）。Phase 3 不取代它：

- **雙軌期**（Phase 3A）：Phase 3 Macro Scorer 算出 6 維分數並寫到 `intelligence.db`；現有 Telegram 報告照舊發。
- **過渡期**（Phase 3B）：現有報告的「資金環境判斷」段落改成讀 Macro Scorer 的 `liquidity` 維度（**byte-identical** 條件：scorer 必產出與原計算同分）。
- **完全整合**（Phase 3C+）：Telegram 報告最後多一段「🌐 總體評分 +X」。

過渡期 byte-identical 驗證：跑現有 macro_daily.py 7 天，把它的 verdict 與新 scorer 的 liquidity 維度 verdict 比對；差異 = bug。

---

## 8. Configuration 範例

```yaml
# config/phase3/macro_weights.yaml
weights:
  economic: 0.20
  monetary: 0.20
  inflation: 0.15
  rates: 0.20
  liquidity: 0.15
  geopolitics: 0.10

# config/phase3/macro_indicators.yaml
indicators:
  gdp_growth:
    type: threshold
    source: yfinance_or_worldbank
    thresholds:
      - { raw: 3.0, score: 60 }
      - { raw: 1.0, score: 20 }
      - { raw: 0.0, score: 0 }
      - { raw: -1.0, score: -60 }
    weight: 0.4
  pmi:
    type: range
    ranges:
      - { low: 55, high: 100, score: 60 }
      - { low: 50, high: 55, score: 20 }
      - { low: 45, high: 50, score: -20 }
      - { low: 0, high: 45, score: -60 }
    weight: 0.3
  vix:
    type: range
    ranges:
      - { low: 0, high: 15, score: 60 }
      - { low: 15, high: 25, score: 0 }
      - { low: 25, high: 35, score: -60 }
      - { low: 35, high: 999, score: -100 }
    weight: 0.4
  fed_funds:
    type: invert_threshold
    expected_change: "as_announced"
    thresholds:
      - { raw: "hike_above_consensus", score: -60 }
      - { raw: "hike_as_expected", score: -20 }
      - { raw: "hold", score: 0 }
      - { raw: "cut_as_expected", score: 20 }
      - { raw: "cut_below_consensus", score: 60 }
    weight: 0.5
```

---

## 9. 單元測試要點

- 6 個維度各做 table-driven 測試：固定 raw input → 固定 sub_score。
- weight 改變後，final score 必為線性組合（sum(w*dim_score)），不可有未預期耦合。
- confidence 在「缺資料」、「多源同向」、「多源衝突」三情境分數正確。
- `valid_until` 邏輯：現在 + TTL 過期後，score 不可再用。
- `explain()` 必含 6 個維度 + 信心值，不可漏。
- `adjustment_factor()` 對不同 sector 必產出不同係數（金融股 ≠ 科技股）。

---

## 10. 風險與緩解

| 風險 | 緩解 |
|---|---|
| Geopolitics 維度過度依賴 LLM 標註 | 預設用關鍵字+規則標註；LLM 標註是 opt-in 強化層 |
| 6 維度權重被亂改導致分數劇變 | config_hash 鎖版本；改動前必須做 30 天 backtest；change log |
| Sub-indicator 公式不透明 | 每次 scoring 必存 sub_score + transformation + threshold_legend 進 SQLite |
| 與現有 macro_daily.py 衝突 | 雙軌期 byte-identical 驗證；過渡期用 feature flag 切換 |

---

_對應模組：`phase3/scoring/macro.py`_
