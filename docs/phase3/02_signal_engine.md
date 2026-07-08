# Phase 3 — Signal Engine

狀態：DESIGN ONLY
上游：Phase 2B BaseReport.fetch() 產出 `RawSignal`
下游：Macro / Industry / Company Scorer

---

## 0. 為什麼需要 Signal Engine

Phase 2B 框架裡，`BaseReport.fetch()` 已經會產出 structured data（例如 yfinance 拉回的 `info` dict、TWSE T86 的近月累積、RSS item list）。但這些資料**形狀各異**：

- yfinance 給 `{"trailingPE": 32.8, "returnOnEquity": 0.362, ...}`（欄位 80+ 個、單位混雜）
- T86 給 `{"code": "2330", "foreign_net": 99000, "prop_net": -21000, "trading_days": 24}`
- DIGITIMES RSS 給 `{"title": "...", "pub_date": "...", "description": "...", "category": "半導體"}`

Scorer 無法直接吃這種異質資料。Signal Engine 的職責：**把所有來源收斂成同一個 `NormalizedSignal` schema**，並做 weighting / decay / confidence 計算。

---

## 1. 五大職責

```
RawSignal (來源原生物)
   │
   │ ingestion
   ▼
TypedSignal (來源別 schema: YFinanceSignal | RSSSignal | T86Signal | CustomSignal)
   │
   │ normalization
   ▼
NormalizedSignal (統一 schema，所有 scorer 共用)
   │
   │ weighting + decay + confidence
   ▼
WeightedSignal (附加 weight / confidence / age)
   │
   │ aggregation
   ▼
AggregatedSignal (多源合併後的單一聚合)
```

### 1.1 Ingestion
- 從 `BaseReport.fetch()` 的 return value 抽取。
- 每個 source 寫一個 **SourceAdapter**：`YFinanceSourceAdapter` / `RSSSourceAdapter` / `T86SourceAdapter` / `MacroDailySourceAdapter`。
- Adapter 職責：把來源 dict 轉成 `TypedSignal`（一個 source 一種 schema，不混雜）。

### 1.2 Normalization
- 把 `TypedSignal` 轉成 `NormalizedSignal`：
  - 統一 `unit`（百分比一律小數 0.0~1.0；金額一律 TWD/USD 標明；日期一律 ISO 8601 + timezone）
  - 統一 `magnitude`（標準化 z-score 或 min-max）
  - 統一 `direction`（bullish / bearish / neutral）

### 1.3 Weighting
- 每個 source 有 **source_weight**（信譽權重，0~1）
  - 例：yfinance 0.9、TWSE T86 0.95、DIGITIMES 0.7、財訊 0.6、未驗證 RSS 0.3
- 每個 signal 類型有 **type_weight**（訊號強度權重，0~1）
  - 例：央行升息 1.0、月營收 0.7、單一新聞 0.3
- final_weight = source_weight × type_weight × recency_factor

### 1.4 Decay
- 每個信號有 `decay_function: Literal["linear", "exponential", "step", "none"]` 和 `half_life_days: float`
- 例：央行利率 14 天 half-life（政策面變化慢）；新聞 1 天 half-life（容易被新訊息覆蓋）；月營收 30 天 half-life（公告後一個月仍有意義，但逐漸淡出）
- 公式：
  - linear: `weight *= max(0, 1 - age_days / half_life_days)`
  - exponential: `weight *= 0.5 ** (age_days / half_life_days)`
  - step: `weight = 1.0 if age_days <= step_threshold else 0.0`
  - none: `weight *= 1.0`

### 1.5 Confidence
- 每個 signal 帶 `confidence: float` (0~1)
- 計算來源：
  - 來源完整性（missing fields 越多 confidence 越低）
  - 資料新舊（24h 內 = 1.0；72h+ = 0.5）
  - 跨源一致（同方向多源 confirm → confidence 上升；衝突 → 下降）
- 用在 scorer 階段：低 confidence 的 signal 對最終分數的影響打折。

### 1.6 Aggregation
- 把多個 `WeightedSignal` 合併成單一 `AggregatedSignal`
- 預設策略：weighted_sum + 多源 alignment check
  - 對同一 entity + 同一維度（如「2330 的 valuation」）的多個信號做加權
  - 如果方向衝突（bullish 0.7 vs bearish 0.6），降低聚合 confidence
- 可擴展：median / trimmed_mean / consensus 等策略

---

## 2. 核心資料結構（Data Model 摘要，完整見 07_data_model.md）

```python
@dataclass(frozen=True)
class RawSignal:
    """來源原生訊號，由 SourceAdapter 接收。"""
    source_id: str              # "yfinance.2330.TW"
    source_type: str            # "yfinance" | "rss" | "t86" | "macro" | "custom"
    raw_data: dict              # 來源原生物
    fetched_at: datetime        # 抓取時間 (UTC, tz-aware)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedSignal:
    """統一 schema 的信號，所有 scorer 共用。"""
    signal_id: str              # hash(source_id + signal_type + entity_id)
    entity_type: str            # "company" | "industry" | "macro" | "news"
    entity_id: str              # "2330" | "AI" | "FED_RATE" | "news:abc123"
    signal_type: str            # "pe_ratio" | "revenue_growth" | "rate_hike" | "sentiment"
    value: float                # 數值（單位見 unit）
    unit: str                   # "ratio" | "pct" | "twd" | "usd" | "score" | "count"
    direction: Literal["bullish", "bearish", "neutral"]
    timestamp: datetime         # 該數值對應的時間（不是 fetch 時間）
    source: str                 # "yfinance" | "t86" | "digitimes" | "wealth" | "cnyes"
    source_ref: str             # 原始 URL / API endpoint / RSS feed（debug 用）


@dataclass(frozen=True)
class WeightedSignal:
    """Normalize 完後加上權重/信心/衰減。"""
    signal: NormalizedSignal
    source_weight: float        # 0~1
    type_weight: float          # 0~1
    recency_weight: float       # 0~1 (decay 結果)
    final_weight: float         # source × type × recency
    confidence: float           # 0~1
    decay_function: str         # "linear" | "exponential" | "step" | "none"
    half_life_days: float | None
    evidence: list[EvidenceItem] = field(default_factory=list)


@dataclass(frozen=True)
class AggregatedSignal:
    """多源合併後的單一聚合。"""
    entity_type: str
    entity_id: str
    signal_type: str
    weighted_sum: float         # 最終值
    contributors: list[WeightedSignal]  # 所有 source 對此聚合的貢獻
    direction_consensus: Literal["bullish", "bearish", "neutral", "mixed"]
    aggregate_confidence: float # 0~1
```

---

## 3. Source Adapter 介面

```python
class SourceAdapter(ABC):
    @abstractmethod
    def adapt(self, raw: RawSignal) -> list[NormalizedSignal]:
        """把 RawSignal 拆成一個或多個 NormalizedSignal。"""
        ...

class YFinanceSourceAdapter(SourceAdapter):
    """從 yfinance .info dict 抽出 ~20 個關鍵欄位，轉成多個 NormalizedSignal。"""

class RSSSourceAdapter(SourceAdapter):
    """從 RSS item 抽出標題、情緒（heuristic + 之後可接 LLM）。"""

class T86SourceAdapter(SourceAdapter):
    """從 T86 累積資料抽出近月法人買賣超。"""
```

預設 adapters 為 Phase 3 內建；未來自訂 source 只要實作 SourceAdapter 介面 + 註冊到 Registry。

---

## 4. Configuration

```yaml
# config/phase3/sources.yaml
sources:
  yfinance:
    source_weight: 0.9
    type_weights:
      pe_ratio: 0.7
      roe: 0.9
      revenue_growth: 0.9
      # ... 完整欄位清單
  t86:
    source_weight: 0.95
    type_weights:
      foreign_net: 1.0
      prop_net: 0.9
  digitimes_rss:
    source_weight: 0.7
    type_weights:
      ai: 0.8
      semiconductor: 0.9
  cnyes_rss:
    source_weight: 0.5
    type_weights:
      headline: 0.4
  macro_yfinance:
    source_weight: 0.9
    type_weights:
      fed_rate: 1.0
      us10y: 0.9
      vix: 0.7
      dxy: 0.6

# config/phase3/decay.yaml
decay:
  fed_rate:
    function: linear
    half_life_days: 14
  monthly_revenue:
    function: exponential
    half_life_days: 30
  news_headline:
    function: exponential
    half_life_days: 1.0
  institutional_flow:
    function: step
    step_threshold_days: 5
  pe_ratio:
    function: none
```

---

## 5. CLI / API 介面

```python
from phase3.signal_engine import SignalEngine
from phase3.config import load_config

config = load_config("/home/ubuntu/macro-report/config/phase3/")
engine = SignalEngine(config)

# 餵 RawSignal
raw = RawSignal(
    source_id="yfinance.2330.TW",
    source_type="yfinance",
    raw_data={"trailingPE": 32.8, "returnOnEquity": 0.362, ...},
    fetched_at=datetime.now(UTC),
)
weighted = engine.process(raw)  # list[WeightedSignal]

# 餵多源 + 聚合
aggregated = engine.aggregate(weighted, target_entity="company:2330", signal_type="valuation")
# → AggregatedSignal(weighted_sum=..., contributors=..., confidence=0.85)
```

CLI：
```bash
python3 /home/ubuntu/macro-report/phase3/cli.py signal process --source yfinance --entity 2330
python3 /home/ubuntu/macro-report/phase3/cli.py signal aggregate --entity 2330 --dim valuation
```

---

## 6. 單元測試要點

- 同一個 RawSignal 經過 YFinanceSourceAdapter 必產出固定的 NormalizedSignal 列表（不隨機）。
- decay 函式對 0/half_life/2×half_life 輸入要產出 1.0/0.5/0.25（exp）或 1.0/0.5/0.0（linear）。
- aggregation 在「單一 source」、「多源同向」、「多源衝突」三種情境要產出正確 direction_consensus。
- 改 config 後 score 要 deterministic（config hash 必記在 ScoreResult 內）。
- confidence 在 missing fields 時要下降。
- performance：1000 個 signal aggregation < 100ms（純函式，不許 I/O）。

---

## 7. 不在 Signal Engine 範圍

- ❌ 排程（Signal Engine 是被動的；排程由 Dispatcher 決定何時呼叫）
- ❌ 派送（aggregated signal 自己不會送 Telegram；要透過 Renderer）
- ❌ 抓資料（純吃 `BaseReport.fetch()` 的產出）
- ❌ 學習 / 動態調整 weights（Phase 4+）

---

## 8. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 來源增加時 schema 漂移 | NormalizedSignal 是 frozen dataclass，缺欄位就 fail fast；adapter 寫 unit test 鎖行為 |
| Decay 設計不對導致舊新聞持續影響分數 | decay_config 集中管理；每個 scorer 啟用前做 30 天 backtest |
| 信心值算法太複雜，debug 困難 | confidence = `completeness × recency × consensus` 三個分量各自記錄，可拆解 |
| 聚合時多源衝突未被發現 | direction_consensus 必填；mixed 狀態要在 ScoreResult.explanation 顯著標出 |

---

_對應模組：`phase3/signals/`（in 10_repo_structure.md）_
