# ADR-002: Freshness/Staleness Metadata Guards

## Status

ACCEPTED — 2026-08-10

## Context

FIE 在開始歷史資料治理之前，沒有統一的新鮮度/過時定義。每個 cron job 有隱含的更新週期（macro daily 08:30、company monthly 12 日、institutional 依 T86 cycle），但沒有顯式的 "stale" 或 "hard-expiry" 判定。使用者無法在執行 Pipeline 前知道資料是否過時。

## Decision

實作 `phase3/freshness.py` 純模組，提供：

### FreshnessStatus 列舉

- `FRESH`：資料日期在可接受範圍內
- `STALE`：超過正常更新週期
- `HARD_EXPIRED`：嚴重過時

### 交易日感知

`config/holidays.json` 包含台股國定假日（2026 年 12 個日期）。新鮮度計算扣除非交易日，避免在假日或國定假日誤判為過時。

### FRESHNESS_POLICY

定義每種 entity_type 的可接受天數閾值。

### CLI 整合

`pipeline-run --freshness-check` 旗標在執行時自動檢查，過時資料產生警告但不阻止執行（advisory，非 blocking）。

### Bridge 整合

`seed_signals.py` 的 4 個 seed 函數接受 `requested_date` 和 `holidays` 參數，在映射時附加 freshness metadata 到信號記錄。向後相容：參數為 None 時不附加。

## Consequences

### 正面

- 使用者可在執行前知道資料新鮮度
- 假日感知避免誤判
- advisory 模式不阻擋緊急使用

### 取捨

- 需要維護 `config/holidays.json`（每年更新）
- 閾值為 config 驅動，需要定期校準

## Validation

- 80 targeted tests PASS（`tests/phase3/test_freshness.py`，19 個測試類別）
- 28 regression tests PASS
- `macro_history.db` SHA-256 不變
- M8 Day-1 影響：non-blocking

## Related

- `phase3/freshness.py`
- `config/holidays.json`
- `phase3/bridge/seed_signals.py`（freshness enrichment）
- `tests/phase3/test_freshness.py`
- Commit: `a635ccc`