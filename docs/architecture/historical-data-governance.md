# 歷史資料治理

## 概述

本文檔定義 FIE 的歷史資料保留、新鮮度、回填與品質管理原則。

## 資料保留

### macro_history.db

- **寫入模式**：`INSERT OR REPLACE`（冪等 upsert by PK）
- **刪除/修剪**：無。`db.py` 不包含任何 `DELETE` 語句
- **保留策略**：append-only，資料只增不減

### intelligence.db（Phase 3 持久層）

- **signal_log**：`retention.py` 提供顯式清理工具
- **graph_edges**：`retention.py` 提供顯式清理工具
- **score_snapshot**：append-only，不提供清理（保留完整歷史快照供重放）

## 新鮮度與過時判定

`phase3/freshness.py` 實作交易日感知的新鮮度分類：

| 狀態 | 含義 | 建議 |
|------|------|------|
| FRESH | 資料日期在可接受範圍內 | 正常使用 |
| STALE | 超過正常更新週期 | 警告，建議更新 |
| HARD_EXPIRED | 嚴重過時 | 不建議使用 |

### 交易日曆

`config/holidays.json` 包含台股國定假日（2026 年 12 個日期）。新鮮度判定考量假日，避免在非交易日誤判為過時。

### CLI 整合

`pipeline-run --freshness-check` 旗標在執行時自動檢查資料新鮮度，過時資料會產生警告但不阻止執行。

## 歷史回填策略

### 回填工具

`phase3/backfill.py` + `backfill` CLI 提供安全的歷史資料回填：

1. **Gap 偵測**：比對 source DB 與 target DB，識別缺失的日期範圍
2. **Dry-run**：`--dry-run` 旗標僅報告計畫，不寫入
3. **執行**：`--execute` 旗標實際寫入，但僅寫入指定的 target DB
4. **安全守護**：拒絕寫入 `macro_history.db`（case-insensitive basename refusal）

### 回填就緒狀態

| 來源 | 狀態 | 說明 |
|------|------|------|
| macro_daily | READY | `fetch_macro_range(start, end)` 支援顯式日期範圍 |
| institutional_daily | READY | `fetch_institutional_range(start, end)` 透過 T86 API 逐日擷取 |
| stock_monthly | BLOCKED | yfinance `.info` 僅提供當前快照，無法取得歷史 |
| industry_derived | BLOCKED | 依賴 stock_monthly 的歷史資料 |

### 回填執行條件

生產回填的 GO/NO-GO 條件：

1. 操作者明確授權
2. M8 Day-7 sign-off 完成（回填不得干擾營運驗收）
3. 執行前備份 source DB
4. 執行時重新驗證 dry-run 結果

## 資料品質

### As-of 語意

`resolve_as_of_dates` 確保跨表時間對齊：對每張表找出 `<= requested_date` 的最新可用資料。所有信號以 `requested_date` 作為 `date_bucket`，原始來源日期保留在 metadata。

### 冪等性

所有寫入操作為冪等（`INSERT OR REPLACE` by PK），重複執行不會產生重複資料或副作用。

## 相關文件

- [ADR-002: Freshness/Staleness Guards](../adr/adr-002-freshness-staleness-guards.md)
- [ADR-003: Historical Backfill Policy](../adr/adr-003-historical-backfill-policy.md)
- [資料供應鏈](data-supply-chain.md)