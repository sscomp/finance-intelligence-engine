# ADR-C07: Freshness / Partial-Failure Semantics

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

- 既有行為（6.0 §11/§12 VERIFIED）：全部 4 個取數點**靜默部分失敗**（per-ticker error stub、T86 日期失敗 return None、RSS 死 feed return []）→ report 照發、exit 0；freshness 分類引擎（FRESH/STALE/HARD_EXPIRED，`phase3/freshness.py`）完整但 Phase 6.1 才接進 wrapper（warnings-only、不改 exit code）；`config/policies/retry.yaml` 記載無人載入的重試政策。
- Phase 6.1 已確立：不把 freshness warnings 轉硬失敗；成功不等於資料新鮮；artifact 內帶 warnings/errors 陣列。
- 工單 §15 明示：不自動轉硬失敗，要定義顯式營運狀態模型與 per-class 政策；不改投資語意。

## Decision

### 1. 營運 run 狀態模型（取代單一 exit 0/1）

| 狀態 | 定義 | exit code（未來 job 層） |
|---|---|---|
| `SUCCESS` | 全部 critical 資料新鮮且齊 | 0 |
| `SUCCESS_WITH_WARNINGS` | 成功，含非關鍵 warnings（既有 warnings-only 語意） | 0 |
| `DEGRADED` | 一定比例/某些非關鍵來源缺或過期——**報告仍發布**，provenance/freshness warnings 顯式標記 | 0（發布） |
| `STALE_DATA` | 所有關鍵輸入都過期（HARD_EXPIRED 等）——**報告可發布但必須攔截自動派送** | 0 + 阻擋 publisher |
| `PARTIAL_DATA` | 部分 entity/leg 無信號（如 golden run 的 3 條 "no signals observed"） | 0 |
| `FAILED` | 管線本身崩潰（typed error） | 非 0 |

- 對應工單的架構問題——**「TWSE 延遲但 macro/company 有效時，早報應否整體失敗？」——建議：不失敗，發布 DEGRADED 報告**。理由：(a) 既有使用者體驗是每日定時收到報告（Hermes 派送），整體失敗會把「缺一項 20 日累積快照」變成「零報告」，資訊損失更大；(b) FIE 的證據鏈設計（每個 score 帶 sources/freshness）使「降級但可解釋的報告」本身就是對的產品形態；(c) 風險點不在發布，而在**派送端**——所以把硬性防線放在「STALE_DATA 攔截 publisher」而非管線失敗。
- 注意現狀缺口：legacy 累積器把「T86 連線失敗」與「當日零買賣超」壓成同一表示（unparsed→0、失敗→縮窗），DEGRADED 分類需要 fetch 層先報告「哪些 source 這次失敗/過期」——`adapter_run_log`（ADR-C06）+ freshness 摘要是落點。fetch 層重試/去靜默屬 6.4 實作（FIR-004/013）。

### 2. Per-source criticality / SLA / 失敗行為（建議初值）

| 資料類 | criticality | freshness SLA | 允許過期 | 失敗時 | 重試 | 下游信心 | 報告可用性 |
|---|---|---|---|---|---|---|---|
| macro（yfinance 6 tickers） | HIGH | 每交易日 | 1 交易日 | DEGRADED；連續 >2 交易日 → STALE_DATA 攔截派送 | 應用層 backoff×3 | 該維度信心下限標記（advisory） | 報告照發+警告 |
| company fundamentals（月） | HIGH | 月度 | 1 個月週期 | 月報 DEGRADED | backoff×3 | 標記 | 照發+警告 |
| T86 institutional flows | MEDIUM | 每交易日 | 20 交易日累積窗 | DEGRADED（既有縮窗行為升級為顯式警告） | per-date 重試 | industry capital_flow 維度標記 | 照發+警告 |
| RSS industry news | LOW | 每週 | 7 日 | 報告照發（既有 `return []` 升級為顯式「feed dead」警告） | 隔日重試自然存在 | 影響 narrative 非 scores | 照發 |

- **不改投資語意**：freshness 分類、decay、scoring 權重全部不動；本 ADR 的「下游信心」攔是指 artifact 元資料/advisory 欄位與派送攔截，不是分數演算法（需 6.3+ 與 protected semantics review 才可再進一步）。
- exit-code 政策：`DEGRADED`/`PARTIAL_DATA` 為 0；`STALE_DATA` 為 0+派送攔截（或未來可設 hard-fail flag，屬語意決策需送審——Phase 6.1 報告 §22 建議 2 的延續）。

## Alternatives

- **A. 全部轉硬失敗**：與工單明示相反；把部分資料變成零價值 → 拒絕。
- **B. 全維持現狀（靜默）**：6.0 FIR-004「outage 產生自信的錯誤報告」未解 → 拒絕。
- **C. per-leg 部分發布（macro 報照發、缺 leg 的報不發）**：增加派送邏輯複雜度；今日 manifest 是單一報告 → 列為未來選項，不承諾。

## Consequences

- 需要 fetch 診斷可見性（哪個 source 失敗）才能分類 DEGRADED——與 ADR-C06 的 adapter_run_log/job freshness_summary 綁定。
- 報告範本需新增狀態徽章（DEGRADED/STALE）與 provenance 警告列表已部分存在（warnings 欄位）。

## Open Questions

- STALE_DATA 攔截派送的實作位置（publisher 端 vs wrapper 端）。
- T86 與 macro 同時遺失時是否升級 FAILED——初值為 DEGRADED（仍發布，多重警告），重估於營運觀察後。