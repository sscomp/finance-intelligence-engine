# Phase 6.5 — ChatGPT Tool Contract（`fie.*` 唯讀工具面）

Status: contract frozen 2026-10-03（branch `feature/fie-phase6-5-cloud-service-boundary`）。
正式決策紀錄：`docs/adr/adr-005..adr-010`。本文件是 §6 要求的 ChatGPT-facing
tool 契約：未來的 ChatGPT plugin/skill 只需要本文件 + service 呼叫，不需要
任何持久層知識。

---

## 0. 共通 wire 契約（in-process reference adapter 的 envelope）

`phase3.service.reference.dispatch(service, operation, params, ctx)` 定義了
未來任何 transport 必須重現的線路格式（`schema_version = "6.5"`）。

### ok envelope

```json
{
  "schema_version": "6.5",
  "kind": "<operation>",
  "request_id": "<opaque>",
  "principal_id": "<opaque>",
  "status": "ok",
  "freshness": {…FRESHNESS METADATA…},
  "payload": {…DOMAIN DATA…},
  "evidence_refs": […PROVENANCE / EVIDENCE…],
  "warnings": […非致命可用性提醒…]
}
```

### error envelope

```json
{
  "schema_version": "6.5",
  "kind": "<operation>",
  "request_id": "<opaque>",
  "principal_id": "<opaque>",
  "status": "error",
  "error": {"code": "<taxonomy>", "message": "<sanitized>", "details": {…}},
  "payload": null
}
```

### 錯誤分類（§5.2；HTTP status code 不是 domain 錯誤模型）

| code | 意義 |
|---|---|
| `INVALID_REQUEST` | 參數缺漏/型別錯誤/未知 kind 或 operation |
| `NOT_FOUND` | 未知 entity / 未知 evidence ref |
| `STALE_DATA` | （保留）明確要求新資料但只有已過期資料的場景 |
| `DATA_UNAVAILABLE` | 域內沒有該資料（非基礎設施問題） |
| `DEPENDENCY_UNAVAILABLE` | 持久層/依賴不可讀（基礎設施問題） |
| `INTERNAL_ERROR` | 未分類失敗；僅例外型別+訊息，**無 stack trace** |

### Freshness 語義（§8）

| service state | 對 client 的意義 |
|---|---|
| `FRESH` | 確定性結果基於已知且在窗口內的資料 — 可視為 current |
| `DEGRADED` | 上游觀測已超出正常更新週期（仍在警告窗內）— 說明 age |
| `STALE` | 已過硬性新鮮度期限 — **不得**當 current 陳述 |
| `UNAVAILABLE` | 完全沒有上游觀測 — 結果基礎不明 |

任何非 FRESH 狀態都會同時出現在 `freshness.state` 與 `warnings`；
**永不**把 stale 資料靜默轉成 current-data 宣稱；讀取路徑**永不**
發起即時網路抓取。

### 隱私/安全共通規則（§5.1 / §7 / §14）

- 回應**不含**：DSN、SQL、表名、本機路徑、物件 repr、憑證。
- principal 是不透明字串；`synthetic-*` 僅用於測試。不接受 email 當
  identity，不從 hostname/路徑推斷身分。
- 所有集合回應有上限（evidence：預設 50、上限 200；signals：100）；
  無 unbounded dump。

---

## 1. `fie.health` → `get_health`

- **用途**：確認 FIE 服務可回答問題（readiness + 概況）。
- **輸入**：無參數。
- **輸出**：`payload = {status, backend_kind(: "sqlite"|"postgres" family),
  schema_current_version, counts:{signals, scores}, warnings, checked_at}`。
- **錯誤**：`DEPENDENCY_UNAVAILABLE`（持久層不可讀）。
- **freshness**：無（健康不是資料）。
- **未來授權**：任何持有基本 scope 的 principal。
- **唯讀**：是。

## 2. `fie.latest_intelligence` → `get_latest_intelligence`

- **用途**：列出各實體（可按 kind 過濾）的最新確定性智慧摘要。
- **輸入**：`kind`（可忽略；`macro|industry|company`）、`limit`（1..200）。
- **輸出**：`payload = [IntelligenceSummary…]`，每項含
  `{kind, entity_id, score, computed_at, as_of, freshness, evidence_refs,
  warnings}`。
- **錯誤**：`INVALID_REQUEST`（未知 kind、limit 非整數/<1）。
- **freshness**：每項頭等（state/as_of/checked_at/source_date/age）。
- **未來授權**：共享智慧讀取 scope。
- **唯讀**：是。

## 3. `fie.entity_intelligence` → `get_entity_intelligence`

- **用途**：單一實體的完整智慧（分數、payload、上游訊號、evidence）。
- **輸入**：`kind`、`entity_id`（必填）、`as_of`（可選，影響 freshness 分類）。
- **輸出**：`payload = {kind, entity_id, as_of, score, payload(域內
  breakdown, JSON-safe), signals[≤100], computed_at}` + 頭等
  `freshness` / `evidence_refs` / `warnings`。
- **錯誤**：`INVALID_REQUEST`（未知 kind）、`DEPENDENCY_UNAVAILABLE`。
- **行為註記**：尚無 snapshot 的實體回 `score=null` + warning
  `no deterministic score snapshot available`（NOT_NOT_FOUND 語義 —
  實體存在於 domain 詞彙但分數未算）。
- **未來授權**：共享智慧讀取 scope。
- **唯讀**：是。

## 4. `fie.evidence` → `get_evidence`

- **用途**：沿著 evidence graph 取得任一結點的有限溯源（§9 的
  `summary → evidence refs →fie.evidence → provenance` 流）。
- **輸入**：`ref`（evidence 結點 reference，如
  `entity:company:2330`、`score:company:2330`）、`limit`（1..200）。
- **輸出**：`payload = {ref, kind, label, metadata(JSON-safe), tags,
  computed_at, related:[{ref, kind, edge_type, direction}]}`，related
  截至上限；`direction` 為 `upstream|downstream`。
- **解析規則**：精確 node id 直接解析；未帶日期的
  `score:<kind>:<id>` 解析為該實體**最新**分數結點（client 不需要知道
  內部日期後綴詞彙）。
- **錯誤**：`INVALID_REQUEST`（空 ref/limit）、`NOT_FOUND`（未知 ref）。
- **未來授權**：共享智慧讀取 scope（provenance 與資料同權限）。
- **唯讀**：是。

## 5. `fie.freshness` → `get_freshness`

- **用途**：單一實體的資料新鮮度狀態（在引用任何數字前先確認誠實性）。
- **輸入**：`kind`、`entity_id`、`as_of`（可選）。
- **輸出**：`payload = {state, as_of, checked_at, source_date, age}`。
  映射規則：governance `FRESH→FRESH`、`STALE→DEGRADED`、
  `EXPIRED→STALE`、`UNAVAILABLE/無觀測→UNAVAILABLE`（policy 本身
  `phase3.freshness` 未變動）。
- **錯誤**：`INVALID_REQUEST`（未知 kind）、`DEPENDENCY_UNAVAILABLE`。
- **未來授權**：共享智慧讀取 scope。
- **唯讀**：是。

---

## 6. 明確不在本面（§20 禁令，全部 Phase 6.6+ 且需新工單）

交易執行、brokerage 操作、portfolio 變更、警示訂閱、Telegram 發佈、
排程器控制、任何寫入操作。