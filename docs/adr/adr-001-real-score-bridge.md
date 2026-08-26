# ADR-001: Real-Score Bridge

## Status

ACCEPTED — 2026-08-09

## Context

Phase 3 的評分引擎（MacroScorer、IndustryScorer、CompanyScorer）已完整實作並通過 1141+ 測試，但生產 cron 腳本（`run.sh` 等）未將 `macro_history.db` 的資料餵入 Phase 3 Pipeline。所有 pipeline-export artifact 僅包含 macro 分數（1 個 evidence handle），company 和 industry 分數為空（`company_score_count: 0`、`industry_score_count: 0`）。

根本原因：cron wrapper 腳本呼叫 `pipeline-export` 時未傳遞 `--industry` 和 `--company` 旗標。即使修正旗標，Pipeline 仍需要從 `signal_log` 讀取信號，但目前沒有任何元件將 `macro_history.db` 的資料轉為 Signal 記錄。

## Decision

建立 `phase3/bridge/seed_signals.py` 模組，作為 `macro_history.db` 與 `intelligence.db`（signal_log）之間的映射層。

### 設計

- **Macro 映射**：6 個欄位（us10y, us2y, us13w, dxy, vix, yield_spread）→ signal（entity_type=macro, entity_id=global）
- **Company 映射**：9 個欄位（pe_trailing, pb_ratio, peg_ratio, roe, earnings_growth, revenue_growth, dividend_yield, market_cap, gross_margin）→ signal（entity_type=company, entity_id=`<code>`）。跳過負 PE 值。
- **Institutional 映射**：2 個欄位（foreign_net, prop_net）→ signal（entity_type=company, source_type=t86）
- **Industry 聚合**：依 `industry_config.json` 將成分股的法人買賣超加總 → signal（entity_type=industry），僅 `capital_flow` 維度
- **As-of 解析**：`resolve_as_of_dates(source_db, requested_date)` 對每張表找出 `<= requested_date` 的最新可用日期

### 安全特性

- 純離線映射，不呼叫外部 API
- 寫入指定的 target DB，拒絕寫入 `macro_history.db`
- 冪等 upsert（by signal_id）

## Consequences

### 正面

- Pipeline 可讀到真實的 company/industry 信號，產出非預設分數
- 評分引擎的完整能力被釋放
- As-of 日期解析確保跨表時間一致性

### 取捨

- 新增 Bridge 層需要維護欄位映射邏輯
- Industry 僅 `capital_flow` 維度有資料（其他維度預設中性，為正確行為）
- macro 和 industry 分數在部分配置下仍為預設值（date-bucket mismatch）

## Validation

- `pipeline-export` 搭配 seeded DB 產出非預設 company 分數，由真實 signal node 支撐
- `tests/test_bridge_seed_signals.py` + `tests/test_real_score_bridge.py` 驗證映射正確性
- 1787 phase3 tests PASS（含 bridge 測試）

## Related

- `phase3/bridge/seed_signals.py`
- `tests/test_bridge_seed_signals.py`
- `tests/test_real_score_bridge.py`
- Commit: `a635ccc`