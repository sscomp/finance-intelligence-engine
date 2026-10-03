# ADR-C02: Source-of-Truth / Derived Data Policy（macro_history.db 與 intelligence.db 的職責）

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

FIE 有兩套互不相連的 SQLite 持久棧（Phase 6.2 耦合審計 §1-2；6.0 audit §9）：

- **`macro_history.db`**（legacy 家族）：raw `sqlite3`、無抽象層。3 張表（`macro_daily` PK `date`；`stock_monthly`、`institutional_daily` PK `(date, code)`），全部 `INSERT OR REPLACE` 最新值覆寫、無 DELETE、無索引（僅 PK 隱式）、無 retention。生產者是三支 legacy fetch 腳本（macro_daily.py、company_monthly.py、institutional.py 資料經 company_monthly.py 寫入）；消費者是 `db.get_*` 查詢與 `phase3/bridge/seed_signals.py` 的 seed。
- **`intelligence.db`**（Phase 3B store）：repos + `SQLiteStore`（WAL、BEGIN IMMEDIATE、checksummed forward-only migrations）。表：`signal_log`（UPSERT by 確定性 `signal_id`）、`score_snapshot`（**trigger 強制 append-only**、無 purge 助手）、`graph_nodes`/`graph_edges`（內容派生 id、UPSERT 冪等）、`adapter_run_log` 與 `ingestion_errors`（**placeholder，無生產寫入者**）、`schema_migrations`。

工單 §8 明示：不得只建議「merge both into PostgreSQL」，必須先確定各自職責。

## Decision

**兩庫不視為重複，而是供應鏈上下游，各自保留職責後再進入同一個受管 PostgreSQL（不同 schema、不同可變性/留存政策）：**

| | `macro_history.db`（→ `market` schema） | `intelligence.db`（→ `derived` schema） |
|---|---|---|
| 職責 | **原始市場觀測的耐久檔案**（external fetch 的 source of truth） | **確定性衍生物 + 審計軌跡**（由 market 經 adapter/bridge 派生） |
| source of truth | YES（對已 fetch 的觀測值而言；上游為 Yahoo/TWSE/RSS） | NO——可從 market + engine 版本 + config 重算（但見下方保留理由） |
| 可變性 | 語意保留：natural-key upsert（latest-wins 快照）；無 per-版本歷史（今日行為即如此） | `signal_log` upsert 冪等；`score_snapshot` append-only 審計（**不可捨棄 trigger 語意**） |
| 遺失時 | **不可再生**——yfinance `.info` 僅當前快照、T86/RSS 窗口會縮，歷史值一旦遺失無法重取 | 可重算/重建（ADR-C04 衍生物遷移政策） |
| retention | 無限期（量小：~1 row/交易日 + 每月 50×2） | signal/graph 有顯式 purge 助手；snapshot 永不剪 |

### 衍生物分級（AUDIT_REQUIRED / MUST_PERSIST / RECOMPUTABLE / CACHEABLE）

- `score_snapshot`：**MUST_PERSIST + AUDIT_REQUIRED**——append-only 審計軌跡；理論可重算，但僅在「同 engine 版本 + 同 config」下可重現，作為決策稽核不應依賴重算。
- `signal_log`：**MUST_PERSIST + RECOMPUTABLE（次要）+ AUDIT_REQUIRED（次要）**——seed 屬確定性映射；保留是為了審計與避免重 fetch。
- `graph_nodes`/`graph_edges`：**RECOMPUTABLE + CACHEABLE**——id 為內容派生（`graph_writer.py:20-30`），重跑冪等；遷移時 RECOMPUTE-then-VERIFY 為主選項，MIGRATE 為連續性保險（逐項政策見報告 §24）。
- `adapter_run_log` / `ingestion_errors`：**AUDIT_REQUIRED**（但今日為 placeholder、0 列）——雲端 job 模型（ADR-C06）落成時才是真正寫入者。
- `schema_migrations`：各環境自有的 schema 狀態，不遷移。

## Alternatives

- **A. 直接併成一張大表/單 schema**：違反工單要求、且把「不可再生的歷史觀測」與「可再生的衍生物」混在一份政策裡（retention、mutability、驗證策略全部衝突）→ 拒絕。
- **B. 全部捨棄重算**：macro_history 不可再生；score_snapshot 的審計價值丟失 → 拒絕。
- **C. 兩庫各自上雲成兩個資料庫**：額外成本與跨庫一致性查詢（bridge 需要 cross-DB SELECT；seed_signals.py:113-347 今日即為 raw cross-DB by path）→ 拒絕；單 instance 內的 schemas 保持 bridge 查詢可移植性。

## Consequences

- Bridge 查詢從 cross-file SQLite SELECT 改成同 instance 的 cross-schema SELECT——語意不變、耦合下降。
- `macro_history.db` 的 basename 路徑守護（`FORBIDDEN_DB_NAME`）在 DB 世界失效，需以 roles/grants/DSN 政策取代（6.0 FIR-012，見 ADR-C03）。
- 兩棧 schema 各宣告三次（`db.py`、`backfill.py:810-857`、test fixture）→ 遷移前先單一 schema owner（6.0 FIR-007）。

## Open Questions

- `macro_daily.signals_json`（legacy blob）在 `market` schema 是否保留為 JSONB 或拆解。
- score_snapshot 若未來需要 per-run 去重（6.0 FIR-006 rerun 疊加問題），政策上以 job/run_id 元資料標記而非修改 append-only 語意。