# ADR-004: M8 Observation R3 Validation Semantics

## Status

ACCEPTED — 2026-08-18 (修正)，2026-08-21 (nested field fix)

## Context

M8 營運驗收的 R3 檢查驗證 "jobs.json 不變性"——即 M8 觀測腳本不應修改排程器配置。原始實作使用 whole-file SHA-256 比較：

```python
r3 = "PASS" if jobs_sha_before == jobs_sha_after else "FAIL"
```

但 `jobs.json` 是共享排程器狀態檔，任何並發 cron job 觸發都會更新 volatile 執行時欄位（`last_run_at`、`next_run_at`、`state`、`fire_claim` 等）和頂層 `updated_at` 時間戳。這導致 R3 在大多數工作日 false-positive FAIL，即使 M8 觀測腳本完全沒有寫入 jobs.json。

## Decision

改用 **scoped signature** 比較：計算 jobs.json 的簽名時，排除 volatile 執行時欄位，僅比較語意/配置欄位。

### 排除的 volatile 欄位（12 個）

- `last_run_at`, `last_status`, `last_error`, `last_delivery_error`
- `next_run_at`, `state`, `fire_claim`
- `paused_at`, `paused_reason`
- `schedule_display`, `model_snapshot`, `provider_snapshot`
- 頂層 `updated_at`

### 嵌套欄位排除（2026-08-21 追加）

`repeat` 欄位是一個 dict，其中 `completed` 計數器在每次 cron 觸發時遞增。後續發現 `repeat.completed` 也是 volatile，需要遞迴排除。

### 實作

`capture_jobs_signature()` 函數讀取 jobs.json，遞迴移除 volatile 欄位（含嵌套），對剩餘的語意內容計算 SHA-256。R3 比較 scoped signature before/after。

## Consequences

### 正面

- R3 能正確區分「cron 正常活動」與「M8 觀測導致的語意變更」
- 不再因並發 cron 觸發而 false-positive
- Day-2 被重新判定為 PASS（原本因 whole-file SHA 不同而 FAIL）

### 取捨

- 簽名計算邏輯更複雜
- 需要維護 volatile 欄位清單（如未來排程器新增 volatile 欄位，需同步更新）
- 嵌套欄位需要遞迴處理

## Validation

- 27 targeted tests PASS（`tests/phase3/test_m8_r3_scoped_validation.py`）
- Day-2 re-adjudicated: FAIL → PASS（版本化記錄附加，原始保留）
- Day-3 scheduler ready

## Related

- `tests/phase3/test_m8_r3_scoped_validation.py`
- Commit: `347a828`
- [M8 里程碑](../governance/milestones/m8.md)