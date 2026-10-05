# FIE 架構總覽

> **⚠️ CURRENT-STATE NOTE（2026-10-05）**：本文件為 SQLite 時代的層次/流程描述
> （歷史設計記錄，保留原狀）。目前 **Production 後端為 PostgreSQL**（authoritative
> production datastore）；**SQLite 僅為 rollback/recovery source**；DB target 一律
> 明確指定 + fail-closed（Phase 6.8A canonical contract；下圖「SQLite Persistence」
> 及〈資料流〉之 SQLite 檔名與預設請以新契約解讀）。current-state 敘事見
> [PostgreSQL Production Architecture](postgresql-production-architecture.md)。

## 定位

Finance Intelligence Engine（FIE）是一套投資研究核心引擎，將多來源市場訊號轉換為可解釋、可追溯、可評分的結構化情報。它不取代現有報表系統，而是作為評分引擎層，為投資決策提供可審計的分析基礎。

## 架構層次

```text
External Sources
    │
    ├── yfinance API      → macro_daily, stock_monthly
    ├── TWSE T86 API      → institutional_daily
    └── RSS feeds         → industry news
         │
         ▼
    Signal Adapters
    (phase3/signals/adapters/)
    ├── yfinance adapter
    ├── t86 adapter
    ├── rss adapter
    └── fixture adapter (testing)
         │
         ▼
    Signal Engine
    (phase3/signals/)
    ├── normalize / decay / confidence / weighting
         │
         ▼
    SQLite Persistence
    (phase3/persistence/)
    ├── signal_log      → Signal 記錄
    ├── score_snapshot  → 分數快照（append-only）
    ├── graph_nodes     → 圖譜節點
    └── graph_edges     → 圖譜邊
         │
         ▼
    Scoring Pipeline
    (phase3/scoring/ + phase3/pipeline/)
    ├── MacroScorer     → 6 dimensions
    ├── IndustryScorer  → 6 dimensions
    ├── CompanyScorer   → 7 dimensions + cross-layer
    └── ScoringPipeline → 整合三層評分
         │
         ▼
    Research Graph
    (phase3/graph/)
    ├── Evidence Tracer  → 溯源
    ├── Lineage          → 上游證據鏈
    ├── Blast Radius     → 下游影響
    └── Cross-Layer Impact → 跨層衝擊
         │
         ▼
    Intelligence Pipeline
    (phase3/pipeline/intelligence_pipeline.py)
    ├── Pipeline Result  → JSON artifact
    ├── Shadow Run       → 決策重放
    └── Portfolio Shadow Run → 投資組合重放
         │
         ▼
    Portfolio Decision Engine
    (phase3/portfolio/)
    ├── Domain Model     → Position / Portfolio / Constraints
    ├── Risk             → Concentration / Drawdown / Correlation / Budget
    ├── Allocation       → Score-weighted / Risk-aware
    ├── Decision         → PortfolioDecision DTO
    ├── Execution        → ExecutionPlan
    └── Report           → Markdown + JSON report
```

## 設計原則

| 原則 | 說明 |
|------|------|
| 分層評分 | 信號 → 維度 → 個體 → 個體×維度 → 整體 |
| 建構即解釋 | 任何分數必帶 evidence（來源、權重、時間戳、衰減量） |
| 純函式 | 所有 Scorer 為 `score(context) -> ScoreResult`，無副作用 |
| Config 驅動 | 權重、閾值、衰減函數在 YAML 配置 |
| 不可變 artifact | ScoreResult 為 frozen dataclass，附 hash |
| 零破壞 | Phase 3 不 import 既有腳本 |
| 評分路徑無 LLM | 分數全為確定性計算 |

## 評分維度

### Macro（總體經濟，6 維度）

economic / monetary / inflation / rates / liquidity / geopolitics

### Industry（產業，6 維度）

capital_flow / rotation / relative_strength / cyclicality / industry_news / macro_sensitivity

> 目前僅 `capital_flow` 有本地權威資料來源（法人買賣超聚合），其他維度預設為中性。

### Company（公司，7 維度 + cross-layer）

valuation / profitability / growth / dividend / quality / momentum / capital_structure

> Company 分數可讀取上游 Macro 和 Industry 分數做 cross-layer 條件調整。

## 資料流

1. Cron 腳本（`run.sh` / `run_weekly.sh` / `run_monthly.sh`）執行資料擷取 + pipeline-export
2. `macro_daily.py` 等腳本擷取資料寫入 `macro_history.db`
3. `phase3.cli pipeline-export` 將資料轉為 JSON artifact（dry-run，不寫 DB）
4. `phase3.cli pipeline-run --persist` 將信號與分數持久化到 `intelligence.db`
5. `phase3.cli explain-score` / `shadow-run` / `portfolio-run` 使用持久化資料或 artifact 做分析

## 相關文件

- [資料供應鏈](data-supply-chain.md)
- [歷史資料治理](historical-data-governance.md)
- [Phase 3 設計文件](../phase3/README.md)