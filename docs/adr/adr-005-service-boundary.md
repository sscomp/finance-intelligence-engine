# ADR-005: Application Service Boundary（Phase 6.5 工單 ADR-01）

## Status

ACCEPTED — 2026-10-03

## Context

Phase 6.3 之後，FIE 的持久層已是 backend-agnostic（SQLite/PostgreSQL 可插換），但應用面的唯一入口仍是 Phase 3B 的內部 API（`phase3.api.run_pipeline`）與 CLI。未來的 ChatGPT 整合若直接接上這些入口，會被迫處理：

- 持久層細節（DSN、連線生命週期、schema 版本）；
- 寫入路徑（ingestion/scoring）與讀取路徑共用同一組呼叫弧；
- 未定義的錯誤型態（原始 exception、stack trace、SQL/路徑字串）。

[VERIFIED] 現況：讀取智慧產物（scores、signals、evidence graph、freshness）在 domain 層已完整存在，缺的是一個「不外洩內部詞彙」的正式邊界。

## Decision

[DECISION] 建立 transport-neutral 型別化服務邊界 `phase3.service`：

- `IntelligenceService` Protocol：5 個唯讀操作（`get_health` / `get_latest_intelligence` / `get_entity_intelligence` / `get_evidence` / `get_freshness`），每個操作接受尾端可選 `RequestContext`（opaque principal + request id + scopes）。
- `DefaultIntelligenceService：單一實作，以 Phase 6.3 的 `DatabaseStore` seam + repository 層為後盾，建構時以 `set_query_only()` 結構性強制唯讀。
- 型別化外部合約（`phase3.service.contracts`）：`HealthReport` / `IntelligenceSummary` / `EntityIntelligence` / `EvidenceItem` / `Freshness`，全部 JSON-safe、帶 `to_dict()`。
- 穩定錯誤分類（`phase3.service.errors`）：INVALID_REQUEST / NOT_FOUND / STALE_DATA / DATA_UNAVAILABLE / DEPENDENCY_UNAVAILABLE / INTERNAL_ERROR，訊息經 URL/path 消毒。
- §5.1 禁令在實作層執行：回應只含 domain 詞彙（score、signal、evidence ref、freshness state），不含 DSN、SQL、表名、本機路徑、物件 repr。

### 為何是應用服務層而非直連持久層

[DECISION] ChatGPT（及任何未來 client）只看得到 service 合約。原因：

1. **資料所有權**：FIE 擁有確定性智慧（§1.1/§1.2）；client 是消費者，不得知道（更不得繞過）schema 或儲存後端。
2. **薄層**（§1.3）：邊界不做評分、不做聚合外的轉換 — 只是把既有 domain 產物以穩定型別曝光。
3. **錯誤衛生**：分類化錯誤 + 消毒訊息，讓未來任何 transport（HTTP、in-process、plugin）不必各自發明防洩漏規則。
4. **可測性**：合約以 in-process reference adapter 驗證（§17），不需要網路。

## Consequences

### 正面

- ChatGPT 整合只需實作一個 adapter，把 5 個操作映射到工具呼叫。
- 讀取操作的型別與錯誤分類可獨立 contract-test（SQLite 與 PostgreSQL 上同一組測試）。
- 唯讀在結構上保證（query-only 連線），不靠紀律。

### 取捨

- [OPEN] `get_entity_intelligence` 目前回傳「最新快照」；`as_of` 參數目前僅影響 freshness 分類，尚未做到時點快照選取（需要 Phase 6.6+ 的 historical-snapshot 存取語義）。
- [ASSUMPTION] entity kind 詞彙（macro/industry/company）維持 Phase 3 既有分類；未來新增 kind 需擴充 `_KINDS` 與 freshness policy 映射。