# ADR-C03: Persistence Target（SQLite 過渡 + 受管 PostgreSQL 邊界）

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

- **既有 seam（半套）**：Phase 3B `phase3/persistence/` 是唯一 SQL 邊界（repos + `SQLiteStore` + checksummed migrations），但 repos 是**具體類別、無 Protocol**（`signal_repo.py:94-228`、`score_repo.py:68-182`、`graph_repo.py:59-271` 建構子吃具體 `SQLiteStore`）；`SQLiteStore` 自述「thin wrapper over stdlib sqlite3 — no ORM」並**公開暴露 raw connection**（`sqlite.py:166-174`）。
- **繞道**：`cli.py:675` 雙底線挖入設 `PRAGMA query_only`；`optimization.py:496-513` duck-type 取走 connection 手寫 `IN (?)` 批查；`migrations.py:217` 依賴 `executescript` 自動 COMMIT（該語意甚至塑造了 transaction manager，`sqlite.py:206-239`）。
- **無 seam 的 legacy 家族**：`db.py`/`backfill.py`/`seed_signals.py` raw sqlite3、DDL 三處重複、`INSERT OR REPLACE`、`date('now',?)` modifier-as-data。
- **方言耦合量化**（生產碼）：13 處 `strftime('now')`、4 處 `ON CONFLICT`（graph_repo.py:119 為 dead dual-clause）、6 處 `INSERT OR REPLACE`、2 處 `AUTOINCREMENT`、41 處 `?` 佔位、10 處 PRAGMA、1 處 `BEGIN IMMEDIATE`、3 處 `date('now',?)`、`Connection.backup` API；JSON blob 全部寫入/讀回、**零 JSON1 查詢** → JSONB 移植屬機械式。
- **交易/併發**：全 repo「每列一個 `BEGIN IMMEDIATE` 交易」（唯一例外 `backfill.py:800`）；文件化單寫入者假設（`sqlite.py:20-23`）；`score_repo.lastrowid` 依賴 rowid 單調遞增（`score_repo.py:103,140,173`）。
- **路徑邊界半套採用**：`macro_history.db` 走 `phase3/paths.py`；`intelligence.db` 三處自行解析（`sqlite_store.py:45-49` 模組相對、`cli.py:1190` 硬編碼字面值、`seed_signals.py:19-20` 裸檔名）。`paths.py:32-35` 明確將 DSN/URL 留白為 future work。

## Decision

1. **維持 SQLite 作為本地開發/測試/單機營運後端，同時把 Repository Protocol 層指定為 6.3 的第一工程**：
   - 為 `SignalRepository`/`ScoreRepository`/`GraphRepository`（與 graph store 表面）定義 Protocol 介面；具體 SQLite 實作與未來 PostgreSQL 實作同時實現 Protocol。
   - 消除兩個繞道：`cli.py:675` 的 query-only 改為 store 能力旗標；`optimization.py` 批查移入 repo 介面方法。
   - `intelligence.db` 路徑解析收斂進 `phase3/paths.py`，並新增 **DSN 支援**（`FIE_DATABASE_URL`，填上 paths.py 留白的 future work）——DSN 指向 postgres 時走 PG 實作，否則維持 SQLite。
2. **PostgreSQL 為未來的 system of record**（適用性評估見報告 §7），目標拓撲：單一受管 PG instance，schemas = `market`（ex-macro_history）、`derived`（ex-intelligence）、`user`（綠地，ADR-C01）、`ops`（job model，ADR-C06）。
3. **legacy 家族先立 seam 再遷移**：`db.py` 家族在遷移期允許以 SQLite 繼續運行，僅先把 DDL 收斂單一 owner（FIR-007）並透過 repo 介面改寫查詢；其 `INSERT OR REPLACE`/`date('now',?)` 轉 PG 等價 upsert。
4. **必須保留的語意**（protected）：`score_snapshot` append-only（trigger→PG trigger/rule 重寫）、`signal_id` 確定性去重 upsert、checksummed forward-only migration 形狀、`compute_config_hash` 穩定性。
5. **parity 測試策略**：三後端 parity（InMemory ↔ SQLite ↔ PG fixture），沿用既有 `tests/phase3/test_graph_parity.py` 模式擴大；PG parity 以 Docker 中的 disposable PG 執行（CI 層級，本階段不實作）。

## Alternatives

- **A. 直接跳 PG、刪 SQLite 路徑**：違反工單 §9「preserve SQLite during transition」；也失去最快的本地測試後端 → 拒絕。
- **B. 引入 ORM（如 SQLAlchemy core）抽象**：重寫成本遠大於 Protocol 層；今日 repos 的 SQL 已很薄 → 拒絕（Protocol + 每後端一個薄實作即可）。
- **C. 維持雙 SQLite 檔案上雲（如 volume 掛載）**：單寫入者/單機假設在多容器環境破裂；`sqlite.py:20-23` 的文件化假設即為反證 → 拒絕作為生產，僅保留於開發。

## Consequences

- 6.3 的工作量有清晰量化邊界：Protocol 化 3 個 repo + 2 個繞道消除 + PG 方言改寫集中在上述耦合清單。
- `INTEGER PRIMARY KEY` rowid 語依賴（lastrowid、`ORDER BY snapshot_id DESC` tiebreak）需在 PG 用 `IDENTITY`/sequence + `RETURNING` 取代，並加測試保護 tiebreak 語意。
- migration runner 需為 PG 重寫（`executescript` → script 逐句執行於單一交易）。

## Open Questions

- PG 實作是否需要 `INSERT ... ON CONFLICT`（PG 9.5+ 語法相同）之外的差異測試矩陣；graph_repo dead dual-clause 應先清理。
- `FIE_DATABASE_URL` 的 secret 供應鏈（本階段僅設計：env 注入，不落盤）。