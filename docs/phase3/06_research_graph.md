# Phase 3 — Research Graph

狀態：DESIGN ONLY
目標：把「Company / Industry / Macro / News / Signal / Report / Person」連結成一張可遍歷的證據圖，每個分數都能追到「最上游的數據與人」。

---

## 0. 為什麼需要 Graph

前三個 scorer（Macro / Industry / Company）產出的 `Score` 都是附 `evidence: list[EvidenceItem]`，但 evidence 是**樹狀的**（一個 score → 多個 dimension → 多個 sub_indicator → 多個 evidence），不是**圖狀的**。

實際投資推理需要圖：

- 「台積電的 valuation 偏貴」 → 追到 PE 32.8 → 追到 yfinance 2330.TW → 追到 yahoo finance API。
- 「半導體走強」 → 追到 rotation +20 → 追到產業 ETF 5 日 +3% → 追到 00830 走勢 → 追到 yahoo API。
- 「Fed 升息影響台股」 → Macro monetary −15 → 半導體 macro_sensitivity ×0.2 → 台積電 macro 調整 −3。
- 「張忠謀退休影響台積電」 → Person(張忠謀) --[曾任 CEO]--> Company(2330) → News(2018) → Signal(governance_risk)。

沒有 graph，evidence 就是斷裂的 list；有了 graph，可以**問出**「鼎鼎最在意的某個判斷，從數據出發可不可以重現？」

---

## 1. 節點類型 (Node Types)

| Node Type | 描述 | 識別碼 | 必填欄位 |
|---|---|---|---|
| `Company` | 一家上市公司 | `code` (e.g. "2330") | name, sector, market |
| `Industry` | 一個產業分類 | `id` (e.g. "AI") | name, keywords |
| `MacroFactor` | 一個總體經濟因子 | `id` (e.g. "FED_RATE") | name, category |
| `News` | 一則新聞 | `id` (hash) | title, source, pub_date, url |
| `Signal` | 一個 signal engine 產出的 NormalizedSignal | `signal_id` | entity_ref, signal_type, value, timestamp |
| `Score` | 一個 scorer 產出的 ScoreResult | `score_id` | entity_ref, score, confidence, timestamp |
| `Report` | 一份 Phase 2B 跑的 report 結果 | `task_id` | report_type, generated_at |
| `Person` | 一個關鍵人物（CEO/董事長/分析師/政府官員） | `id` | name, role, company_ref? |
| `Source` | 一個資料來源 | `id` | name, type, url |

每個 node 都有：
- `node_id: str` (unique)
- `node_type: NodeType`
- `created_at: datetime`
- `metadata: dict` (額外自由欄位)
- `tags: list[str]` (可搜尋)

---

## 2. Edge 類型 (Edge Types)

| Edge Type | From → To | 語意 | 必填欄位 |
|---|---|---|---|
| `MEMBER_OF` | Company → Industry | 個股所屬產業 | weight (0~1) |
| `BELONGS_TO` | Industry → MacroFactor | 產業對某 macro 因子敏感 | beta (float) |
| `EXPOSED_TO` | Company → MacroFactor | 公司直接暴露在某 macro 因子 | beta (float) |
| `GENERATED` | Signal → Source | 該信號從哪個 source 抓的 | - |
| `REFERS_TO` | News → Company/Industry/MacroFactor/Person | 新聞主題 | relevance (0~1) |
| `CONTRIBUTES_TO` | Signal → Score | 該信號貢獻到某個 score | weight, contribution |
| `INFLUENCES` | Score → Score | 跨層調整（macro→industry→company） | adjustment |
| `INCLUDES` | Report → Score | 該 report 包含此 score | - |
| `AUTHORED_BY` | Report → Person | 報告由某人撰寫 | - |
| `WORKS_AT` | Person → Company | 人在公司任職 | role, period |
| `CITES` | Score/Report → Source | 引用了某個 source | - |
| `DERIVED_FROM` | Score → Score | 衍生關係（公司 score 來自 industry+macro） | - |

每個 edge 都有：
- `edge_id: str`
- `edge_type: EdgeType`
- `from_node_id: str`
- `to_node_id: str`
- `weight: float` (optional, 0~1)
- `metadata: dict`
- `created_at: datetime`

---

## 3. 完整 Node/Edge Schema

```python
from enum import Enum

class NodeType(str, Enum):
    COMPANY = "company"
    INDUSTRY = "industry"
    MACRO_FACTOR = "macro_factor"
    NEWS = "news"
    SIGNAL = "signal"
    SCORE = "score"
    REPORT = "report"
    PERSON = "person"
    SOURCE = "source"


class EdgeType(str, Enum):
    MEMBER_OF = "member_of"
    BELONGS_TO = "belongs_to"
    EXPOSED_TO = "exposed_to"
    GENERATED = "generated"
    REFERS_TO = "refers_to"
    CONTRIBUTES_TO = "contributes_to"
    INFLUENCES = "influences"
    INCLUDES = "includes"
    AUTHORED_BY = "authored_by"
    WORKS_AT = "works_at"
    CITES = "cites"
    DERIVED_FROM = "derived_from"


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: NodeType
    label: str                            # 顯示用
    created_at: datetime
    metadata: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    edge_type: EdgeType
    from_node_id: str
    to_node_id: str
    weight: float | None = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

---

## 4. Graph Store 介面

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
    def bfs(self, start: str, max_depth: int = 3) -> list[GraphNode]: ...

    @abstractmethod
    def shortest_path(self, from_id: str, to_id: str) -> list[GraphEdge] | None: ...

    @abstractmethod
    def query(self, cypher_or_sql: str) -> list[dict]: ...
```

**Phase 3A 預設實作**：SQLite-backed（`intelligence.db` 內一個 `graph_nodes` 與 `graph_edges` 表）。**Phase 3B+ 可升級**：NetworkX（in-memory）或 Neo4j（外部）。

### 4.1 SQLite Schema
```sql
CREATE TABLE graph_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,    -- ISO 8601
    metadata_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]'
);

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

CREATE INDEX idx_edges_from ON graph_edges(from_node_id, edge_type);
CREATE INDEX idx_edges_to ON graph_edges(to_node_id, edge_type);
CREATE INDEX idx_nodes_type ON graph_nodes(node_type);
```

### 4.2 NetworkX 升級路徑
Phase 3B 之後，當 graph 超過 10k nodes / 50k edges 時，SQLite BFS 會慢（每跳都是 SQL）。NetworkX in-memory 是 zero-rewrite 升級路徑（介面相同、實作換掉）。

---

## 5. 主要 API 與使用情境

### 5.1 Evidence Tracing（核心）
**情境**：鼎鼎說「為什麼台積電是 +45？」
```python
chain = graph.trace_evidence(
    start_node="score:company:2330:2026-07-08",
    direction="upstream",  # 從 score 往上追到 source
    max_depth=10,
)
# 輸出：
# score:company:2330
#   ├── dimension:valuation (score=+30, weight=0.15)
#   │     ├── sub:pe_ratio (raw=32.8, sub_score=-30)
#   │     │     └── signal:yfinance.2330.TW (timestamp=2026-07-08)
#   │     │           └── source:yfinance
#   │     ├── sub:pb_ratio (raw=10.6, sub_score=-40)
#   │     ...
#   ├── dimension:financial_quality (score=+60, weight=0.20)
#   │     ├── sub:roe (raw=0.362, sub_score=+60)
#   │     │     └── signal:yfinance.2330.TW
#   │     ...
#   ├── macro_adjustment: +8
#   │     └── score:macro:2026-07-08 (liquidity_dim=+35 × 0.05 = +1.75 ...)
#   └── industry_adjustment: +5
#         └── score:industry:AI:2026-07-08 (score=+45 > 30)
```

### 5.2 BFS（廣度優先）
**情境**：找出所有跟「半導體」相關的 news
```python
news = graph.bfs(
    start="industry:半導體",
    edge_types=[EdgeType.REFERS_TO],
    direction="in",  # 找出指向 industry 的 news
    max_depth=2,
)
# 取出所有 News nodes
```

### 5.3 Shortest Path
**情境**：「半導體」到「FED_RATE」怎麼連？
```python
path = graph.shortest_path("industry:半導體", "macro:FED_RATE")
# → [MEMBER_OF: 2330→半導體, EXPOSED_TO: 2330→FED_RATE]
```

### 5.4 Subgraph
**情境**：匯出某一支公司的完整子圖
```python
sub = graph.subgraph(center="company:2330", hops=3, format="graphviz")
# → 產出 .dot 檔，可丟到 graphviz 渲染
```

---

## 6. 建圖流程（Population）

Phase 3A 的圖是**由 scorer 自動填入的**，不是手動建：

```
Signal Engine 產出 NormalizedSignal
   ↓ add_node(Signal) + add_edge(Signal→Source, GENERATED)
Scorer 產出 ScoreResult
   ↓ add_node(Score) + add_edge(Signal→Score, CONTRIBUTES_TO) + add_edge(Score→Score, INFLUENCES)
Industry / Macro / Company 互相調整
   ↓ add_edge(Score→Score, DERIVED_FROM)
RSS news 抓進來
   ↓ add_node(News) + add_edge(News→Company/Industry/Person, REFERS_TO)
Report 完成
   ↓ add_node(Report) + add_edge(Report→Score, INCLUDES)
```

### 6.1 自動偵測關聯
- News 標題含「台積電」/「2330」/「TSMC」→ REFERS_TO company:2330
- News 標題含「AI」/「半導體」→ REFERS_TO industry:AI/半導體
- News 標題含「Fed」「升息」「降息」→ REFERS_TO macro_factor:FED_RATE
- News 含人名 → REFERS_TO person (從內建 person index 查)

人名 index 在 `config/phase3/people.yaml`：
```yaml
people:
  - id: "person:morris_chang"
    name: "張忠謀"
    aliases: ["Moris Chang", "Morris Chang", "TSMC創辦人"]
    works_at: [{ company: "company:2330", role: "Founder", period: "1987-2018" }]
  - id: "person:c.c.wei"
    name: "魏哲家"
    aliases: ["C.C. Wei", "魏哲家"]
    works_at: [{ company: "company:2330", role: "CEO", period: "2018-present" }]
  # ...
```

### 6.2 Idempotent
add_node / add_edge 用 `node_id` / `edge_id` PK，重複呼叫 = UPSERT。Phase 3A 的 cron job 跑 7 天也不會建立重複節點。

---

## 7. Evidence Tracing 範例輸出

```
🔍 **台積電 (2330) 綜合分數: +45 / 100** （信心 0.82）

📊 **財務品質** +60 (w=0.20)
  • ROE 36.2% → +60 [yfinance, 2026-07-08]
  • ROA 22.1% → +50 [yfinance, 2026-07-08]
  • D/E 0.18 → +40 [yfinance, 2026-07-08]

📈 **成長** +50 (w=0.15)
  • 營收 YoY +35% → +60 [yfinance, 2026-07-08]
  • EPS YoY +58% → +60 [yfinance, 2026-07-08]

💰 **估值** -30 (w=0.15)
  • PE 32.8 → -30 [yfinance, 2026-07-08]
  • PB 10.6 → -40 [yfinance, 2026-07-08]

🌐 **跨層調整**
  • Macro 影響: +8 (liquidity +35 × 0.05 = +1.75; rates +40 × 0.02 = +0.8; ...)
  • Industry 影響: +5 (AI 產業 +45 > 30)

🔗 **證據鏈** (10 個 source 引用)
  • yfinance:2330.TW (5 個指標)
  • t86:2330 (2 個信號)
  • rss:digitimes (1 篇新聞)
  • rss:cnyes (1 篇新聞)
  • score:macro:2026-07-08 (1 個 cross-layer)
  • score:industry:AI:2026-07-08 (1 個 cross-layer)

🎯 **判斷**: 基本面極優但估值偏貴，產業與總體環境支撐，持有偏多
```

每一行後面 `[yfinance, 2026-07-08]` 都是 `EvidenceItem`，可以從該分數走 graph 回到 source。

---

## 8. CLI / API 介面

```python
from phase3.graph import GraphStore, EvidenceTracer

store = GraphStore("/home/ubuntu/macro-report/intelligence.db")
tracer = EvidenceTracer(store)

# 證據追溯
chain = tracer.trace(score_id="score:company:2330:2026-07-08", max_depth=10)
print(chain.to_tree_string())

# 子圖匯出
dot = store.subgraph_to_dot(center="company:2330", hops=3, output="/tmp/2330.dot")

# 查詢
nodes = store.query("SELECT * FROM graph_nodes WHERE node_type = 'news' AND created_at > '2026-07-01'")
```

CLI：
```bash
python3 /home/ubuntu/macro-report/phase3/cli.py graph trace --score score:company:2330:2026-07-08
python3 /home/ubuntu/macro-report/phase3/cli.py graph export-dot --center company:2330 --hops 3 --out /tmp/2330.dot
python3 /home/ubuntu/macro-report/phase3/cli.py graph stats  # node count by type
```

---

## 9. 單元測試要點

- add_node / add_edge 對同一個 ID 重複呼叫要 UPSERT，不可拋錯。
- BFS 跨層時不漏 edge（測一個 4 層鏈，必須走完 4 hop）。
- trace_evidence 從 score 走回 source，路徑長度合理（< 10 hop）。
- shortest_path 在不連通時回 None，不可當錯。
- subgraph 對大型中心（hub 節點）要有 max_nodes 上限，避免爆量。
- 升級測試：同一介面在 SQLite 與 NetworkX 實作下產出相同結果。

---

## 10. 風險與緩解

| 風險 | 緩解 |
|---|---|
| Graph 節點爆炸（每次 fetch 都建一個 signal node） | signal 用 `signal_id = hash(source + entity + signal_type + date)`，同日重複 = UPSERT |
| 跨層追溯效能差 | depth 預設上限 10；subgraph 用 max_nodes 限制 |
| SQLite 全文搜尋不足 | metadata_json 內放關鍵字，搭配 FTS5 虛擬表（Phase 3B+） |
| 升級 NetworkX 時 schema 漂移 | GraphStore 介面是 ABC，兩個實作共用同一組 unit test |
| 隱私：人名/敏感資料 | 個人 node 預設 opt-in；config 控制是否寫入 |

---

_對應模組：`phase3/graph/`（in 10_repo_structure.md）_
