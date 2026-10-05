# Finance Intelligence Engine (FIE)

Finance Intelligence Engine（FIE）是一套針對台灣股市的投資研究核心引擎。它將總體經濟指標、產業動態、公司財務數據與法人買賣超等多來源市場訊號，轉換為具有可追溯性（Traceable）、可解釋性（Explainable）、可評分（Scorable）的結構化投資情報。系統涵蓋信號擷取、維度評分、研究圖譜、投資組合決策建議與風險控管，持久層採雙後端（生產環境：PostgreSQL；SQLite 僅為 rollback/recovery source），並提供完整的 CLI 操作介面。

> 本專案目前處於 **工程完成（Engineering Complete）** 階段，已通過完整的單元測試與安全防護驗證。營運驗收（Operational Acceptance）正在進行中。本專案尚未宣告任何開源授權條款。

---

## 當前生產狀態（Current production state — 2026-10-05）

> 本 README 其餘章節的 SQLite 預設路徑／SQLite 為預設之敘述，是 **6.8A 之前的歷史文件內容**（保留原狀以維持各 phase 記錄），已由 Phase 6.8A runtime contract normalization **廢除**（所有 target 明確指定、缺值/矛盾一律 fail-closed，無任何 silent default／fallback）。以本節與下方架构文件為準。

| 項目 | 現況 |
|---|---|
| **Production DB** | **PostgreSQL**（authoritative production datastore；2026-10-05 controlled cutover accepted） |
| **SQLite** | **ROLLBACK / RECOVERY SOURCE ONLY** — 不是 production default、不是 silent fallback；操作需 explicit rollback contract |
| **DB target 決定方式** | explicit arg ＞ canonical env（`FIE_DB_TARGET_*`）＞ legacy alias（`FIE_DB_PATH` / `FIE_DATABASE_URL` / `FIE_INTELLIGENCE_DB`）＞ **fail-closed（無任何預設）**；single canonical resolver：`phase3/runtime_contract.py` |
| **Production credentials** | operator-owned 0600 env contract files（**git 之外**；任何 secret-bearing env file 不得入 Git） |
| **Codex Cloud** | **NOT IMPLEMENTED / NOT ENABLED / NOT DEPLOYED** — 僅 readiness 架構與 API 提案文件（PROPOSED/FUTURE）；execution validation **NOT VALIDATED**（6.9A-R3） |
| **Isolated test PG provisioning** | repository-owned `scripts/provision-test-postgres.sh`（6.9A-R3）：discovery > pinned portable distribution（Maven Central, sha256-verified）— fresh clone 無系統 `initdb`/`pg_ctl` 仍得真實 ephemeral PG |

詳細架構：[PostgreSQL Production Architecture](docs/architecture/postgresql-production-architecture.md)、
[Codex Cloud Integration Readiness](docs/architecture/codex-cloud-integration-readiness.md)、
[Cloud Execution Contract（Phase 6.8C：fresh-clone bootstrap / isolated test 契約；6.9A-R3 更新）](docs/architecture/cloud-execution-contract.md)、
[Ephemeral Test-PostgreSQL Provisioning Contract（6.9A-R3）](docs/architecture/ephemeral-postgresql-provisioning.md)、
production baselines（[FIE_6_7B](docs/production/FIE_6_7B_PRODUCTION_BASELINE.md) /
[ABACUS_FIE_6_7B](docs/production/ABACUS_FIE_6_7B_PRODUCTION_BASELINE.md)）。

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
├── scripts/                      # canonical bootstrap / cloud-safe test / DB-target guard
│   ├── bootstrap.sh              # 一鍵 venv + 宣告安裝（idempotent；6.8C）
│   ├── test-cloud.sh             # 雲端安全隔離測試入口（scrubbed env + canonical provisioner；6.8C/R3）
│   ├── provision-test-postgres.sh# canonical ephemeral PG provisioner（portable distribution；6.9A-R3）
│   ├── cloud_negative_controls.py# runtime fail-closed 負向控制（8 情境；6.8C）
│   ├── rehearsal_db_guard.sh     # fail-closed DB-target guard（shell boundary）
│   └── db_target_identity.py     # guard 的 stdin 指紋 adapter（薄層）
├── examples/                     # operator 環境檔範本（synthetic placeholder；無 secret）
│   └── fie-wrapper.env.example
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

> **6.8C 檔案分類註記**：
> ① `run.sh` / `run_weekly.sh` / `run_monthly.sh` 是 **PRODUCTION_OPERATOR_ONLY**
> 的 cron 入口（依賴 operator-owned 0600 wrapper env file——範本見
> [examples/fie-wrapper.env.example](examples/fie-wrapper.env.example)；
> **不屬於** cloud execution profile，cloud 場景勿執行）。
> ② 根目錄的 `PHASE2*.md` / `PHASE3*` / `PHASE4_*` / `*REVIEW*` / `*SOP*` /
> `evidence_inventory.md` 為**歷史 phase/ops 記錄**（point-in-time；其 host 路徑
> 與 SQLite 時代敘述已由 6.8A/6.7B 廢除，以本檔「Current production state」為準）。
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
- （選用）PostgreSQL 後端 parity 測試：`pip install -e ".[postgres]"` 與一個 disposable PG cluster
- 網路連線（yfinance API、TWSE API、RSS feeds）

### 設定步驟（Phase 6.1 之後：宣告式相依 + 可移植路徑）

> **Canonical bootstrap（6.8C）**：fresh clone 後，步驟 2–3 可由一鍵入口取代
> （idempotent、含離線 import 驗證；契約見
> [Cloud Execution Contract](docs/architecture/cloud-execution-contract.md)），
> 隨後以 `scripts/test-cloud.sh` 執行隔離測試。

```bash
# 1. Clone 專案
git clone https://github.com/sscomp/finance-intelligence-engine.git
cd finance-intelligence-engine

# 2.（6.8C canonical，一鍵；等效取代下方手動步驟 2–3）
bash scripts/bootstrap.sh --all

# 2. 建立虛擬環境（手動等效路徑）
python3 -m venv .venv
source .venv/bin/activate

# 3. 依 pyproject.toml 宣告安裝（相依為 PyYAML、yfinance；pandas 僅測試需要）
pip install -e .
#   測試環境（含 pandas）：
pip install -e ".[test]"

# 4. 驗證 CLI 可用（或使用已安裝的 console script：fie-cli）
python -m phase3.cli --help
```

> Phase 6.1 起本專案以 `pyproject.toml` 宣告相依與套件邊界（無 `requests`；
> legacy fetch 家族 `phase3/` 引擎用標準庫，網路取數用 yfinance）。所有執行
> 時間路徑可由環境變數覆寫——在任意 checkout 位置與任意資料目錄執行皆可：
>
> | 環境變數 | 用途 | 預設 |
> |---|---|---|
> | `FIE_PROJECT_ROOT` | 專案根 | 由 `phase3/paths.py` 自動探測 |
> | `FIE_DATA_DIR` | 可寫資料目錄（含 DB 預設位置、logs） | 專案根 |
> | `FIE_CONFIG_DIR` | 設定目錄 | 專案根 |
> | `FIE_ARTIFACT_DIR` | 報告 artifact 輸出 | `<專案根>/metadata/reports/artifacts` |
> | `FIE_DB_PATH` | 歷史資料庫 raw target（SQLite 路徑或 PG DSN；cutover 後生產值為 PG DSN） | （6.8A 起：**無任何預設**；未指定屬 fail-closed） |
> | `FIE_DATABASE_URL` | intelligence target 指定（SQLite 路徑或 `postgres://` DSN） | （6.8A 起：**無任何預設**；未指定屬 fail-closed） |
> | `FIE_PYTHON` | wrapper 使用的直譯器（6.9A-R4-R2-R1 起：**hard contract** — 設定但不可用 → fail-closed 78；未設定時由 canonical resolver 解析：`$VIRTUAL_ENV` > repo `.venv` > PATH `python3`） | `python3`（未設定時可攜解析） |
> | `FIE_TELEGRAM_CHAT_ID` / `FIE_TELEGRAM_SECONDARY_CHAT_ID` | 派送 chat ID（個人資料，不落盤） | 無 |
>
> **⚠️ 6.8A normalization（2026-10-05，current state）**：上表「預設」欄位的 SQLite
> 預設屬歷史文件內容。Phase 6.8A 起 SQLite 時代的 implicit defaults **已廢除**：
> DB target 永遠 explicit（canonical `FIE_DB_TARGET_{RAW,INTELLIGENCE,TEST,ROLLBACK}`
> > 上表 legacy aliases），缺失/矛盾/malformed 一律 fail-closed 拒絕（exit 78），
> 無任何 silent fallback。Production 後端為 PostgreSQL；SQLite 僅為
> rollback/recovery source。詳見
> [PostgreSQL Production Architecture](docs/architecture/postgresql-production-architecture.md)。
>
> （以下為 Phase 6.3 歷史文件內容——保留時期語意）
> Phase 3B store (`intelligence.db`) 預設仍為 `phase3/data/intelligence.db`，可用
> 各 pipeline 子命令的 `--db-path` 覆寫。Phase 6.3 起的後端選擇與優先序：
> **明確引數 > `FIE_DATABASE_URL` > SQLite 預設路徑**。`postgres://`／
> `postgresql://` 開頭開啟 PostgreSQL 後端（本階段僅限 disposable/synthetic
> parity，見下文〈持久層後端〉；DSN 內含密碼時所有輸出一律遮罩，嚴禁將
> 含密碼 DSN 落入 Git 或日誌）。

---

## 快速開始

### 初始化資料庫

```bash
# 初始化 Phase 3 intelligence.db（預設路徑：phase3/data/intelligence.db）
python -m phase3.cli init-db --force

# 指定自訂路徑
python -m phase3.cli init-db --db-path /tmp/my-intelligence.db --force

# 指定 PostgreSQL（disposable/synthetic；見下節）
python -m phase3.cli init-db --db-path "postgresql://fie@/fie_test?host=/tmp/fie-pg&port=54329" --force
```

### 持久層後端 (Phase 6.3)

> **⚠️ 現況說明（Phase 6.8A/6.7B-cutover，2026-10-05）**：本小節為 Phase 6.3 歷史
> 語意（SQLite 預設）。目前 **Production 後端為 PostgreSQL**（authoritative
> production datastore）；**SQLite 僅為 rollback/recovery source**，且 6.8A 起任何
> implicit SQLite default 已廢除（所有 target 明確指定，缺值 fail-closed）。
> 詳見 [PostgreSQL Production Architecture](docs/architecture/postgresql-production-architecture.md)。

Phase 3B 的持久層在 Phase 6.3 抽象為雙後端：**SQLite 為預設並完整保留**；
**PostgreSQL 僅作為 disposable/synthetic parity 後端**（驗證 schema 與 SQL
方言可攜性；本階段不做 production migration／cloud provisioning）。兩個
後端共用同一份 repository SQL（`%s` 佔位符）、同一組 migration 版本
（v1 schema 雙方言孿生：`schema_v1` / `schema_pg`）、同一契約測試
（`tests/phase3/persistence/test_backend_contract.py`）與確定性 pipeline
parity 測試（`tests/phase3/test_pg_parity.py`）。

後端指定方式（Phase 6.3 歷史優先序：明確引數 > `FIE_DATABASE_URL` > SQLite 預設；
**6.8A 起預設已廢除，見上註記**）：

```bash
# 方式一：--db-path 明確引數（上述 init-db / pipeline-run 等）
# 方式二：環境變數
export FIE_DATABASE_URL="postgresql://user:password@host:5432/db"   # 不得含真實密碼入 Git/日誌
export FIE_DATABASE_URL="sqlite://phase3/data/intelligence.db"      # 等同預設
```

PostgreSQL 需要 optional extra：`pip install -e ".[postgres]"`（psycopg 3）。
**僅在 disposable 資料庫上使用**——schema `score_snapshot` 為 append-only
（trigger 強制），重置採 DROP 全表後重放 migration，切勿指向任何共
用／生產資料庫：

```bash
# Docker 一行式（有 docker 的主機）
docker run -d --rm --name fie-pg -e POSTGRES_USER=fie -e POSTGRES_PASSWORD=fie \
    -e POSTGRES_DB=fie_test -p 54329:5432 postgres:18

#  portable binaries（無 docker／無 root 主機；本階段驗證路徑）
#   1) 下載 postgresql-18.6 portable tarball（記錄 sha256 後解壓至 /tmp/fie-pg）
#   2) initdb 若報 libxml2 缺漏：建 /tmp/fie-pg/compat 目錄，
#      將 libxml2.so.2 symlink 指向主機的 libxml2（本主機為 libxml2.so.16），
#      以下指令皆帶 LD_LIBRARY_PATH=/tmp/fie-pg/compat
#   3) 使用者層 cluster（trust auth、/tmp socket、非常規埠）：
export PATH="/tmp/fie-pg/postgresql-18.6.0-x86_64-unknown-linux-gnu/bin:$PATH"
initdb -D /tmp/fie-pg/pgdata -U fie --auth=trust
pg_ctl -D /tmp/fie-pg/pgdata -l /tmp/fie-pg/server.log \
    -o "-p 54329 -k /tmp/fie-pg" start
createdb -h /tmp/fie-pg -p 54329 -U fie fie_test
```

### 服務邊界（Phase 6.5）

Phase 6.5 在持久層抽象之上新增**transport-neutral 型別化服務邊界**
（`phase3.service`）——未來 ChatGPT／其他 client 的唯一入口，全部**唯讀**：

| 操作 | 說明 |
|------|------|
| `get_health` | readiness：backend family、counts、schema 版本 |
| `get_latest_intelligence` | 各實體最新智慧摘要（含頭等 freshness / evidence refs / warnings） |
| `get_entity_intelligence` | 單一實體完整智慧：score、payload、上游訊號、溯源 |
| `get_evidence` | 沿 evidence graph 取得有限溯源（預設 50、上限 200） |
| `get_freshness` | 新鮮度狀態：`FRESH / DEGRADED / STALE / UNAVAILABLE` |

錯誤以穩定分類回報（`INVALID_REQUEST` / `NOT_FOUND` / `STALE_DATA` /
`DATA_UNAVAILABLE` / `DEPENDENCY_UNAVAILABLE` / `INTERNAL_ERROR`），
訊息經 URL/path 消毒；回應不含 DSN、SQL、表名、本機路徑或憑證。
批次（排程計算）進入點獨立為 `phase3.service.batch.BatchWorker`。

```bash
# in-process 呼叫範例（與未來任何 transport 同一組 envelope）
python3 - <<'PY'
from phase3.service import (
    DefaultIntelligenceService, dispatch, RequestContext,
)
service = DefaultIntelligenceService(open_store(resolve_spec()))  # 略去建置細節
ctx = RequestContext(principal_id="synthetic-alpha", request_id="req-001")
print(dispatch(service, "health", {}, ctx))
PY
```

完整 tool 契約（`fie.health` / `fie.latest_intelligence` /
`fie.entity_intelligence` / `fie.evidence` / `fie.freshness`，全部唯讀）：
[docs/architecture/phase6-5/chatgpt-tool-contract.md](docs/architecture/phase6-5/chatgpt-tool-contract.md)。
HTTP transport 刻意延後至 Phase 6.6+（ADR-010）。

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

> **⚠️ 6.8C 說明**：以下「執行測試」小節為歷史手動形式（可攜但無隔離保障）。
> **canonical 入口**請用 `scripts/test-cloud.sh`（scrubbed env + 隔離 PostgreSQL +
> 負向控制 + 自動清理；契約見
> [Cloud Execution Contract](docs/architecture/cloud-execution-contract.md) 與
> [Ephemeral Test-PostgreSQL Provisioning](docs/architecture/ephemeral-postgresql-provisioning.md)）。
> 6.9A-R3：fresh clone 即使**無**系統 PostgreSQL tooling，test-cloud 仍會經由
> canonical provisioner 以 pinned、sha256-verified 的 portable PostgreSQL
> distribution 起出真實 ephemeral cluster（首次取得需 Maven Central 網路；
> cached 之後離線）。
> **6.9A-R4-R2-R1**：interpreter 解析走 ONE canonical resolver
> （`scripts/fie_python_resolver.sh`：explicit `FIE_PYTHON` hard contract >
> `$VIRTUAL_ENV` > repo `.venv` > PATH `python3`；不可用 → exit 78）— 不依賴
> operator-host interpreter 路徑；scrubbed test child 以 explicit allowlist
> 受網路上下文（無 blanket 繼承），secret boundary denylist 永不進 child；
> 測試側 cache 解析與 runtime 同源（`tests/cloud_child_env.py::canonical_cache_state()`）。
> portable cache 的 acquisition lock 帶所有權 metadata（LK1）；cleanup 對
> ACTIVE 保留、對 UNKNOWN fail-closed 96（保留 + 回報）。
> `FIE_HTTP_PORT=18720` 等覆寫**僅**在服務並存主機上需要；transport tests 本身
> bind ephemeral 端口，可攜環境不須設定。

### 執行測試

```bash
# （6.8C canonical）雲端安全隔離測試：focused set + 負向控制
bash scripts/test-cloud.sh
# （6.8C canonical）full regression：hermetic、隔離 PG、負向控制、清理
bash scripts/test-cloud.sh --full

# Phase 3 完整測試套件（歷史手動形式）
PYTHONPATH=. python -m unittest discover -s tests/phase3

# 全部測試（含 Phase 2B framework + bridge）（歷史手動形式）
PYTHONPATH=. python -m unittest discover -s tests --top-level-dir=.

# 特定測試模組
PYTHONPATH=. python -m unittest tests.phase3.test_scorers
PYTHONPATH=. python -m unittest tests.phase3.test_portfolio_allocation
```

### 持久層契約與後端 parity 測試 (Phase 6.3)

```bash
# 後端契約測試：同一組斷言在 SQLite 與 disposable PostgreSQL 上各跑一次
#   （無 psycopg 或無 disposable cluster 時,PG 腿會附原因 skip，不會失敗）
PYTHONPATH=. python -m unittest tests.phase3.persistence.test_backend_contract

# 確定性 pipeline parity：SQLite vs PostgreSQL，同 fixture → 同 domain 內容
#   （run_id／wall-clock 時間戳為 per-run 揮發欄位，比對前正規化剔除）
PYTHONPATH=. python -m unittest tests.phase3.test_pg_parity
```

PG 測試目標 DSN 可用 `FIE_TEST_PG_DSN` 覆寫；預設指向本階段驗證用的
trust-auth `/tmp` cluster（`postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329`）。

### 服務邊界契約測試 (Phase 6.5)

```bash
# 唯讀服務邊界契約（SQLite 為主, PG 腿需 disposable cluster, 附原因 skip）
PYTHONPATH=. python -m unittest discover -s tests/phase3/service -t .
```

涵蓋工單 §17 的契約面：5 個操作的回應合約、錯誤分類、freshness 映射、
opaque principal 傳播、讀取路徑純度（永不寫入、永不觸發攝入）、
批次/互動分離、執行時設定優先序與機密消毒、可攜性審計（無開發機
絕對路徑）、structured 請求日誌（§15）。

### 參考 HTTP 執行時（Phase 6.6）

Phase 6.6 在凍結的 Phase 6.5 服務邊界上交付「最薄」HTTP 傳輸層
（`phase3.transport`，僅標準庫；[ADR-011](docs/adr/adr-011-reference-transport.md)）：

```bash
# 安裝（任何 CWD 皆可啟動；§12 封裝紀律）
python -m pip install -e .
fie-http-server                       # 或 python -m phase3.transport.http

# 設定（§9：參數 > 環境 > 安全預設）
# FIE_HTTP_HOST=127.0.0.1  FIE_HTTP_PORT=8787  FIE_AUTH_MODE=none|token
# FIE_AUTH_TOKEN=（token 模式；僅環境、永不入源碼/日誌）
# FIE_LOG_LEVEL=INFO  FIE_REQUEST_TIMEOUT=60  FIE_DATABASE_URL=<Phase 6.3 契約>
FIE_DATABASE_URL=sqlite:///tmp/fie66-smoke.db fie-http-server &
curl -s http://127.0.0.1:8787/healthz   # {"status":"alive"}——不觸及持久層
curl -s http://127.0.0.1:8787/readyz | head -c 200
```

路由（全部唯讀 GET；非 GET → 405；機器可讀契約見
[docs/architecture/phase6-6/http-api-contract.json](docs/architecture/phase6-6/http-api-contract.json)）：`/healthz`、
`/readyz`、`/v1/health`、`/v1/intelligence/latest`、
`/v1/intelligence/entity/{kind}/{entity_id}`、`/v1/evidence/{ref}`、
`/v1/freshness/{kind}/{entity_id}`。回應體為 Phase 6.5 dispatch
信封原樣；HTTP 狀態碼是確定性的錯誤映射（§7）。

容器參考構建（§13；非 root、明確埠、無內嵌憑證/生產資料——
**不**部署到生產）與 PostgreSQL 一次性叢集閘門（§15/§16）詳見
[docs/architecture/phase6-6/reference-runtime.md](docs/architecture/phase6-6/reference-runtime.md)。

```bash
# Phase 6.6 傳輸契約測試（SQLite 閘門 + 認證 + 設定 + 可攜性稽核 + PG 同構）
PYTHONPATH=. python -m unittest discover -s tests/phase3/transport -t .
```

### 測試哲學

- **確定性優先**：所有測試必須為確定性（deterministic），不依賴網路或時間
- **安全防護測試**：獨立的安全防護測試套件驗證生產 DB 不被修改、路徑守護（macro_history.db refusal）、以及投資組合的安全限制（TD7/TD8）
- **Shadow-Run 確定性**：Pipeline artifact 的重放結果必須 byte-identical（modulo `generated_at`）
- **測試 snapshot**：截至最後驗證（2026-08-26），Phase 3 套件 1787 tests PASS，全部測試 1961 tests PASS（2 skipped）
- **可移植性 (Phase 6.1)**：測試不假設任何 checkout 位置、使用者名稱或 venv 路徑。與主機相關的資料測試（production `macro_history.db` bridge 整合、外部 Hermes 排程器 R3 驗證）在該主機資料不存在時會明確 skip（附原因），不會失敗。

> 上列測試數量為特定時間點的 snapshot，非永久宣告。實際數量隨開發進展會變動。
> Phase 6.1 驗證（2026-10-03）：全部測試 1951 tests、0 failed、0 errors（59 justified skips，見 Phase 6.1 報告）。

### 時區政策 (Phase 6.1)

信號時間戳一律以 timezone-aware UTC 儲存與比較；naive 輸入（舊資料列、裸日期字串）視為 UTC，並在兩個邊界統一正規化——寫入邊界（adapter `_parse_*`）與讀取邊界（`phase3/pipeline/signal_loader.py`）。

### Golden 管線驗證命令

```bash
# 初始化臨時 DB → 載入 fixture 信號 → 三腿（Macro + Industry + Company）管線
PHASE3B_ENABLED=1 python -m phase3.cli init-db --db-path /tmp/gold/golden.db --force
PHASE3B_ENABLED=1 python -m phase3.cli ingest-signals --db-path /tmp/gold/golden.db \
    --source fixture --input tests/phase3/fixtures/fixture_company_industry_2026-07-08.json
PHASE3B_ENABLED=1 python -m phase3.cli pipeline-run --date 2026-07-08 \
    --db-path /tmp/gold/golden.db --persist --json --company 2330 --industry AI \
    --output /tmp/gold/golden.json
# 除 run_id / 時間戳 / duration 外，同輸入重跑必須 byte-identical（確定性契約）。
```

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

- [PostgreSQL Production Architecture](docs/architecture/postgresql-production-architecture.md) — **目前生產後端架構（current state）**
- [Codex Cloud Integration Readiness](docs/architecture/codex-cloud-integration-readiness.md) — **PROPOSED/FUTURE，未實作**
- [FIE 架構總覽](docs/architecture/finance-intelligence-engine.md)
- [資料供應鏈](docs/architecture/data-supply-chain.md)
- [歷史資料治理](docs/architecture/historical-data-governance.md)
- [Production Baseline — FIE 6.7B](docs/production/FIE_6_7B_PRODUCTION_BASELINE.md) / [Production Baseline — ABACUS FIE 6.7B](docs/production/ABACUS_FIE_6_7B_PRODUCTION_BASELINE.md)
- [Phase 6.5 ChatGPT Tool Contract](docs/architecture/phase6-5/chatgpt-tool-contract.md)
- [Phase 6.5 Service Planes](docs/architecture/phase6-5/service-boundary-planes.md)
- [Phase 6.6 Reference Runtime](docs/architecture/phase6-6/reference-runtime.md)
- [Phase 6.6 HTTP API Contract（machine-readable）](docs/architecture/phase6-6/http-api-contract.json)

### 架構決策紀錄（ADR）

- [ADR-001: Real-Score Bridge](docs/adr/adr-001-real-score-bridge.md)
- [ADR-002: Freshness/Staleness Metadata Guards](docs/adr/adr-002-freshness-staleness-guards.md)
- [ADR-003: Historical Backfill Policy](docs/adr/adr-003-historical-backfill-policy.md)
- [ADR-004: M8 Observation R3 Validation Semantics](docs/adr/adr-004-m8-r3-validation-semantics.md)
- [ADR-005: Application Service Boundary（Phase 6.5）](docs/adr/adr-005-service-boundary.md)
- [ADR-006: Interactive vs Batch Runtime（Phase 6.5）](docs/adr/adr-006-interactive-vs-batch-runtime.md)
- [ADR-007: ChatGPT Tool Boundary（Phase 6.5）](docs/adr/adr-007-chatgpt-tool-boundary.md)
- [ADR-008: Multi-User Data Boundary（Phase 6.5）](docs/adr/adr-008-multi-user-data-boundary.md)
- [ADR-009: Cloud-Safe Runtime Configuration（Phase 6.5）](docs/adr/adr-009-runtime-configuration.md)
- [ADR-010: Transport Decision（Phase 6.5）](docs/adr/adr-010-transport-decision.md)
- [ADR-011: Reference Transport & Thread-Affine Executor（Phase 6.6）](docs/adr/adr-011-reference-transport.md)
- [ADR-012: Auth Boundary（Phase 6.6）](docs/adr/adr-012-auth-boundary.md)

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