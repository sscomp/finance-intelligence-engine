# Phase 3 — API Specification

狀態：DESIGN ONLY
目標：Phase 3 對外暴露的所有介面（Python API / CLI / JSON-RPC）。**所有介面都遵循「pure input → pure output」原則**，不藏 side effect。

---

## 0. 介面分層

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: Python API (for code embedding)                     │
│   from phase3 import ScorerEngine, GraphStore                │
├─────────────────────────────────────────────────────────────┤
│ Layer 2: CLI (for ops & debugging)                          │
│   python3 -m phase3 score macro --date 2026-07-08            │
├─────────────────────────────────────────────────────────────┤
│ Layer 3: JSON-RPC over HTTPS (for GPT Orchestrator, Phase 4) │
│   POST /v1/phase3/score/macro  {date: "2026-07-08"}          │
└─────────────────────────────────────────────────────────────┘
```

**Phase 3A 範圍**：Layer 1 + Layer 2。
**Phase 3B 範圍**：Layer 1 + Layer 2 + Layer 3（HTTP 端點）。

---

## 1. Layer 1 — Python API

### 1.1 入口物件

```python
from phase3 import (
    SignalEngine,
    MacroScorer,
    IndustryScorer,
    CompanyScorer,
    GraphStore,
    EvidenceTracer,
    ConfigLoader,
    ScoreResult, MacroScore, IndustryScore, CompanyScore,
    MacroContext, IndustryContext, CompanyContext,
    RawSignal, NormalizedSignal, WeightedSignal, AggregatedSignal,
    EvidenceItem, GraphNode, GraphEdge,
)
```

### 1.2 Signal Engine

```python
class SignalEngine:
    def __init__(self, config: SignalConfig): ...

    def adapt(self, raw: RawSignal) -> list[NormalizedSignal]:
        """把 RawSignal 透過對應 SourceAdapter 轉成 NormalizedSignal list。"""

    def process(self, raw: RawSignal) -> list[WeightedSignal]:
        """adapt + weight + decay + confidence → WeightedSignal list。"""

    def aggregate(
        self,
        signals: list[WeightedSignal],
        entity_type: str,
        entity_id: str,
        signal_type: str,
    ) -> AggregatedSignal:
        """多源合併。"""
```

### 1.3 Macro Scorer

```python
class MacroScorer:
    def __init__(self, config: MacroConfig, signal_engine: SignalEngine): ...

    def score(self, signals: list[WeightedSignal], as_of: datetime | None = None) -> MacroScore:
        """Pure function: signals + as_of → MacroScore。"""

    def score_with_context(self, context: ScoringContext) -> MacroScore:
        """Variant: 從 ScoringContext 取得 signals（for chain scoring）。"""
```

### 1.4 Industry Scorer

```python
class IndustryScorer:
    def __init__(
        self,
        config: IndustryConfig,
        signal_engine: SignalEngine,
        macro_context: MacroContext | None = None,
    ): ...

    def score(
        self,
        industry_id: str,
        signals: list[WeightedSignal],
        as_of: datetime | None = None,
    ) -> IndustryScore: ...

    def score_all(
        self,
        signals_by_industry: dict[str, list[WeightedSignal]],
    ) -> list[IndustryScore]: ...
```

### 1.5 Company Scorer

```python
class CompanyScorer:
    def __init__(
        self,
        config: CompanyConfig,
        signal_engine: SignalEngine,
        macro_context: MacroContext | None = None,
        industry_contexts: dict[str, IndustryContext] | None = None,
    ): ...

    def score(
        self,
        code: str,
        signals: list[WeightedSignal],
        as_of: datetime | None = None,
    ) -> CompanyScore: ...

    def score_batch(
        self,
        codes: list[str],
        signals_by_code: dict[str, list[WeightedSignal]],
    ) -> list[CompanyScore]: ...
```

### 1.6 Graph Store

```python
class GraphStore(ABC):
    @abstractmethod
    def add_node(self, node: GraphNode) -> None: ...

    @abstractmethod
    def add_edge(self, edge: GraphEdge) -> None: ...

    @abstractmethod
    def get_node(self, node_id: str) -> GraphNode | None: ...

    @abstractmethod
    def get_neighbors(
        self,
        node_id: str,
        edge_types: list[EdgeType] | None = None,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[tuple[GraphNode, GraphEdge]]: ...

    @abstractmethod
    def bfs(
        self,
        start: str,
        max_depth: int = 3,
        edge_types: list[EdgeType] | None = None,
    ) -> list[GraphNode]: ...

    @abstractmethod
    def shortest_path(
        self,
        from_id: str,
        to_id: str,
    ) -> list[GraphEdge] | None: ...

    @abstractmethod
    def subgraph(
        self,
        center: str,
        hops: int = 2,
        max_nodes: int = 500,
        format: Literal["networkx", "graphviz", "json"] = "json",
    ) -> str | dict: ...

    @abstractmethod
    def query(self, sql: str, params: tuple = ()) -> list[dict]: ...
```

### 1.7 Evidence Tracer

```python
class EvidenceTracer:
    def __init__(self, graph: GraphStore): ...

    def trace(
        self,
        score_id: str,
        max_depth: int = 10,
        output_format: Literal["tree", "flat", "json"] = "tree",
    ) -> EvidenceChain:
        """從 score_id 向上游追溯到 source。"""

    def explain_score(self, score_id: str) -> str:
        """中文一句話總結這個 score 的 evidence。"""
```

### 1.8 Config Loader

```python
class ConfigLoader:
    @staticmethod
    def load(config_dir: Path) -> Phase3Config: ...

    @staticmethod
    def load_scorer(scorer_name: str, config_dir: Path) -> ScorerManifest: ...

    @staticmethod
    def compute_hash(config_files: list[Path]) -> str: ...
```

### 1.9 完整使用範例

```python
from pathlib import Path
from datetime import datetime
from phase3 import (
    ConfigLoader, SignalEngine, MacroScorer, IndustryScorer, CompanyScorer,
    GraphStore, EvidenceTracer, RawSignal,
)

# 1. Load config
config = ConfigLoader.load(Path("/home/ubuntu/macro-report/config/phase3/"))

# 2. Set up signal engine
engine = SignalEngine(config.signal)

# 3. Fetch raw signals (from Phase 2B BaseReport.fetch() or direct yfinance)
raw = RawSignal(
    source_id="yfinance.2330.TW",
    source_type="yfinance",
    raw_data={"trailingPE": 32.8, "returnOnEquity": 0.362},
    fetched_at=datetime.utcnow(),
)
weighted = engine.process(raw)

# 4. Score macro (chain start)
macro_scorer = MacroScorer(config.macro, engine)
macro_score = macro_scorer.score(signals=[])
macro_context = macro_score.to_macro_context()

# 5. Score industry (depends on macro)
industry_scorer = IndustryScorer(config.industry, engine, macro_context)
industry_score = industry_scorer.score(industry_id="半導體", signals=[])
industry_context = industry_score.to_industry_context()

# 6. Score company (depends on macro + industry)
company_scorer = CompanyScorer(
    config.company, engine, macro_context,
    industry_contexts={"半導體": industry_context},
)
company_score = company_scorer.score(code="2330", signals=weighted)

# 7. Build graph
graph = GraphStore.sqlite(Path("/home/ubuntu/macro-report/intelligence.db"))
# ... add_node / add_edge for signals, scores, cross-layer adjustments

# 8. Trace evidence
tracer = EvidenceTracer(graph)
chain = tracer.trace(company_score.score_id)
print(chain.to_tree_string())
```

---

## 2. Layer 2 — CLI

### 2.1 入口
```bash
python3 -m phase3 [command] [subcommand] [options]
# 或
python3 /home/ubuntu/macro-report/phase3/cli.py [command] [subcommand] [options]
```

### 2.2 Command Tree

```
phase3
├── signal
│   ├── process      # RawSignal → WeightedSignal
│   ├── aggregate    # 多源合併
│   └── inspect      # 查 signal_log
├── score
│   ├── macro        # 跑 macro scorer
│   ├── industry     # 跑 industry scorer
│   ├── company      # 跑 company scorer
│   ├── all          # 全部依序跑（macro → industry → company）
│   └── inspect      # 查 score_snapshot
├── graph
│   ├── trace        # 證據追溯
│   ├── export-dot   # 匯出 graphviz
│   ├── stats        # 統計 node / edge 數量
│   └── query        # 跑 SQL 查 graph_nodes / graph_edges
├── config
│   ├── validate     # 驗 config 是否合法
│   ├── hash         # 算 config_hash
│   └── show         # 顯示當前 merged config
├── explain
│   └── score        # 一句話解釋某個 score
├── backtest
│   └── run          # 用歷史 raw data 跑某個 config，與既有分數 diff
└── version
```

### 2.3 命令細節

#### `phase3 signal process`
```bash
python3 -m phase3 signal process \
  --source yfinance \
  --entity 2330 \
  --date 2026-07-08 \
  --out /tmp/signals.json
```
輸出：`/tmp/signals.json` 內含 `list[WeightedSignal]`

#### `phase3 score macro`
```bash
python3 -m phase3 score macro \
  --date 2026-07-08 \
  --no-persist          # 不寫 SQLite
  --format json          # 輸出格式
```
輸出：MacroScore JSON

#### `phase3 score all`
```bash
python3 -m phase3 score all \
  --date 2026-07-08 \
  --industries AI 半導體 金融 \
  --companies 2330 2382 2881 \
  --persist
```
輸出：依序跑 macro → industry → company，結果寫 SQLite

#### `phase3 graph trace`
```bash
python3 -m phase3 graph trace \
  --score score:company:2330:2026-07-08 \
  --max-depth 10 \
  --format tree
```
輸出：ASCII tree 顯示 evidence chain

#### `phase3 backtest run`
```bash
python3 -m phase3 backtest run \
  --config /tmp/new_company_weights.yaml \
  --start 2026-06-01 \
  --end 2026-07-08 \
  --diff-threshold 30 \
  --out /tmp/backtest_diff.json
```
輸出：每個 score 的 old vs new 對比，超過 diff_threshold 的維度列入

### 2.4 全域 Flag
```bash
--config-dir <path>     # config 路徑（預設 /home/ubuntu/macro-report/config/phase3/）
--db-path <path>        # intelligence.db 路徑
--date <YYYY-MM-DD>     # 模擬「當天」（預設 today）
--no-persist            # 不寫 SQLite
--format json|tree|text # 輸出格式
--verbose               # 詳細 log
--quiet                 # 抑制非錯誤輸出
```

### 2.5 Exit Codes
| Code | 意思 |
|---|---|
| 0 | 成功 |
| 1 | 通用錯誤 |
| 2 | config 錯誤（validate 失敗） |
| 3 | 資料錯誤（缺必要 input） |
| 4 | DB 錯誤（schema mismatch / disk full） |
| 5 | 計算錯誤（scorer 拋 exception） |

---

## 3. Layer 3 — JSON-RPC over HTTPS（Phase 3B+）

### 3.1 端點

Base URL：`https://phase3.biaobecue.com`（透過 Cloudflare Tunnel，與現有 HTTPS 服務一致）

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/v1/phase3/score/macro` | 跑 macro score |
| `POST` | `/v1/phase3/score/industry` | 跑 industry score |
| `POST` | `/v1/phase3/score/company` | 跑 company score |
| `POST` | `/v1/phase3/score/all` | 跑全部（macro → industry → company） |
| `GET`  | `/v1/phase3/score/{score_id}` | 查 score |
| `GET`  | `/v1/phase3/evidence/{score_id}` | 查 evidence chain |
| `GET`  | `/v1/phase3/graph/{entity_id}` | 查子圖 |
| `GET`  | `/v1/phase3/health` | 健康檢查 |

### 3.2 認證
- Bearer token（與現有 Hermes Bridge 一致）
- Header: `Authorization: Bearer <PHASE3_API_KEY>`
- API key 在 `.env` 內 `PHASE3_API_KEY=*** 產生

### 3.3 Request / Response 範例

#### `POST /v1/phase3/score/company`
```json
{
  "code": "2330",
  "as_of": "2026-07-08T00:00:00Z",
  "include_evidence": true,
  "max_evidence": 10
}
```

Response:
```json
{
  "score": {
    "score_id": "score:company:2330:2026-07-08",
    "code": "2330",
    "name": "台積電",
    "sector": "半導體",
    "score": 45.0,
    "raw_score": 32.0,
    "macro_adjustment": 8.0,
    "confidence": 0.82,
    "dimensions": [
      {
        "name": "financial_quality",
        "score": 60.0,
        "weight": 0.20,
        "sub_indicators": [
          {
            "name": "roe",
            "raw_value": 0.362,
            "raw_unit": "ratio",
            "sub_score": 60.0,
            "transformation": "threshold",
            "source": "yfinance"
          }
        ]
      }
    ],
    "macro_context_hash": "abc123",
    "industry_context_hash": "def456",
    "config_hash": "ghi789",
    "timestamp": "2026-07-08T00:30:00Z",
    "valid_until": "2026-07-15T00:30:00Z"
  },
  "evidence": [
    {
      "evidence_id": "ev:001",
      "source_type": "yfinance",
      "source_ref": "https://query1.finance.yahoo.com/v10/finance/quoteSummary/2330.TW",
      "raw_value": "0.362",
      "description": "ROE 36.2% (yfinance .info.returnOnEquity)",
      "timestamp": "2026-07-08T00:00:00Z",
      "weight": 0.18
    }
  ]
}
```

#### Error Response
```json
{
  "error": {
    "code": "SCORE_NOT_FOUND",
    "message": "Score score:company:2330:2026-06-01 not found or expired",
    "details": {
      "score_id": "score:company:2330:2026-06-01",
      "valid_until": "2026-06-08T00:00:00Z"
    }
  }
}
```

### 3.4 限流
- 60 requests / minute / IP
- 1000 requests / day / API key
- 429 Too Many Requests 含 `Retry-After` header

### 3.5 Idempotency
- 每個 `POST /v1/phase3/score/*` 必含 `Idempotency-Key: <uuid>` header
- 同 key + 1 小時內重複請求，回傳同樣 response（從 SQLite cache）
- 不同 key 視為新請求

---

## 4. 錯誤處理約定

### 4.1 錯誤碼（跨所有介面）
| Code | 意思 | HTTP | Python Exception |
|---|---|---|---|
| `CONFIG_INVALID` | config 驗證失敗 | 400 | `Phase3ConfigError` |
| `CONFIG_NOT_FOUND` | config 檔案不存在 | 404 | `Phase3ConfigError` |
| `SCORE_NOT_FOUND` | score_id 不存在或過期 | 404 | `ScoreNotFoundError` |
| `ENTITY_NOT_FOUND` | entity 不存在 | 404 | `EntityNotFoundError` |
| `DATA_INCOMPLETE` | 必要 input 缺漏 | 422 | `DataIncompleteError` |
| `SIGNAL_EXPIRED` | signal TTL 過期且無法 refresh | 410 | `SignalExpiredError` |
| `CROSS_LAYER_MISSING` | 跨層依賴未滿足（如 industry 沒 macro） | 412 | `CrossLayerMissingError` |
| `INTERNAL` | 內部錯誤（scorer bug） | 500 | `Phase3InternalError` |
| `RATE_LIMITED` | 超過 rate limit | 429 | `RateLimitedError` |

### 4.2 錯誤回應格式（JSON-RPC）
```json
{
  "error": {
    "code": "SCORE_NOT_FOUND",
    "message": "...",
    "details": {...},
    "trace_id": "uuid-for-debug",
    "timestamp": "ISO 8601"
  }
}
```

---

## 5. 版本與相容性

### 5.1 API Versioning
- URL path versioning：`/v1/`, `/v2/`
- 破壞性變更必升 major version
- 新欄位（additive）可在 minor version 加，不破壞 client
- response 內必含 `"api_version": "3.0.0"`

### 5.2 6 個月 Compatibility Window
- v1.0 endpoint 至少存活 6 個月
- deprecation 公告會在 response header `X-Deprecation-Notice` 內

---

## 6. OpenAPI 規格

Phase 3B 會產出完整 `openapi.yaml`（OpenAPI 3.1）放在：
`/home/ubuntu/macro-report/docs/phase3/openapi.yaml`

供 ChatGPT Custom GPT Action 直接 import。

---

## 7. 範例：給 GPT Orchestrator 的合約

GPT 端可以這樣呼叫（pseudo code）：

```python
import httpx

async def ask_phase3(question: str) -> str:
    """鼎鼎問『台積電現在怎麼樣？』→ 給 GPT 看 score + evidence。"""
    async with httpx.AsyncClient() as client:
        # 1. 拿當前 company score
        r = await client.post(
            "https://phase3.biaobecue.com/v1/phase3/score/company",
            headers={"Authorization": f"Bearer {PHASE3_API_KEY}"},
            json={"code": "2330", "include_evidence": True, "max_evidence": 5},
        )
        r.raise_for_status()
        score_data = r.json()["score"]
        evidence = r.json()["evidence"]

        # 2. 組成 GPT prompt
        prompt = f"""
        使用者問題: {question}

        台積電 (2330) 綜合分數: {score_data['score']}/100
        各維度: {format_dimensions(score_data['dimensions'])}
        跨層調整: macro {score_data['macro_adjustment']}

        證據:
        {format_evidence(evidence)}

        請用繁體中文回答使用者的問題，並引用上述證據。
        不要引入新的事實，只摘要提供的 evidence。
        """
        return await ask_gpt(prompt)
```

---

## 8. 風險

| 風險 | 緩解 |
|---|---|
| API endpoint 暴露導致未授權存取 | Bearer token + rate limit + audit log |
| Idempotency-Key 衝突導致 stale response | 1 小時 TTL 過期重算 |
| OpenAPI 規格與實作不同步 | 規格由 unit test 驗證（contract test） |
| 錯誤碼 enum 漂移 | 集中 enum + schema 驗證 |

---

_對應實作：`phase3/api/`（Python）、`phase3/cli.py`（CLI）、`phase3/server/`（JSON-RPC，Phase 3B+）_
