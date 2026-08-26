# ADR-003: Historical Backfill Policy

## Status

ACCEPTED — 2026-08-10

## Context

`macro_history.db` 的歷史覆蓋很淺：`macro_daily` 僅 33 個交易日（2026-06-18 至 2026-08-07），`stock_monthly` 和 `institutional_daily` 僅 3 個月度快照。建立有意義的歷史視窗需要回填，但回填操作必須不損壞生產 DB、不造假資料、且可審計。

## Decision

### 回填工具

實作 `phase3/backfill.py` + `backfill` CLI，提供：

1. **Gap 偵測**：比對 source DB 與 target DB，識別缺失的日期範圍
2. **Dry-run**：`--dry-run` 僅報告計畫，不寫入
3. **Execute**：`--execute` 實際寫入，但僅寫入指定的 `--target-db`
4. **Source 分類**：4 個 source class（macro_daily、stock_monthly、institutional_daily、industry_derived）

### 安全守護

- **生產 DB 拒絕**：target DB 的 path 如果解析為 `macro_history.db`（case-insensitive basename），CLI 拒絕執行
- **臨時 DB 隔離**：回填永遠寫入臨時/測試 DB，不直接修改生產資料
- **顯式授權**：需要 `--execute` 旗標，預設為 dry-run

### Fetch Range Enablement

`macro_daily.py` 的 `fetch_indicators()` 擴充為支援 `start_date` / `end_date` 參數（yfinance `Ticker.history(start, end)`），保留 legacy `period="5d"` 行為。

### 回填就緒分類

| Source | Status | Reason |
|--------|--------|--------|
| macro_daily | READY | `fetch_macro_range(start, end)` 支援顯式日期範圍 |
| institutional_daily | READY | `fetch_institutional_range(start, end)` 透過 T86 API 逐日擷取 |
| stock_monthly | BLOCKED | yfinance `.info` 僅提供當前快照，無法取得歷史 fundamentals |
| industry_derived | BLOCKED | 依賴 stock_monthly 的歷史資料 |

### 生產回填條件

回填的 GO/NO-GO 條件：

1. 操作者明確授權
2. M8 Day-7 sign-off 完成
3. 執行前備份 source DB
4. 執行時重新驗證 dry-run 結果

## Consequences

### 正面

- 安全的回填路徑，不會意外損壞生產 DB
- Gap 偵測讓使用者知道缺什麼
- Dry-run 允許事前審查

### 取捨

- `stock_monthly` 歷史回填無法自動化（yfinance API 限制）
- 生產回填需要人工授權，無法全自動
- T86 API 逐日擷取可能觸發 rate limit

## Validation

- 68 targeted tests PASS（`tests/phase3/test_backfill.py`）
- 108 regression tests PASS
- 37 fetch range tests PASS（`tests/phase3/test_fetch_range.py`）
- `macro_history.db` SHA-256 不變

## Related

- `phase3/backfill.py`
- `phase3/cli.py`（`backfill` 子命令）
- `macro_daily.py`（fetch range enablement）
- `tests/phase3/test_backfill.py`
- `tests/phase3/test_fetch_range.py`
- Commits: `4aa53fa`（backfill tooling）、`4aa53fa`（fetch range enablement）