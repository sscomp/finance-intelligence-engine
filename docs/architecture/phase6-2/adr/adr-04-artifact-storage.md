# ADR-C04: Artifact / Object Storage

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

今日的產出 artifact 全部是本機檔案（Phase 6.2 盤點 §2）：

- `metadata/reports/artifacts/<label>.intelligence_report.{json,md}`（pipeline-report/pipeline-export 寫入；gitignored）——檔名覆寫同 label、無版本、無自動留存；`ReportArtifact` 附 sha256。
- `logs/<date>.json` 與 `logs/company-<date>.json`、`logs/industry-<date>.json`（legacy 三腳本每日覆寫的 debug dump）。
- `metadata/tasks|reports` 的 JSON 檔 store（run_report.py，opt-in；原子寫 mkstemp+fsync+os.replace）。
- Wrapper 的 per-run seed DB（`/tmp/macro-report-intelligence/<date>-*.db`，**從不清理**）。
- pipeline artifacts 同時是 shadow-run/portfolio-shadow-run 的重放輸入（`shadow_run.py:52`）——即 artifact 兼任「重放的確定性輸入」。

多容器雲端環境中本機檔案不可共用、不可耐存；部分 artifact 很大（JSON 含 evidence handles），但查詢時只需按 label/run_id 取回。

## Decision

1. **報告/輸出（Category E）走 S3 相容物件儲存**（S3 API 為界面標準，供應商中立）：`.intelligence_report.json/.md`、recovery 報告、未來 attachments。
   - **關聯式（derived schema）只留索引列**：`artifact_id`、job/run_id、label、kind（json/md/csv）、object key、content sha256、size、as_of、engine SHA、建立時間——查詢/列表/權限都在關聯式端，內容在物件端。
   - **命名版本化**：`<run_id或job_id>/<label>[.<attempt>]/<file>`；同 label 重跑不覆寫（解決今日「新檔案 per run_label 或覆寫」的含糊），並直接支援 shadow-run 重放需要「原始 artifact 位址」的需求。
   - **完整性**：寫入時計 sha256 存入索引列；讀取時驗 hash。
   - **留存政策**：intellectual artifact 預設永久（量小：每日 2 檔 + 每週 2 + 每月 2，見 §11 workload）；`logs/<date>.json` 类 debug dump 定義為 **ephemeral**（30 天或更短），不入物件儲存的長期層。
2. **本機檔案路徑保留為開發/測試的 artifact backend**：`phase3/paths.py artifact_dir()` 語意不變，未來加 `FIE_ARTIFACT_STORE`（`local`|`s3` 風格 DSN），預設 `local`——與 ADR-C03 的雙後端模式同構。
3. **Wrapper 的 per-run seed DB 不物件化、也不入關聯式**：改為 job 模型下的 scoped run（每 run 寫 run_id 標記的 derived 列，或 disposable Postgres schema/database per run 於雲端排程中）——本階段僅決策方向，實作屬 6.4。

## Alternatives

- **A. 全部塞進 PostgreSQL（大 JSONB/TEXT 欄）**：量雖小，但 blob 進行式資料庫違反「relational 只留查詢面」原則、備份/還原成本升高、未來附件類型會再長大 → 拒絕。
- **B. 維持 host volume/NFS**：多 replica 一致性與備份複雜度 → 拒絕作為生產。
- **C. 不儲存 artifact、即丟**：破壞 shadow-run 重放與審計需求 → 拒絕。

## Consequences

- `reporting.py` 的寫入路徑需抽 artifact sink 介面（與 SnapshotSink 同構）；shadow-run `--artifact` 改吃 object reference 或本機路徑雙軌。
- 索引列與物件本體一致性成為新的失敗模式（孤兒物件/孤兒索引列）——以「先寫物件、後寫索引列」的順序 + 對帳工具處理（6.3+）。

## Open Questions

- 物件儲存供應商選擇隨雲端平台決定（報告 §20-21）；本 ADR 只定界面（S3 API）與索引 schema 草案。
- `.hermes` task store（外部排程器的 JSON store）是否上移物件儲存——取決於 Hermes 排程器本身是否被雲端 job runner 取代（ADR-C06）。