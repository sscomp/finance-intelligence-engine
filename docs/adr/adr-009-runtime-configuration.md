# ADR-009: Cloud-Safe Runtime Configuration（Phase 6.5 工單 ADR-05）

## Status

ACCEPTED — 2026-10-03

## Context

Phase 6.3 已有 `FIE_DATABASE_URL` 環境變數與 `resolve_spec()` 的 explicit > env > default 優先序，但服務/批次執行時還需要資料目錄、artifact 目錄、執行 profile 與日誌格式 — 且在雲端（Codex Cloud 或任何 provider）執行時：

- [VERIFIED] 開發機專屬絕對路徑（舊部署使用者的 home 目錄、A3 hostname、
  部署容器路徑）**不得**出現在任何預設值、程式碼或追蹤檔案中 —
  可攜性審計（`tests/phase3/service/test_runtime_config.py`）逐字串掃描
  程式碼與 Phase 6.5 文件（工單 §10、§20）；
- 憑證只由環境注入，**不得**寫入 repo、不得出現於日誌或錯誤訊息（§14）。

## Decision

[DECISION] `phase3.service.runtime_config` 提供單一 `load_runtime_config()` 合約：

| 變數 | 意義 | 缺省 |
|---|---|---|
| `FIE_DATABASE_URL` | Phase 3B store 目標 | portable SQLite default |
| `FIE_DATA_DIR` | 檔案系統資料根（僅 macro_history layer 等明確支援 FS 的層） | `phase3.paths` 可攜預設 |
| `FIE_CONFIG_DIR` | 設定覆蓋根 | 跟隨 `FIE_DATA_DIR` |
| `FIE_ARTIFACT_DIR` | 報告 artifact 輸出 | `phase3.paths` 可攜預設 |
| `FIE_SERVICE_ENV` | `local`/`test`/`staging`/`production`（6.5 僅診斷用途） | `local` |
| `FIE_LOG_FORMAT` | `structured`/`plain` | `structured` |

優先序（documented + tested）：**explicit argument > environment > 可攜安全預設** — 與 `resolve_spec()` 相同形狀。

### 機密處理

- `ServiceRuntimeConfig.database_spec` 只以**消毒後**形式存在（`sanitize_db_url()` 密碼遮罩）；原始 DSN 只存活在 `resolve_spec()` 與 backend 的開連線路徑，永不進 config 邊界。
- `redact()` 對任何診斷文字做 URL/path 消毒（§5.1 同族規則）。
- [VERIFIED] 測試含密碼的 PostgreSQL DSN 走過 `load_runtime_config().to_dict()` 後僅出現 `:***` 遮罩形式，且錯誤訊息中的 URL/path 被消毒。

## Consequences

### 正面

- 環境佈建 = 6 個環境變數，全部 provider-neutral；Codex Cloud/任何容器都可填。
- config 的 log/report 檢視（`to_dict()`）天然 secret-safe，可進 observability（§15）。
- 執行 profile 為 Phase 6.6 的行為差異（如 staging 連複本）預留掛點，現階段零行為分歧。

### 取捨

- [OPEN] `FIE_CONFIG_DIR` 目前覆蓋點極少（本地 config 檔案讀取仍以 repo config 為主）；完整外部化設定屬 Phase 6.6+。
- [DECISION] 不引入 dotenv/秘密管理 SDK；秘密注入是執行環境的責任（§1.6 vendor neutrality）。