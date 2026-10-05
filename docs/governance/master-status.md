# FIE 系統狀態總覽

> **⚠️ CURRENT-STATE NOTICE（2026-10-05）**：以下快照之日期為 **2026-08-26**（Phase 5
> 時期的狀態快照；保留原狀作為里程碑歷史）。**目前生產現況**（Phase 6.7B/6.8A 之後）：
> **Production backend = PostgreSQL**（2026-10-05 controlled cutover accepted）；
> **SQLite = rollback/recovery source only**；runtime DB target 一律 explicit + fail-closed
> （Phase 6.8A canonical contract）；**Codex Cloud = NOT IMPLEMENTED**（僅 readiness 文件）。
> 詳見 [PostgreSQL Production Architecture](../architecture/postgresql-production-architecture.md)
> 與 [production baselines](../production/ABACUS_FIE_6_7B_PRODUCTION_BASELINE.md)。

> **快照日期**：2026-08-26
> **快照時當前階段**：Phase 5 工程完成 — M8 營運驗收進行中

## 里程碑狀態

| 里程碑 | 內容 | 狀態 | Gate |
|--------|------|------|------|
| Phase 3A | 基礎架構（信號、評分、持久層） | 完成 | — |
| Phase 3B | 配接器、圖譜、端到端整合 | 完成 | — |
| Phase 4 Task 1B | 圖譜持久化（SQLiteGraphStore） | 完成 | F1 |
| Phase 4 Task 2 | 查詢優化基礎（BatchReader） | 完成 | — |
| Phase 4 Task 3A | Sentinel 基準清理 | 完成 | — |
| Phase 4 Task 3B | 分數解釋 CLI | 完成 | — |
| Phase 4 Task 4 | Shadow-Run 決策重放 | 完成 | — |
| Phase 4 Ops A1-A3 | Pipeline artifact 匯出、Cron 註冊、7 日 shadow | 完成 | — |
| Phase 5 M2 | 投資組合領域模型 | 完成 | G-P5-2 |
| Phase 5 M3 | 風險引擎（S1 DTO / S2 集中度+回撤 / S3 相關性+風險預算） | 完成 | G-P5-3 |
| Phase 5 M4 | 配置引擎（S1 DTO / S2 score-weighted / S3 risk-aware + CLI） | 完成 | G-P5-4 |
| Phase 5 M5 | 執行規劃 | 完成 | G-P5-5 |
| Phase 5 M6 | 投資組合報告 | 完成 | G-P5-6 |
| Phase 5 M7 | 工程完成驗證 | 完成 | G-P5-7 |
| Phase 5 M8 | 營運驗收（7 日 shadow-run） | 進行中 | G-P5-8 |
| Phase 5 M9 | 生產就緒 | 待定 | G-P5-9 |

## 測試狀態（2026-08-26 快照）

| 測試層級 | 結果 |
|----------|------|
| Phase 3 套件 | 1787 tests PASS |
| 全部測試（含 Phase 2B + bridge） | 1961 tests PASS, 2 skipped |

> 測試數量為特定時間點的 snapshot，非永久宣告。

## FIE 資料治理進度

| 任務 | 內容 | 狀態 |
|------|------|------|
| T-1 | 新鮮度/過時 metadata + 守護 | 完成 |
| T-2 | 歷史回填工具 | 完成 |
| T-2.1 | Macro 歷史 fetch range enablement | 完成 |
| T-3A | Macro + Institutional 回填就緒/dry-run 計畫 | 完成（CONDITIONAL GO） |
| T-3B | 生產回填執行 | 待授權 |
| T-4 | Company 歷史回填 | BLOCKED（yfinance API 限制） |

## 已知限制

1. **Industry 評分部分覆蓋**：僅 `capital_flow` 維度有本地權威資料來源（法人買賣超聚合），其他維度預設為中性
2. **Company 歷史回填**：yfinance `.info` 僅提供當前快照，無法取得歷史 fundamentals，已標記為 BLOCKED
3. **無前端 UI**：所有操作透過 CLI
4. **無券商整合**：不包含交易執行功能
5. **尚未宣告開源授權**