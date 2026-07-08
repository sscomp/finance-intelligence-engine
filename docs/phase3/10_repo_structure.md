# Phase 3 — Repository Structure

狀態：DESIGN ONLY
目標：完整目錄樹、所有檔案清單、import 邊界。

---

## 0. 設計原則

1. **Phase 3 是獨立的子套件**，不 import Phase 2B framework 內部 class。
2. **Phase 3 自己一套 datamodel / config / storage**，但與 macro-report 共用 venv。
3. **新增的所有東西在 `phase3/` 子樹下**，不污染 `/home/ubuntu/macro-report/` 根目錄。
4. **每個 module 都有對應的 test**，新模組無 test = fail CI。
5. **`intelligence.db` 獨立檔案**，與 `macro_history.db` 完全分離。

---

## 1. 完整目錄樹

```
/home/ubuntu/macro-report/
│
├── (既有檔案，零修改)
│   ├── macro_daily.py
│   ├── company_monthly.py
│   ├── industry_weekly.py
│   ├── institutional.py
│   ├── db.py
│   ├── run.sh
│   ├── run_weekly.sh
│   ├── run_monthly.sh
│   ├── industry_config.json
│   ├── taiwan50_config.json
│   ├── macro_history.db
│   ├── run_report.py                 # Phase 2B CLI
│   └── reports/                      # Phase 2B framework
│       ├── base.py
│       ├── pipeline.py
│       ├── registry.py
│       ├── common/
│       ├── plugins/
│       └── hermes/
│
├── phase3/                           # ★ Phase 3 主目錄
│   ├── __init__.py                   # 公開 API surface
│   ├── cli.py                        # CLI 入口
│   │
│   ├── datamodel/                    # 所有 frozen dataclass
│   │   ├── __init__.py
│   │   ├── signals.py                # RawSignal, NormalizedSignal, WeightedSignal, AggregatedSignal
│   │   ├── evidence.py               # EvidenceItem
│   │   ├── scores.py                 # ScoreResult, DimensionResult, SubIndicatorResult
│   │   ├── macro.py                  # MacroScore
│   │   ├── industry.py               # IndustryScore
│   │   ├── company.py                # CompanyScore
│   │   ├── context.py                # MacroContext, IndustryContext, CompanyContext
│   │   ├── graph.py                  # GraphNode, GraphEdge, NodeType, EdgeType
│   │   └── config.py                 # Phase3Config, ScorerManifest, SourceManifest
│   │
│   ├── signals/                      # Signal Engine
│   │   ├── __init__.py
│   │   ├── engine.py                 # SignalEngine (main)
│   │   ├── adapters/
│   │   │   ├── __init__.py
│   │   │   ├── base.py               # SourceAdapter ABC
│   │   │   ├── yfinance.py           # YFinanceSourceAdapter
│   │   │   ├── rss.py                # RSSSourceAdapter
│   │   │   ├── t86.py                # T86SourceAdapter
│   │   │   ├── macro.py              # MacroDailySourceAdapter
│   │   │   └── custom.py             # CustomSourceAdapter (template)
│   │   ├── decay.py                  # decay functions
│   │   ├── weighting.py              # source/type weight 計算
│   │   ├── confidence.py             # confidence 計算
│   │   ├── aggregator.py             # 多源聚合
│   │   └── people_matcher.py         # News → Person 偵測
│   │
│   ├── scoring/                      # Scorers
│   │   ├── __init__.py
│   │   ├── base.py                   # BaseScorer ABC
│   │   ├── macro.py                  # MacroScorer
│   │   ├── industry.py               # IndustryScorer
│   │   ├── company.py                # CompanyScorer
│   │   ├── dimensions/               # 維度函式
│   │   │   ├── __init__.py
│   │   │   ├── macro/
│   │   │   │   ├── economic.py
│   │   │   │   ├── monetary.py
│   │   │   │   ├── inflation.py
│   │   │   │   ├── rates.py
│   │   │   │   ├── liquidity.py
│   │   │   │   └── geopolitics.py
│   │   │   ├── industry/
│   │   │   │   ├── rotation.py
│   │   │   │   ├── relative_strength.py
│   │   │   │   ├── cyclicality.py
│   │   │   │   ├── macro_sensitivity.py
│   │   │   │   ├── industry_news.py
│   │   │   │   └── capital_flow.py
│   │   │   └── company/
│   │   │       ├── financial_quality.py
│   │   │       ├── growth.py
│   │   │       ├── profitability.py
│   │   │       ├── valuation.py
│   │   │       ├── momentum.py
│   │   │       ├── risk.py
│   │   │       └── news_sentiment.py
│   │   ├── transformations/          # sub-score 變換
│   │   │   ├── __init__.py
│   │   │   ├── threshold.py
│   │   │   ├── z_score.py
│   │   │   ├── invert.py
│   │   │   ├── range_score.py
│   │   │   └── lookup.py
│   │   ├── cross_layer.py            # 跨層調整邏輯
│   │   └── explain.py                # explain() 中文短句生成（rule-based）
│   │
│   ├── graph/                        # Research Graph
│   │   ├── __init__.py
│   │   ├── store.py                  # GraphStore ABC
│   │   ├── sqlite_store.py           # SQLiteGraphStore
│   │   ├── networkx_store.py         # NetworkXGraphStore (Phase 3B+)
│   │   ├── evidence_tracer.py        # EvidenceTracer
│   │   ├── traversal.py              # BFS / DFS / shortest_path
│   │   ├── subgraph.py               # 子圖匯出
│   │   └── population.py             # 自動建圖（從 scorer 結果）
│   │
│   ├── storage/                      # 持久化
│   │   ├── __init__.py
│   │   ├── db.py                     # intelligence.db connection
│   │   ├── schema.py                 # table 定義
│   │   ├── migrations.py             # SchemaMigrator
│   │   ├── score_repo.py             # score_snapshot CRUD
│   │   ├── signal_repo.py            # signal_log CRUD
│   │   └── graph_repo.py             # graph_nodes/edges CRUD
│   │
│   ├── config/                       # 設定載入
│   │   ├── __init__.py
│   │   ├── loader.py                 # ConfigLoader
│   │   ├── validator.py              # 驗證 config 合法
│   │   └── hasher.py                 # config_hash 計算
│   │
│   ├── explain/                      # 文字解釋
│   │   ├── __init__.py
│   │   ├── rule_based.py             # 純規則中文短句
│   │   └── llm_optional.py           # LLM 強化（opt-in, Phase 3C）
│   │
│   ├── cli/                          # CLI 細分
│   │   ├── __init__.py
│   │   ├── main.py                   # 主入口
│   │   ├── signal.py                 # signal 子命令
│   │   ├── score.py                  # score 子命令
│   │   ├── graph.py                  # graph 子命令
│   │   ├── config.py                 # config 子命令
│   │   ├── explain.py                # explain 子命令
│   │   └── backtest.py               # backtest 子命令
│   │
│   └── server/                       # Phase 3B+ JSON-RPC server
│       ├── __init__.py
│       ├── app.py                    # FastAPI app
│       ├── routes.py                 # 端點
│       ├── auth.py                   # Bearer token 驗證
│       └── ratelimit.py              # rate limiting
│
├── config/phase3/                    # 設定檔（YAML）
│   ├── enabled.yaml                  # kill switch
│   ├── cross_layer_adjustment.yaml  # 跨層影響設定
│   ├── sector.yaml                   # 產業 ↔ 個股對照
│   ├── sources/                      # source manifests
│   │   ├── yfinance.yaml
│   │   ├── rss_digitimes.yaml
│   │   ├── rss_wealth.yaml
│   │   ├── rss_cnyes.yaml
│   │   ├── t86.yaml
│   │   └── macro_yfinance.yaml
│   ├── decay.yaml                    # decay 規則
│   ├── scorers/                      # scorer manifests
│   │   ├── macro.yaml
│   │   ├── industry.yaml
│   │   └── company.yaml
│   ├── weights/                      # weight 設定
│   │   ├── macro_weights.yaml
│   │   ├── industry_weights.yaml
│   │   ├── company_weights.yaml
│   │   └── sector_macro_sensitivity.yaml
│   ├── indicators/                   # indicator 規則
│   │   ├── macro_indicators.yaml
│   │   ├── industry_indicators.yaml
│   │   └── company_indicators.yaml
│   ├── industry_stocks.yaml          # 產業成分股
│   ├── people.yaml                   # 人物 index
│   └── thresholds/                   # 預設 threshold 表
│       ├── pe_thresholds.yaml
│       ├── roe_thresholds.yaml
│       └── ...
│
├── intelligence.db                  # SQLite（自動產生，第一個 Phase 3 跑時建）
│
├── tests/phase3/                     # Phase 3 測試
│   ├── __init__.py
│   ├── test_signal_engine.py
│   ├── test_source_adapters.py
│   ├── test_decay.py
│   ├── test_weighting.py
│   ├── test_confidence.py
│   ├── test_aggregator.py
│   ├── test_scorer_macro.py
│   ├── test_scorer_industry.py
│   ├── test_scorer_company.py
│   ├── test_dimensions/
│   │   ├── test_macro_dimensions.py
│   │   ├── test_industry_dimensions.py
│   │   └── test_company_dimensions.py
│   ├── test_transformations.py
│   ├── test_cross_layer.py
│   ├── test_graph_store.py
│   ├── test_evidence_tracer.py
│   ├── test_storage.py
│   ├── test_config_loader.py
│   ├── test_explain.py
│   ├── test_cli.py
│   └── test_backtest.py
│
└── docs/phase3/                      # 本設計文件
    ├── 01_architecture_overview.md
    ├── 02_signal_engine.md
    ├── 03_macro_scoring.md
    ├── 04_company_scoring.md
    ├── 05_industry_scoring.md
    ├── 06_research_graph.md
    ├── 07_data_model.md
    ├── 08_risks_assumptions.md
    ├── 09_api_spec.md
    ├── 10_repo_structure.md          # 本文件
    ├── 11_milestone_roadmap.md
    └── openapi.yaml                  # Phase 3B+ 產出
```

---

## 2. 模組 Import 邊界

```
┌──────────────────────────────────────────────────────────────┐
│                       phase3/api (Layer 1)                    │
│  公開介面: SignalEngine / Scorers / GraphStore / EvidenceTracer│
└──────┬───────────────────┬────────────────────┬──────────────┘
       │                   │                    │
       ▼                   ▼                    ▼
┌──────────────┐  ┌────────────────┐  ┌──────────────────┐
│ phase3/      │  │ phase3/        │  │ phase3/          │
│ scoring/     │  │ signals/       │  │ graph/           │
│              │  │                │  │                  │
│ 純函式       │  │ Pure functions │  │ GraphStore ABC   │
│ 無 I/O       │  │ + adapters     │  │ + SQLite impl     │
└──────┬───────┘  └────────┬───────┘  └────────┬─────────┘
       │                   │                    │
       └───────────────────┴────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ phase3/datamodel       │
              │ 全是 frozen dataclass   │
              │ 純資料，無邏輯          │
              └────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ phase3/storage          │
              │ SQLite I/O             │
              └────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ phase3/config           │
              │ 載入 + 驗證 + hash     │
              └────────────────────────┘

                  (絕對禁止的 import)
       ┌──────────────────────────────────────┐
       │ ❌ phase3 不可 import:                │
       │   - phase2b framework 內部 class       │
       │   - 現有 macro_daily.py 等 .py        │
       │   - 現有 run.sh 等 shell              │
       │   - 現有 db.py                        │
       │   - 現有 jobs.json / cron             │
       │                                      │
       │ ❌ phase3/datamodel 不可 import:     │
       │   - 任何 phase3/* 其他子模組          │
       │   （datamodel 必須是純資料層）         │
       │                                      │
       │ ❌ phase3/scoring/* 不可 import:     │
       │   - phase3/storage (無 I/O)           │
       │   - phase3/graph (無圖操作)           │
       │   （scoring 必須是純函式）             │
       └──────────────────────────────────────┘
```

### 2.1 允許的例外
- `phase3/cli/*` 與 `phase3/server/*` 可 import 所有 phase3 模組（它們是 facade layer）。
- `phase3/__init__.py` re-export 公開 API，方便 `from phase3 import X`。

---

## 3. 檔案清單（含估計規模）

### 3.1 Core（純函式 / 純資料）
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/__init__.py` | 50 | 公開 API re-export |
| `phase3/datamodel/signals.py` | 80 | 4 個 signal dataclass |
| `phase3/datamodel/evidence.py` | 40 | EvidenceItem |
| `phase3/datamodel/scores.py` | 150 | ScoreResult + 兩個子類型 |
| `phase3/datamodel/macro.py` | 40 | MacroScore |
| `phase3/datamodel/industry.py` | 50 | IndustryScore |
| `phase3/datamodel/company.py` | 60 | CompanyScore |
| `phase3/datamodel/context.py` | 100 | 3 個 Context + adjustment_factor |
| `phase3/datamodel/graph.py` | 80 | GraphNode + GraphEdge + 2 enum |
| `phase3/datamodel/config.py` | 100 | Phase3Config + 2 manifest |

### 3.2 Signal Engine
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/signals/engine.py` | 150 | SignalEngine main class |
| `phase3/signals/adapters/base.py` | 50 | SourceAdapter ABC |
| `phase3/signals/adapters/yfinance.py` | 250 | 抽出 ~20 個 yfinance 欄位 |
| `phase3/signals/adapters/rss.py` | 100 | RSS → sentiment |
| `phase3/signals/adapters/t86.py` | 100 | T86 → 法人買賣超 |
| `phase3/signals/adapters/macro.py` | 150 | 從 macro_daily.py 結果抽 macro signal |
| `phase3/signals/decay.py` | 80 | 4 種 decay function |
| `phase3/signals/weighting.py` | 80 | source/type weight 計算 |
| `phase3/signals/confidence.py` | 100 | 信心值計算 |
| `phase3/signals/aggregator.py` | 150 | 多源聚合 |
| `phase3/signals/people_matcher.py` | 80 | News → Person 偵測 |

### 3.3 Scoring
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/scoring/base.py` | 100 | BaseScorer ABC |
| `phase3/scoring/macro.py` | 200 | MacroScorer |
| `phase3/scoring/industry.py` | 250 | IndustryScorer |
| `phase3/scoring/company.py` | 300 | CompanyScorer |
| `phase3/scoring/cross_layer.py` | 150 | 跨層調整 |
| `phase3/scoring/explain.py` | 150 | 中文短句 |
| 18 個 dimension 檔 | 100×18 = 1800 | 各維度函式 |
| 5 個 transformation 檔 | 50×5 = 250 | 變換函式 |

### 3.4 Graph
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/graph/store.py` | 60 | GraphStore ABC |
| `phase3/graph/sqlite_store.py` | 350 | SQLite 實作 |
| `phase3/graph/networkx_store.py` | 150 | Phase 3B+ |
| `phase3/graph/evidence_tracer.py` | 200 | 證據追溯 |
| `phase3/graph/traversal.py` | 100 | BFS/DFS/shortest |
| `phase3/graph/subgraph.py` | 100 | 子圖匯出 |
| `phase3/graph/population.py` | 150 | 自動建圖 |

### 3.5 Storage
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/storage/db.py` | 100 | connection management |
| `phase3/storage/schema.py` | 150 | DDL |
| `phase3/storage/migrations.py` | 100 | SchemaMigrator |
| `phase3/storage/score_repo.py` | 150 | score_snapshot CRUD |
| `phase3/storage/signal_repo.py` | 150 | signal_log CRUD |
| `phase3/storage/graph_repo.py` | 150 | graph_nodes/edges CRUD |

### 3.6 Config
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/config/loader.py` | 150 | YAML loader |
| `phase3/config/validator.py` | 200 | 驗 schema |
| `phase3/config/hasher.py` | 50 | config_hash |

### 3.7 CLI / Server
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `phase3/cli.py` | 50 | entrypoint |
| `phase3/cli/main.py` | 100 | argparse 主入口 |
| `phase3/cli/signal.py` | 100 | signal 子命令 |
| `phase3/cli/score.py` | 200 | score 子命令 |
| `phase3/cli/graph.py` | 150 | graph 子命令 |
| `phase3/cli/config.py` | 100 | config 子命令 |
| `phase3/cli/explain.py` | 50 | explain 子命令 |
| `phase3/cli/backtest.py` | 150 | backtest 子命令 |
| `phase3/server/app.py` | 200 | FastAPI app (Phase 3B+) |
| `phase3/server/routes.py` | 300 | 端點 (Phase 3B+) |
| `phase3/server/auth.py` | 100 | Bearer token (Phase 3B+) |
| `phase3/server/ratelimit.py` | 100 | rate limit (Phase 3B+) |

### 3.8 Tests（估計 ~30 個檔案）
| 路徑 | 估計行數 | 用途 |
|---|---|---|
| `tests/phase3/test_*.py` × 30 | 平均 200 行 | 完整覆蓋 |

### 3.9 Configs（YAML）
| 路徑 | 用途 |
|---|---|
| `config/phase3/enabled.yaml` | kill switch |
| `config/phase3/cross_layer_adjustment.yaml` | 跨層影響 |
| `config/phase3/sector.yaml` | 產業 ↔ 個股 |
| `config/phase3/sources/*.yaml` | 6 個 source manifest |
| `config/phase3/decay.yaml` | decay 規則 |
| `config/phase3/scorers/*.yaml` | 3 個 scorer manifest |
| `config/phase3/weights/*.yaml` | 4 個 weight 設定 |
| `config/phase3/indicators/*.yaml` | 3 個 indicator 規則 |
| `config/phase3/industry_stocks.yaml` | 產業成分股 |
| `config/phase3/people.yaml` | 人物 index |
| `config/phase3/thresholds/*.yaml` | threshold 表 |

**總計**：~12,000 行 Python（含 tests）+ ~2,000 行 YAML。

---

## 4. 命名約定

| 類型 | 命名 | 範例 |
|---|---|---|
| 檔案 | snake_case | `signal_engine.py` |
| Class | PascalCase | `SignalEngine` |
| frozen dataclass | PascalCase | `NormalizedSignal` |
| enum value | snake_case | `"yfinance"` |
| function | snake_case | `compute_score` |
| 私有 function | _prefix | `_load_config` |
| 私有 module | _prefix | `_internal.py` |
| 公開 API re-export | 在 `__init__.py` | `from .signals import ...` |
| config 檔 | snake_case.yaml | `macro_weights.yaml` |
| test 檔 | test_<module>.py | `test_signal_engine.py` |
| fixture 檔 | conftest.py | |
| 環境變數 | PHASE3_* prefix | `PHASE3_API_KEY` |

---

## 5. Type Hints 約定

- **必用**：所有 function signature 必含完整 type hints
- **必用**：所有 frozen dataclass 必含 type hints
- **必用**：list / dict / Optional 必含 element type
- **工具**：用 `from __future__ import annotations` 支援 forward reference
- **不許用**：Any, 沒有 type hint 的函式

範例：
```python
from __future__ import annotations
from typing import Literal
from datetime import datetime

def score(
    self,
    signals: list[WeightedSignal],
    as_of: datetime | None = None,
) -> MacroScore:
    ...
```

---

## 6. 測試目錄結構

```
tests/phase3/
├── __init__.py
├── conftest.py                          # 共用 fixture
├── fixtures/
│   ├── __init__.py
│   ├── sample_yfinance_data.py          # 假 yfinance 結果
│   ├── sample_t86_data.py               # 假 T86 結果
│   ├── sample_rss_data.py               # 假 RSS
│   ├── sample_macro_scores.py           # 假 MacroScore
│   ├── sample_industry_scores.py        # 假 IndustryScore
│   └── sample_company_scores.py         # 假 CompanyScore
│
├── test_signal_engine.py
├── test_source_adapters/
│   ├── test_yfinance_adapter.py
│   ├── test_rss_adapter.py
│   ├── test_t86_adapter.py
│   └── test_macro_adapter.py
├── test_decay.py
├── test_weighting.py
├── test_confidence.py
├── test_aggregator.py
│
├── test_scorer/
│   ├── test_macro_scorer.py
│   ├── test_industry_scorer.py
│   └── test_company_scorer.py
│
├── test_dimensions/
│   ├── test_macro_dimensions.py
│   ├── test_industry_dimensions.py
│   └── test_company_dimensions.py
│
├── test_transformations.py
├── test_cross_layer.py
│
├── test_graph/
│   ├── test_graph_store.py
│   ├── test_evidence_tracer.py
│   └── test_subgraph.py
│
├── test_storage/
│   ├── test_schema.py
│   ├── test_migrations.py
│   ├── test_score_repo.py
│   ├── test_signal_repo.py
│   └── test_graph_repo.py
│
├── test_config.py
├── test_explain.py
├── test_cli.py
└── test_backtest.py
```

### 6.1 Fixture 範例
```python
# tests/phase3/fixtures/sample_yfinance_data.py

SAMPLE_2330_INFO = {
    "trailingPE": 32.8,
    "returnOnEquity": 0.362,
    "returnOnAssets": 0.221,
    "revenueGrowth": 0.351,
    "earningsGrowth": 0.584,
    "grossMargins": 0.619,
    "operatingMargins": 0.495,
    "profitMargins": 0.420,
    "trailingAnnualDividendRate": 24.0,
    "trailingAnnualDividendYield": 0.0101,
    "priceToBook": 10.61,
    "pegRatio": 0.85,
    "beta": 1.05,
    "fiftyTwoWeekHigh": 2440.0,
    "fiftyTwoWeekLow": 1325.0,
    "currentPrice": 2410.0,
    "targetMeanPrice": 2648.0,
    "marketCap": 6.25e12,  # TWD
    "debtToEquity": 18.0,
    "currentRatio": 2.5,
    "freeCashflow": 1.4e12,
}
```

---

## 7. 與 venv 與 supervisord 的關係

### 7.1 venv
Phase 3 共用現有 `macro-venv`（位於 `/home/ubuntu/macro-venv/`）。
- 額外需要的套件：無（全部 stdlib + 既有的 yfinance）
- Phase 4 之後若要接 NetworkX / Neo4j 才需新增

### 7.2 supervisord
- Phase 3 Phase 3A 階段不需常駐 process（CLI 觸發式）
- Phase 3B 的 server 才需 supervisord 加 program
- Phase 3A 期間**不動** supervisord

### 7.3 cron
- **絕不**修改現有 `~/.hermes/cron/jobs.json`
- Phase 3 的觸發透過現有 cron → run.sh → 加 phase3 hook

---

## 8. 開發工具鏈

| 工具 | 用途 | 必用？ |
|---|---|---|
| `unittest`（stdlib） | 單元測試 | 必用 |
| `pytest`（optional） | 較好讀的 test runner | optional（要裝才能用） |
| `mypy --strict` | 型別檢查 | 必用 |
| `ruff` | lint + format | optional |
| `coverage` | 測試覆蓋率 | 必用，目標 > 80% |

---

## 9. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 目錄過深、import 鏈太長 | `phase3/__init__.py` re-export 公開 API，使用端只 `from phase3 import X` |
| 模組耦合導致 unit test 難寫 | 嚴格 import 邊界（見 §2）；每個模組可單獨 mock |
| 與現有 reports/ 命名衝突 | Phase 3 在獨立 `phase3/` 子樹，零交叉 |
| 既有檔案被誤改 | CI step 跑 protected file baseline check（繼承 Phase 2B SOP） |
| 測試 fixture 膨脹 | fixture 集中管理（`tests/phase3/fixtures/`） |

---

_對應模組：`phase3/*`（約 70 個 Python 檔 + 30 個 test 檔 + 25 個 YAML）_
