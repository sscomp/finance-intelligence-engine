# ADR-C06: Scheduler / Job Model

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

今日排程完全外置：Hermes cron agent 執行 `config/reports/*.yaml` 宣告的 `cron_expr`（macro_daily `30 8 * * 1-6`、company_monthly 月 12 日、industry_weekly 週一、constituents_quarterly 季初——後者無 script，是人工瀏覽器步驟）。已知缺陷（6.0 §12）：TZ/主機耦合（`institutional.py:23` naive host-local now）、假日照問不在累積器、`score_snapshot` 無重跑去重（FIR-006，同日重跑疊加 snapshot）、per-run seed DB 永不清理、run id uuid4 無狀態可查。工單 §14 要求雲端排程設計但**不**建生產 jobs。

## Decision

1. **排程器 = 雲端原生 cron → 觸發 batch job，job 狀態以關聯式表（`ops.jobs`）為系統記錄**：
   - 明確 `TZ=Asia/Taipei`（排程判定），執行器以 `config/holidays.json` 做交易日閘門——非交易日 job 直至 `SKIPPED_NON_TRADING_DAY`（此工具已存在：`phase3/freshness.py` 的假日與交易日邏輯，唯一缺口是 legacy 累積器未用它，FIR-010）。
   - **idempotency**：natural-key upsert（既有語意）+ `signal_id` 確定性去重；**duplicate-run 防護**：`job_type + as_of + status IN(active)` 的唯一約束——第二個同日 job 在插入即失敗。
   - **retry**：指數退避、上限 3 次（供應商層）+ 應用層對 fetch 階段的 per-source 重試（6.0 FIR-013 的 serial-50-calls 風險需 batching/backoff，屬 6.4 實作）。
   - **run IDs**：沿用 uuid4 run_id + 內部確定性 id；`parent_run_id` 支援重跑血緣（Hermes 已有此概念，`parent_task_id`）。
   - **safe rerun/backfill**：rerun = 新 run_id、寫入仍冪等；backfill 只寫臨時 DB 的既有契約保留。
2. **job 記錄欄位（報告 §13 全欄位清單）**：`job_id`（uuid4 PK）、`job_type`（daily_macro/weekly_industry/monthly_company/on_demand_analysis/manual_rerun）、`req`uested_at/started_at/finished_at、`status`（ADR-C07 狀態模型）、`as_of`、`freshness_summary`（JSON）、`error_summary`（JSON）、`run_id`、`engine_version`（git SHA + config_hash）、`artifact_refs`（物件儲存索引 FK，ADR-C04）。**job state 屬於關聯式持久化**——`ops.jobs` schema——因為它需要唯一約束去重、審計查詢與多容器一致讀。
3. **`adapter_run_log` 落成**：Phase 3B 的 placeholder 表（0 列生產寫入者）自然成為 job 內 per-adapter 記錄，與 ops.jobs 呼应——不再需要新表。
4. **本階段僅設計**；不建任何 scheduler job、不寫 systemd/cron、不改 Hermes。

## Alternatives

- **A. 維持 Hermes 瀏覽器 agent 排程**：不可上雲、人工步驟不可審計、TZ/主機耦合 → 拒絕作為生產（可作為遷移期平行觀察）。
- **B. job 狀態放檔案（如 jobs.json）**：多容器併發、唯一約束與審計查詢都無解 → 拒絕。
- **C. workflow 引擎（Airflow/Temporal 等）**：3 個 daily/weekly/monthly job 的工作負載遠低於其營運成本 → 拒絕（重估條件：on-demand 分析量或依賴鏈複雜化）。

## Consequences

- FIR-006（同日重跑 snapshot 疊加）在建議的 job 模型下以「去重約束 + as_of 狀態查詢」阻擋，不需要改 append-only 語意。
- 季度 constituents_quarterly 的人工瀏覽器步驟**不被雲端 job 模型涵蓋**——保留為人工程序或未來標記為手動 job_type；這是唯一不直接自動化的既有 job。

## Open Questions

- 排程器產品選擇隨平台決定（報告 §20/21）；要求：支援 TZ、retry、可觀察執行、手動觸發、低成本（工單 §22）。
- job 表是否需要 per-user 維度（on_demand_analysis 屬 user 請求）→ user domain FK 可選，6.6 再定。