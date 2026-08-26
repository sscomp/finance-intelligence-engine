# Finance Intelligence Engine (FIE)

Finance Intelligence Engine（FIE）是一套針對台灣股市的投資研究核心引擎。它將總體經濟指標、產業動態、公司財務數據與法人買賣超等多來源市場訊號，轉換為具有可追溯性（Traceable）、可解釋性（Explainable）、可評分（Scorable）的結構化投資情報。系統涵蓋信號擷取、維度評分、研究圖譜、投資組合決策建議與風險控管，並以 SQLite 為持久層，提供完整的 CLI 操作介面。

> 本專案目前處於 **工程完成（Engineering Complete）** 階段，已通過完整的單元測試與安全防護驗證。營運驗收（Operational Acceptance）正在進行中。本專案尚未宣告任何開源授權條款。

---

## 目錄

- [核心能力](#核心能力)
- [系統架構](#系統架構)
- [Repository 結構](#repository-結構)
- [資料來源與新鮮度原則](#資料來源與新鮮度原則)
- [投資組合 Shadow-Run 與營運驗收](#投資組合-shadow-run-與營運驗收)
- [安裝與環境設定](#安裝與環境設定)
- [快速開始](#快速開始)
- [測試](#測試)
- [治理文件與 ADR](#治理文件與-adr)
- [安全限制與非目標](#安全限制與非目標)
- [專案狀態與路線圖](#專案狀態與路線圖)
- [授權](#授權)

---

## 核心能力

| 能力 | 說明 |
|------|------|
| **多層信號擷取** | 透過 Adapter 模式整合 yfinance（總體經濟指標、公司財務）、TWSE T86 API（法人買賣超）、RSS（產業新聞）等資料來源，統一轉換為 Signal 資料結構 |
| **三層評分引擎** | Macro（總體經濟 6 維度）、Industry（產業 6 維度）、Company（公司 7 維度），每個分數附帶證據鏈與信心值 |
| **研究圖譜** | 將信號、分數、來源之間的關係建模為有向圖（SQLite Graph Store），支援上游溯源（Lineage）、下游影響範圍（Blast Radius）、跨層衝擊分析（Cross-Layer Impact） |
| **分數解釋** | `explain-score` CLI 可從任一分數節點出發，自動組裝完整的證據鏈報告（JSON / Markdown） |
| **Shadow-Run 決策重放** | 將歷史 Pipeline 產出的 JSON artifact 重新執行，比較分數差異（score delta）、信心值變化，支援 `structured_copy` 與 `real_replay` 兩種模式 |
| **投資組合決策引擎** | 基於分數的配置建議（score-weighted / risk-aware）、風險指標計算（集中度、最大回撤、相關性、風險預算）、執行規劃與報告產出 |
| **歷史資料回填工具** | `backfill` CLI 支援 gap 偵測、dry-run 規劃、以及受控的歷史資料補填（僅寫入臨時 DB，永不寫入生產 DB） |
| **資料新鮮度守護** | 交易日感知的 staleness 分類（FRESH / STALE / HARD_EXPIRED），CLI `--freshness-check` 旗標可在 Pipeline 執行時自動檢查 |

---

## 系統架構

```text
External Sources (yfinance / TWSE T86 / RSS)
        │
        ▼
  Signal Adapters
  (phase3/signals/adapters/)
        │
        ▼
  Signal Engine
  (phase3/signals/)
     ├── normalize / decay / confidence / weighting
        │
        ▼
  SQLite Persistence
  (phase3/persistence/)
     ├── signal_log / score_snapshot / graph_nodes / graph_edges
        │
        ▼
  Signal Loader → Input Builder
  (phase3/pipeline/)
        │
        ▼
  Scoring Pipeline
  (phase3/scoring/)
     ├── MacroScorer (6 dimensions)
     ├── IndustryScorer (6 dimensions)
     └── CompanyScorer (7 dimensions + cross-layer)
        │
        ▼
  Research Graph
  (phase3/graph/)
     ├── Evidence Tracer / Lineage / Blast Radius / Cross-Layer Impact
        │
        ▼
  Intelligence Pipeline
  (phase3/pipeline/intelligence_pipeline.py)
     ├── Shadow Run (decision replay)
     └── Portfolio Shadow Run (portfolio replay)
        │
        ▼
  Portfolio Decision Engine
  (phase3/portfolio/)
     ├── Allocation / Risk / Execution / Reporting
     └── Safety Guards (TD7 / TD8)
```

### 關鍵設計原則

- **分層評分**：信號 → 維度 → 個體 → 個體×維度 → 整體，每一層可獨立讀取與除錯
- **建構即解釋**：任何分數必帶 `evidence: list[EvidenceItem]`，含來源、權重、時間戳、衰減量
- **純函式**：所有 Scorer 為 `score(context) -> ScoreResult`，無副作用、無 I/O
- **Config 驅動**：權重、閾值、衰減函數全部在 YAML 配置，不寫死於程式碼
- **不可變 artifact**：每個 ScoreResult 為 frozen dataclass，附 hash，可做前後差異比較
- **評分路徑無 LLM**：分數全為確定性計算；LLM 僅在敘述解釋階段介入
- **零破壞**：Phase 3 模組不 import 既有腳本（`macro_daily.py` 等），既有報表路徑完全不動

---

## Repository 結構

```text
macro-report/
├── phase3/                       # Phase 3 Intelligence Engine 核心
│   ├── api.py                    # Pipeline API (factory / errors)
│   ├── backfill.py               # 歷史資料回填工具
│   ├── cli.py                    # CLI 入口（所有子命令）
│   ├── freshness.py              # 資料新鮮度分類與守護
│   ├── bridge/                   # Real-score bridge (seed_signals)
│   ├── config/                   # 配置載入
│   ├── datamodel/                # 資料模型（Signal / Score / Evidence / Graph）
│   ├── graph/                    # 研究圖譜（traversal / queries / optimization）
│   ├── persistence/              # SQLite 持久層（schema / repo / migrations / retention）
│   ├── pipeline/                 # Pipeline（scoring / shadow_run / portfolio_shadow_run / reporting）
│   ├── portfolio/                # 投資組合（domain / allocation / risk / decision / execution / report）
│   ├── scoring/                  # 評分引擎（macro / industry / company / explain）
│   └── signals/                  # 信號系統（engine / adapters / decay / confidence / weighting）
├── config/                       # 配置檔案
│   ├── holidays.json             # 台股交易日曆（國定假日）
│   ├── phase3/                   # Phase 3 配置（scorers / sources / decay / weights）
│   ├── policies/                 # 策略配置（retry / scoring）
│   ├── publishers/               # 發布配置（telegram）
│   └── reports/                  # 報表 manifest（macro_daily / industry_weekly / company_monthly）
├── tests/                        # 測試套件
│   ├── phase3/                   # Phase 3 單元測試
│   ├── test_bridge_seed_signals.py
│   ├── test_phase2b_framework.py
│   ├── test_phase2b_step4b.py
│   ├── test_real_score_bridge.py
│   └── ...
├── docs/                         # 文件
│   ├── phase3/                   # Phase 3 設計文件（架構 / 信號 / 評分 / 圖譜 / 資料模型）
│   ├── contracts/                # 介面契約
│   ├── runbooks/                 # 操作手冊
│   ├── architecture/             # 架構文件
│   ├── governance/               # 治理文件與里程碑紀錄
│   └── adr/                      # 架構決策紀錄（ADR）
├── templates/                    # 報表模板
├── macro_daily.py                # 總體經濟日報資料擷取腳本
├── company_monthly.py            # 公司研究月報資料擷取腳本
├── industry_weekly.py            # 產業趨勢週報資料擷取腳本
├── institutional.py              # 法人買賣超資料擷取模組
├── db.py                         # 資料庫操作模組
├── run_report.py                 # Phase 2B 報表框架 CLI
├── run.sh                        # 日報執行腳本
├── run_weekly.sh                 # 週報執行腳本
├── run_monthly.sh                # 月報執行腳本
├── taiwan50_config.json          # 台灣50成分股配置
├── industry_config.json          # 產業關鍵字與供應鏈映射
└── .gitignore
```

---

## 資料來源與新鮮度原則

### 資料供應鏈

FIE 的資料來源分為三層：

1. **原始資料層**：`macro_daily.py`、`company_monthly.py`、`institutional.py` 透過 yfinance API 及 TWSE T86 API 擷取資料，寫入 `macro_history.db` 的三張表（`macro_daily`、`stock_monthly`、`institutional_daily`）
2. **Bridge 層**：`phase3/bridge/seed_signals.py` 將 `macro_history.db` 的資料映射為 Phase 3 的 Signal 記錄，寫入 `intelligence.db` 的 `signal_log` 表
3. **評分層**：Pipeline 從 `signal_log` 讀取信號，經過 Scoring Pipeline 產出分數與證據鏈

### 新鮮度與過時守護

系統實作了交易日感知的新鮮度分類（`phase3/freshness.py`）：

- **FRESH**：資料日期在可接受範圍內
- **STALE**：資料日期超過正常更新週期，CLI 發出警告
- **HARD_EXPIRED**：資料嚴重過時，建議不使用

新鮮度判斷考量台股交易日曆（`config/holidays.json`），避免在假日或國定假日誤判為過時。`pipeline-run --freshness-check` 旗標可在執行時自動檢查。

### 歷史資料治理

歷史資料的保留、回填與品質管理遵循以下原則：

- `macro_history.db` 使用 `INSERT OR REPLACE`（冪等 upsert），無刪除或修剪邏輯
- Phase 3 的 `retention.py` 對 `signal_log` 和 `graph_edges` 提供顯式清理工具，但 `score_snapshot` 為 append-only
- 回填工具（`backfill` CLI）永遠只寫入指定的臨時 DB，拒絕寫入生產 `macro_history.db`
- `stock_monthly` 的歷史回填受 yfinance API 限制（`.info` 僅提供當前快照），已標記為 BLOCKED，不造假資料

詳見 [歷史資料治理文件](docs/architecture/historical-data-governance.md)。

---

## 投資組合 Shadow-Run 與營運驗收

### Shadow-Run 機制

Shadow-Run 是 FIE 的決策重放驗證機制，用於確保 Pipeline 的確定性：

- **Intelligence Pipeline Shadow-Run**（`shadow-run` CLI）：重放歷史 artifact 的評分決策，比較分數差異
- **Portfolio Shadow-Run**（`portfolio-shadow-run` CLI）：重放投資組合決策，驗證配置建議的穩定性

兩種模式皆為唯讀操作，不寫入任何 DB，不修改任何 artifact。

### M8 營運驗收

M8 是 Phase 5 路線圖的營運驗收里程碑，要求連續 7 天執行 `portfolio-run` 對比真實 artifact，0 次非預期失敗。觀測期間使用 R1–R7 調解檢查，其中 R3（jobs.json 不變性）使用 scoped signature 比較，排除排程器的揮發性執行時欄位（`last_run_at`、`next_run_at`、`state` 等），僅比較語意配置欄位。

詳見 [M8 里程碑文件](docs/governance/milestones/m8.md)。

---

## 安裝與環境設定

### 系統需求

- Python 3.11+
- SQLite 3（系統內建）
- 網路連線（yfinance API、TWSE API、RSS feeds）

### 設定步驟

```bash
# 1. Clone 專案
git clone https://github.com/sscomp/finance-intelligence-engine.git
cd finance-intelligence-engine

# 2. 建立虛擬環境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安裝相依套件
pip install yfinance requests pyyaml

# 4. 驗證 CLI 可用
python -m phase3.cli --help
```

> 本專案沒有 `requirements.txt` 或 `pyproject.toml`。上述相依套件為實際使用的外部套件。`phase3/` 子套件為純標準庫實作，不需要額外安裝。

---

## 快速開始

### 初始化資料庫

```bash
# 初始化 Phase 3 intelligence.db（預設路徑：phase3/data/intelligence.db）
python -m phase3.cli init-db --force

# 指定自訂路徑
python -m phase3.cli init-db --db-path /tmp/my-intelligence.db --force
```

### 執行評分 Pipeline

```bash
# Dry-run（預設，不寫入 DB）
python -m phase3.cli pipeline-run --date 2026-08-10

# 持久化到指定 DB
python -m phase3.cli pipeline-run --date 2026-08-10 --persist --db-path /tmp/intelligence.db

# 含新鮮度檢查
python -m phase3.cli pipeline-run --date 2026-08-10 --freshness-check

# 匯出 JSON artifact
python -m phase3.cli pipeline-run --date 2026-08-10 --json --output /tmp/report.json
```

### 解釋分數

```bash
# 使用內建範例圖譜
python -m phase3.cli explain-score

# 從 Pipeline artifact 解釋
python -m phase3.cli explain-score --from-pipeline --pipeline-artifact /tmp/report.json

# 從 SQLite graph store 查詢
python -m phase3.cli explain-score --db-path /tmp/intelligence.db --node "score:company:2330"
```

### Shadow-Run 決策重放

```bash
# Structured copy 模式（預設）
python -m phase3.cli shadow-run --artifact /tmp/report.json

# Real replay 模式（重新執行 scorer）
python -m phase3.cli shadow-run --artifact /tmp/report.json --inputs-source real_replay
```

### 投資組合操作

```bash
# 執行投資組合配置
python -m phase3.cli portfolio-run \
  --pipeline-artifact /tmp/report.json \
  --portfolio-file /tmp/portfolio.json \
  --policy-type score_weighted

# 投資組合 shadow-run
python -m phase3.cli portfolio-shadow-run \
  --artifact /tmp/report.json \
  --portfolio-file /tmp/portfolio.json
```

### 歷史資料回填

```bash
# Dry-run（規劃模式，不寫入）
python -m phase3.cli backfill \
  --source macro_daily \
  --target-db /tmp/backfill.db \
  --start 2025-08-10 --end 2026-06-17 \
  --dry-run

# 實際回填（僅寫入指定 DB）
python -m phase3.cli backfill \
  --source macro_daily \
  --target-db /tmp/backfill.db \
  --start 2025-08-10 --end 2026-06-17 \
  --execute
```

### 研究圖譜查詢

```bash
# 上游溯源
python -m phase3.cli lineage --node "score:company:2330" --db-path /tmp/intelligence.db

# 下游影響範圍
python -m phase3.cli blast-radius --node "signal:macro:global" --db-path /tmp/intelligence.db

# 跨層衝擊分析
python -m phase3.cli cross-layer-impact --node "score:macro:global" --db-path /tmp/intelligence.db
```

---

## 測試

### 執行測試

```bash
# Phase 3 完整測試套件
PYTHONPATH=. python -m unittest discover -s tests/phase3

# 全部測試（含 Phase 2B framework + bridge）
PYTHONPATH=. python -m unittest discover -s tests --top-level-dir=.

# 特定測試模組
PYTHONPATH=. python -m unittest tests.phase3.test_scorers
PYTHONPATH=. python -m unittest tests.phase3.test_portfolio_allocation
```

### 測試哲學

- **確定性優先**：所有測試必須為確定性（deterministic），不依賴網路或時間
- **安全防護測試**：獨立的安全防護測試套件驗證生產 DB 不被修改、路徑守護（macro_history.db refusal）、以及投資組合的安全限制（TD7/TD8）
- **Shadow-Run 確定性**：Pipeline artifact 的重放結果必須 byte-identical（modulo `generated_at`）
- **測試 snapshot**：截至最後驗證（2026-08-26），Phase 3 套件 1787 tests PASS，全部測試 1961 tests PASS（2 skipped）

> 上列測試數量為特定時間點的 snapshot，非永久宣告。實際數量隨開發進展會變動。

---

## 治理文件與 ADR

本專案的演進決策與治理歷史已整理為公開文件結構：

- [治理文件索引](docs/governance/README.md) — 如何閱讀治理歷史
- [系統狀態總覽](docs/governance/master-status.md) — 當前里程碑狀態
- [決策軌跡](docs/governance/decision-log.md) — 依時間順序的關鍵決策
- [M6 里程碑](docs/governance/milestones/m6.md) — 投資組合報告
- [M7 里程碑](docs/governance/milestones/m7.md) — 工程完成驗證
- [M8 里程碑](docs/governance/milestones/m8.md) — 營運驗收

### 架構文件

- [FIE 架構總覽](docs/architecture/finance-intelligence-engine.md)
- [資料供應鏈](docs/architecture/data-supply-chain.md)
- [歷史資料治理](docs/architecture/historical-data-governance.md)

### 架構決策紀錄（ADR）

- [ADR-001: Real-Score Bridge](docs/adr/adr-001-real-score-bridge.md)
- [ADR-002: Freshness/Staleness Metadata Guards](docs/adr/adr-002-freshness-staleness-guards.md)
- [ADR-003: Historical Backfill Policy](docs/adr/adr-003-historical-backfill-policy.md)
- [ADR-004: M8 Observation R3 Validation Semantics](docs/adr/adr-004-m8-r3-validation-semantics.md)

### Phase 3 設計文件

Phase 3 的完整設計文件（架構總覽、信號引擎、評分維度、研究圖譜、資料模型等）位於 [docs/phase3/](docs/phase3/README.md)。

---

## 安全限制與非目標

### 安全限制

- **生產 DB 守護**：所有 CLI 操作拒絕寫入 `macro_history.db`（case-insensitive basename refusal）
- **投資組合安全守護**：TD7（風險指標限制）、TD8（配置限制），包括集中度上限、最大回撤、HHI 限制、相關性限制
- **無券商整合**：系統不包含任何券商 API、網路下單、或訂單執行功能
- **評分路徑無 LLM**：所有分數為確定性計算，LLM 僅用於敘述解釋
- **回填安全性**：回填工具永遠只寫入臨時 DB，需要明確的 `--execute` 旗標才執行

### 非目標

- 本專案不提供即時交易訊號或自動化交易
- 本專案不取代任何現有報表系統（Phase 3 為評分引擎，既有腳本為發布器）
- 本專案不提供投資建議；所有輸出為研究分析用途
- 本專案不包含前端 UI 或 Web 介面

---

## 專案狀態與路線圖

### 目前狀態

Phase 5 工程完成（Engineering Complete）。M2–M7 全部完成，M8 營運驗收進行中。

| 里程碑 | 內容 | 狀態 |
|--------|------|------|
| M2 | 投資組合領域模型 | 完成 |
| M3-S1/S2/S3 | 風險指標（集中度、回撤、相關性、風險預算） | 完成 |
| M4-S1/S2/S3 | 配置引擎（score-weighted、risk-aware、CLI） | 完成 |
| M5 | 執行規劃 | 完成 |
| M6 | 投資組合報告 | 完成 |
| M7 | 工程完成驗證 | 完成 |
| M8 | 營運驗收（7 日 shadow-run） | 進行中 |
| M9 | 生產就緒 | 待定 |

### 路線圖摘要

- **M8**：連續 7 天 portfolio shadow-run 觀測，驗證決策穩定性與 R1–R7 調解檢查
- **M9**：最終 SSOT 升級，正式宣告 Phase 5 完成

---

## 授權

本專案目前 **尚未宣告任何開源授權條款**。

在宣告授權條款之前，本專案的所有權利均予保留。未經明確授權，不得複製、修改、分發或使用本專案的任何部分。

---

*Finance Intelligence Engine — 可解釋、可追溯、可配置的投資研究引擎。*