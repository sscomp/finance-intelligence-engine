# Phase 3 — Data Model

狀態：DESIGN ONLY
目標：所有 Phase 3 物件的 schema 統一規格、SQLite 表格、JSON manifest 結構。

---

## 0. 三層儲存策略

| 層 | 內容 | 儲存體 | 生命週期 |
|---|---|---|---|
| **L1 — Score Snapshots** | 每個 ScoreResult 的完整 snapshot | SQLite `intelligence.db.score_snapshot` | 永久（用於 backtest、trending、debug） |
| **L2 — Signal Log** | 每個 NormalizedSignal / WeightedSignal | SQLite `intelligence.db.signal_log` | 永久（用於 evidence trace、replay） |
| **L3 — Graph Edges** | Node + Edge | SQLite `intelligence.db.graph_nodes / graph_edges` | 永久（用於 traversal、tracing） |

**檔案**：`/home/ubuntu/macro-report/intelligence.db`（**獨立檔案**，不與現有 `macro_history.db` 混用，便於獨立備份與風險隔離）。

---

## 1. Python 物件（frozen dataclass）

### 1.1 RawSignal
```python
@dataclass(frozen=True)
class RawSignal:
    source_id: str              # "yfinance.2330.TW"
    source_type: str            # "yfinance" | "rss" | "t86" | "macro" | "custom"
    raw_data: dict
    fetched_at: datetime
    metadata: dict = field(default_factory=dict)
```

### 1.2 NormalizedSignal
```python
@dataclass(frozen=True)
class NormalizedSignal:
    signal_id: str              # hash(source_id + signal_type + entity_id + date_bucket)
    entity_type: str            # "company" | "industry" | "macro" | "news"
    entity_id: str              # "2330" | "AI" | "FED_RATE"
    signal_type: str            # "pe_ratio" | "roe" | "rate_hike" | "sentiment"
    value: float
    unit: str                   # "ratio" | "pct" | "twd" | "usd" | "score" | "count" | "z_score"
    direction: Literal["bullish", "bearish", "neutral"]
    timestamp: datetime         # 該數值對應時間（UTC, tz-aware）
    source: str                 # "yfinance" | "t86" | "digitimes" | "wealth" | "cnyes"
    source_ref: str             # URL / API endpoint / RSS feed
```

### 1.3 EvidenceItem
```python
@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str            # hash 自動產生
    source_type: str            # "yfinance" | "rss" | "t86" | "config" | "computed"
    source_ref: str             # 原始 URL / API path / config key
    raw_value: str              # 原始值的字串表示（debug 用）
    description: str            # 簡短敘述：「yfinance PE ratio 32.8」
    timestamp: datetime
    weight: float | None = None # 該 evidence 在計算中的實際權重
    metadata: dict = field(default_factory=dict)
```

### 1.4 SubIndicatorResult
```python
@dataclass(frozen=True)
class SubIndicatorResult:
    name: str                              # "roe"
    raw_value: float | str | None
    raw_unit: str
    sub_score: float                       # -100~100
    transformation: Literal["threshold", "z_score", "invert", "range", "invert_threshold", "lookup"]
    threshold_legend: dict | None = None
    source: str
    source_ref: str
    evidence: list[EvidenceItem] = field(default_factory=list)
```

### 1.5 DimensionResult
```python
@dataclass(frozen=True)
class DimensionResult:
    name: str                              # "financial_quality"
    score: float                           # -100~100
    sub_indicators: list[SubIndicatorResult]
    weight: float                          # 該維度在所屬 scorer 中的權重
    confidence: float                      # 0~1
    evidence: list[EvidenceItem] = field(default_factory=list)
```

### 1.6 ScoreResult（基底，Macro/Industry/Company 都繼承這個）
```python
@dataclass(frozen=True)
class ScoreResult:
    score_id: str                          # hash(entity_id + scorer_type + date)
    scorer_type: Literal["macro", "industry", "company"]
    entity_type: str
    entity_id: str
    score: float                           # -100~100
    confidence: float                      # 0~1
    dimensions: list[DimensionResult]
    overall_evidence: list[EvidenceItem]   # top-N
    timestamp: datetime
    config_hash: str                       # sha256 of merged config
    valid_until: datetime                  # TTL
    cross_layer_adjustments: list[CrossLayerAdjustment] = field(default_factory=list)

    def explain(self) -> str:
        """產出中文短句總結（rule-based，不調用 LLM）。"""
        ...


@dataclass(frozen=True)
class CrossLayerAdjustment:
    from_scorer: str                       # "macro" | "industry"
    from_score_id: str
    to_scorer: str
    to_score_id: str
    adjustment: float                      # -X~+Y
    reason: str                            # "liquidity +35 × sensitivity 0.05"
    evidence: list[EvidenceItem] = field(default_factory=list)
```

### 1.7 MacroScore / IndustryScore / CompanyScore（具體子類）
```python
@dataclass(frozen=True)
class MacroScore(ScoreResult):
    scorer_type: Literal["macro"] = "macro"
    entity_type: str = "macro"
    entity_id: str = "global"

    def to_macro_context(self) -> MacroContext: ...


@dataclass(frozen=True)
class IndustryScore(ScoreResult):
    scorer_type: Literal["industry"] = "industry"
    industry_name: str = ""
    constituent_count: int = 0

    def to_industry_context(self) -> IndustryContext: ...


@dataclass(frozen=True)
class CompanyScore(ScoreResult):
    scorer_type: Literal["company"] = "company"
    code: str = ""
    name: str = ""
    sector: str = ""
    raw_score: float = 0.0                 # 跨層調整前的分數
    macro_adjustment: float = 0.0
    macro_context_hash: str | None = None

    def to_company_context(self) -> CompanyContext: ...
```

### 1.8 Context Objects（跨層傳遞用）
```python
@dataclass(frozen=True)
class MacroContext:
    score: float
    confidence: float
    dimensions: dict[str, DimensionResult]
    score_id: str
    timestamp: datetime

    def adjustment_factor(self, company_sector: str) -> float: ...


@dataclass(frozen=True)
class IndustryContext:
    industry_id: str
    score: float
    confidence: float
    score_id: str
    timestamp: datetime


@dataclass(frozen=True)
class CompanyContext:
    code: str
    score: float
    confidence: float
    score_id: str
    timestamp: datetime
```

### 1.9 GraphNode / GraphEdge
```python
@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str                         # NodeType enum
    label: str
    created_at: datetime
    metadata: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    edge_type: str                         # EdgeType enum
    from_node_id: str
    to_node_id: str
    weight: float | None = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime
```

---

## 2. SQLite Schema

```sql
-- 1. score_snapshot
CREATE TABLE score_snapshot (
    score_id TEXT PRIMARY KEY,
    scorer_type TEXT NOT NULL,             -- 'macro' | 'industry' | 'company'
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_label TEXT,                     -- 顯示用 ("台積電" | "AI" | "global")
    score REAL NOT NULL,
    confidence REAL NOT NULL,
    raw_score REAL,                        -- company 才有
    macro_adjustment REAL,                 -- company 才有
    config_hash TEXT NOT NULL,
    macro_context_hash TEXT,
    industry_context_hash TEXT,
    timestamp TEXT NOT NULL,               -- ISO 8601 UTC
    valid_until TEXT NOT NULL,
    result_json TEXT NOT NULL              -- 完整 frozen dataclass JSON
);
CREATE INDEX idx_score_entity ON score_snapshot(entity_type, entity_id, timestamp DESC);
CREATE INDEX idx_score_type_time ON score_snapshot(scorer_type, timestamp DESC);

-- 2. signal_log
CREATE TABLE signal_log (
    signal_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    value REAL,
    unit TEXT,
    direction TEXT,                        -- 'bullish' | 'bearish' | 'neutral'
    source TEXT,
    source_ref TEXT,
    source_weight REAL,
    type_weight REAL,
    recency_weight REAL,
    final_weight REAL,
    confidence REAL,
    decay_function TEXT,
    half_life_days REAL,
    timestamp TEXT NOT NULL,
    expires_at TEXT,
    contributing_to_score_id TEXT,         -- 可選，指向 score_snapshot
    evidence_json TEXT
);
CREATE INDEX idx_signal_entity ON signal_log(entity_type, entity_id, timestamp DESC);
CREATE INDEX idx_signal_score ON signal_log(contributing_to_score_id);

-- 3. graph_nodes
CREATE TABLE graph_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_graph_nodes_type ON graph_nodes(node_type);

-- 4. graph_edges
CREATE TABLE graph_edges (
    edge_id TEXT PRIMARY KEY,
    edge_type TEXT NOT NULL,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    weight REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (from_node_id) REFERENCES graph_nodes(node_id),
    FOREIGN KEY (to_node_id) REFERENCES graph_nodes(node_id)
);
CREATE INDEX idx_graph_edges_from ON graph_edges(from_node_id, edge_type);
CREATE INDEX idx_graph_edges_to ON graph_edges(to_node_id, edge_type);
```

### 2.1 與現有 `macro_history.db` 的關係
- 既有 `macro_daily` / `stock_monthly` / `institutional_daily` 三張表**完全不動**。
- `intelligence.db` 為新檔案，獨立備份、獨立 query。
- 兩者 join 透過 `entity_id` 邏輯關聯（不在 DB 層做 FK，避免跨 DB 約束）。

---

## 3. JSON Manifest 結構

每個 Scorer / SourceAdapter 在註冊時必備 manifest：

### 3.1 Scorer Manifest
```yaml
# config/phase3/scorers/macro.yaml
name: macro
version: 1.0.0
description: "Macro environment scoring (6 dimensions)"
ttl_hours: 24
dimensions:
  - economic
  - monetary
  - inflation
  - rates
  - liquidity
  - geopolitics
default_weights:
  economic: 0.20
  monetary: 0.20
  inflation: 0.15
  rates: 0.20
  liquidity: 0.15
  geopolitics: 0.10
depends_on: []               # 沒有前置依賴
output:
  score_id_template: "score:macro:{date}"
  context_class: "MacroContext"
```

```yaml
# config/phase3/scorers/industry.yaml
name: industry
version: 1.0.0
description: "Industry scoring (6 dimensions)"
ttl_hours: 24
dimensions: [rotation, relative_strength, cyclicality, macro_sensitivity, industry_news, capital_flow]
default_weights:
  rotation: 0.15
  relative_strength: 0.20
  cyclicality: 0.15
  macro_sensitivity: 0.15
  industry_news: 0.20
  capital_flow: 0.15
depends_on: [macro]          # 必須先有 macro score
output:
  score_id_template: "score:industry:{industry_id}:{date}"
  context_class: "IndustryContext"
```

```yaml
# config/phase3/scorers/company.yaml
name: company
version: 1.0.0
description: "Company scoring (7 dimensions + macro/industry adjustment)"
ttl_hours: 168              # 7 天
dimensions: [financial_quality, growth, profitability, valuation, momentum, risk, news_sentiment]
default_weights:
  financial_quality: 0.20
  growth: 0.15
  profitability: 0.15
  valuation: 0.15
  momentum: 0.10
  risk: 0.10
  news_sentiment: 0.15
depends_on: [macro, industry]
output:
  score_id_template: "score:company:{code}:{date}"
  context_class: "CompanyContext"
```

### 3.2 Source Adapter Manifest
```yaml
# config/phase3/sources/yfinance.yaml
name: yfinance
version: 1.0.0
type: financial_data
source_weight: 0.9
supported_signal_types:
  - pe_ratio
  - roe
  - roa
  - revenue_growth
  - earnings_growth
  - peg_ratio
  - trailing_pe
  - pb_ratio
  - beta
  - fifty_two_week_high
  - fifty_two_week_low
  - target_mean_price
  - market_cap
rate_limit: 60              # 60 calls/min
batch_size: 1
timeout_seconds: 30
```

---

## 4. 序列化 / 反序列化

### 4.1 JSON
所有 frozen dataclass 必支援 `to_dict()` 與 `from_dict()`：

```python
def to_dict(self) -> dict:
    return {
        "score_id": self.score_id,
        "score": self.score,
        "confidence": self.confidence,
        "timestamp": self.timestamp.isoformat(),
        "valid_until": self.valid_until.isoformat(),
        "dimensions": [d.to_dict() for d in self.dimensions],
        "overall_evidence": [e.to_dict() for e in self.overall_evidence],
        "config_hash": self.config_hash,
        "cross_layer_adjustments": [a.to_dict() for a in self.cross_layer_adjustments],
        # ... 其它欄位
    }

@classmethod
def from_dict(cls, data: dict) -> "ScoreResult":
    return cls(
        score_id=data["score_id"],
        score=data["score"],
        confidence=data["confidence"],
        timestamp=datetime.fromisoformat(data["timestamp"]),
        valid_until=datetime.fromisoformat(data["valid_until"]),
        dimensions=[DimensionResult.from_dict(d) for d in data["dimensions"]],
        overall_evidence=[EvidenceItem.from_dict(e) for e in data["overall_evidence"]],
        config_hash=data["config_hash"],
        cross_layer_adjustments=[CrossLayerAdjustment.from_dict(a) for a in data["cross_layer_adjustments"]],
    )
```

### 4.2 Schema Versioning
每個 result JSON 必含 `schema_version: "3.0"`。升級時用 `SchemaMigrator` 處理版本差異。

---

## 5. Hash 與快取

### 5.1 signal_id 生成
```python
def make_signal_id(source: str, entity: str, signal_type: str, date_bucket: str) -> str:
    return hashlib.sha256(
        f"{source}:{entity}:{signal_type}:{date_bucket}".encode()
    ).hexdigest()[:16]
```
同 source + entity + signal_type + 同日 = 同一 signal_id = UPSERT。

### 5.2 score_id 生成
```python
def make_score_id(scorer: str, entity: str, date: str) -> str:
    return hashlib.sha256(
        f"{scorer}:{entity}:{date}".encode()
    ).hexdigest()[:16]
```

### 5.3 config_hash 生成
```python
def compute_config_hash(config_files: list[Path]) -> str:
    """所有相關 config 檔案的 sha256 合併。"""
    h = hashlib.sha256()
    for path in sorted(config_files):  # 排序確保 deterministic
        h.update(path.read_bytes())
    return h.hexdigest()[:16]
```

config_hash 寫入 score_snapshot → 改 config 後重跑會看到不同 hash，trace 清楚。

---

## 6. TTL 策略

| 物件類型 | TTL | 過期處理 |
|---|---|---|
| MacroScore | 24h | 過期後 Phase 3B 觸發 refresh；Phase 3A 階段可手動觸發 |
| IndustryScore | 24h | 同上 |
| CompanyScore | 168h (7天) | 同上 |
| NormalizedSignal | 依 signal_type（見 `decay.yaml`） | 過期 weight=0，但仍可查 raw data |
| GraphNode / GraphEdge | 永久 | 不刪除（append-only） |

---

## 7. 升級到 NetworkX / Neo4j 的路徑

`GraphStore` 是 ABC；`SQLiteGraphStore` 是 Phase 3A 實作；`NetworkXGraphStore` 與 `Neo4jGraphStore` 是未來實作。**介面不變**，升級 = 改一行 import。

升級時機（trigger）：
- graph_nodes > 10k
- BFS 查詢 P95 > 500ms
- 需要跨節點 graph algorithm（PageRank、社群偵測）

---

## 8. Schema 變更的紀律

1. **新欄位必 nullable** 或有 `default`。永遠不 drop 欄位。
2. **欄位 rename** 用 `ALTER TABLE ... RENAME COLUMN`，不在 application 層 mapping。
3. **breaking change** 必升 `schema_version`，加 `SchemaMigrator`。
4. **每個 table** 必寫 `tests/test_schema_<table>.py` 驗證新增 row / 查詢 / 索引覆蓋率。

---

## 9. 與 Phase 2B framework 的介面契約

Phase 3 **不 import** Phase 2B framework 的任何內部 class。但遵守以下契約：

- Phase 2B 的 `BaseReport.fetch()` 產出可以餵給 Phase 3 的 `SourceAdapter.adapt()`。契約是：return value 必為 `dict`（或 `list[dict]`）。
- Phase 3 的 `Scorer.score(context)` 是純函式，return `ScoreResult`。BaseReport.analyze() 拿到 ScoreResult 後丟給 Renderer。

---

## 10. 風險與緩解

| 風險 | 緩解 |
|---|---|
| JSON 序列化 breaking change | schema_version 欄位 + SchemaMigrator |
| intelligence.db 與 macro_history.db 不一致 | 兩者獨立檔案；cross-reference 用 entity_id，不做 FK |
| Graph 升級 NetworkX 時 unit test 全紅 | GraphStore ABC + 雙實作共用同一份 test 套 |
| score_id collision | SHA256 前 16 字元 + entity_id 範圍限制，collision 機率 < 1e-10 |
| config 改動追溯困難 | config_hash 必寫入每個 score；CLI 提供 `score trace --config-diff` |

---

_對應模組：`phase3/datamodel/`（schema 定義）、`phase3/storage/`（SQLite + GraphStore 實作）_
