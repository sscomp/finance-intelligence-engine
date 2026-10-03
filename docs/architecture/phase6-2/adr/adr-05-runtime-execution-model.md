# ADR-C05: Runtime Execution Model

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

今日執行流（Phase 6.2 盤點 + 6.0 §8/§12）：外部排程器（Hermes）→ wrapper（run*.sh）→ legacy fetch 腳本（唯一具網路碼的 4 個呼叫點）→ `macro_history.db` → bridge seed（cross-DB SELECT）→ Phase 3 engine（in-process deterministic：Adapters → Signals → Scoring → Evidence/Graph → Pipeline artifact）→ artifact 檔案。六種責任全部在同一進程內串行，以檔案/DB 交接。

工單 §13 要求把執行流映射成雲端 runtime 責任，避免不必要的微服務。

## Decision

**三個 runtime 形態，不多做：**

| # | 形態 | 內含責任 | 觸發 |
|---|---|---|---|
| 1 | **batch worker（同一容器）** | 資料擷取、正規化、signal generation、scoring、evidence graph、portfolio evaluation、report generation——即今日 pipeline-run/pipeline-export 的 in-process 流程 | 排程（ADR-C06）或 API 起動的短 job |
| 2 | **API service（輕量 HTTP 層）** | API 邊界（報告 §14）的 read/write 端點：讀 model 直查 derived/user schema；寫請求轉為 job（非同步）；deterministic explain/evidence 查詢同步處理 | 常駐或 scale-to-zero |
| 3 | **scheduler/orchestrator** | 觸發 batch job、記錄 job 狀態、重試與去重 | 雲端 cron/排程服務 |

- **責任分類**：ingestion=scheduled batch；normalization+signal generation+scoring+graph=scheduled batch（同 job 內連續段）＋ on-demand 短 job（手動重跑/ChatGPT 請求的分析）；portfolio evaluation=batch + API 同步讀（風險指標）；report generation=batch；API serving=request/response；scheduled orchestration=排程器；health/observability=job 表 + 排程器狀態（見 ADR-C06）。
- **明確不做的事**：不拆 per-stage 微服務（今日流程是單進程 in-process 確定性管線，拆開只引入 IPC/一致性問題）；不引入訊息佇列（job 表即佇列語意，量級遠不需要）；不把 legacy fetch 與 scoring 拆成不同容器（保持 adapter 離線設計原則與「engine 無網路碼」邊界）。**取數與評分在同一 job 內完成**——phase3 引擎零網路碼（6.0 §11 驗證：網路呼叫僅存在 legacy 4 呼叫點）意味著取數是唯一需要 egress 的容器責任。
- **確定性引擎邊界原樣保留**（6.0 §14 VERIFIED：LLM-free scoring、排序確定性、config_hash）：API 解釋/敘事屬未來 ChatGPT 端（ADR-C08），不進 job。
- **容器化目標**：單一 FIE image（pyproject 安裝 + engine + CLI），batch 與 API 用同一 image 不同 entrypoint。Phase 6.1 已證明任意路徑 + `FIE_*` env 可移植——容器僅是把它包起來（Phase 6.1 報告 §22 建議 4）。

## Alternatives

- **A. 每階段一個服務（fetch/score/report/API 分離）**：五個 deploy 單元只為一個每日 ≤ 數分鐘的工作負載；營運負擔不成比例 → 拒絕（工單:「prefer the smallest operational architecture」）。
- **B. 事件驅動流（adapter 完成即觸發下游）**：需要佇列基礎設施與 at-least-once 專門處理；資料以日為粒度、事件優勢為零 → 拒絕。
- **C. 一直維持 Hermes 排程器 + A3**：違反 §24 no-machine gate → 拒絕作為生產。

## Consequences

- batch worker 的一次執行即「ingest + seed + score + persist + export」，與今日 `run*.sh` 語意相同——遷移可逐步驗證（先 batch 後 API）。
- API service 的寫入端點一律轉 job（非同步），讀端點直查——避免 API 進程內執行長評分。
- 容器內不應再假定 WAL SQLite 的單寫入者為「本機檔案」——受管 PG 接手（ADR-C03）。

## Open Questions

- API service 進程數與 autoscaling 上限（初期 1 即可；ChatGPT 流量的併發未證明需要 >1）。
- on-demand analysis job 的同步等待上限（建議 API 直接查 job 狀態，不長連線）。