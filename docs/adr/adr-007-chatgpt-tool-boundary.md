# ADR-007: ChatGPT Tool Boundary（Phase 6.5 工單 ADR-03）

## Status

ACCEPTED — 2026-10-03

## Context

Phase 6.5 的最終 client 是 ChatGPT。它需要：

- 一組可描述為工具（function calling）的唯讀操作；
- 穩定的輸入/輸出型別（JSON-safe），可轉成 tool schema 與回覆內容；
- freshness 與 provenance 作為「頭等」概念，讓模型能誠實回答「這個數字多新、根據什麼」；
- 錯誤時的可用語义（不是 stack trace），能轉化為對使用者的自然語言說明。

[VERIFIED] 工具語義需求已在 `phase3.service` 的 5 個操作 + envelope 中實現（`reference.py` 定義了 in-process envelope 形狀）；缺的是把合約寫成 ChatGPT 面的正式 tool 文件。

## Decision

[DECISION] ChatGPT tool 契約 = 5 個 tool，一一映射到 service 操作，全部唯讀：

| tool | service 操作 | 讀取 |
|---|---|---|
| `fie.health` | `get_health` | backend family + counts + schema version |
| `fie.latest_intelligence` | `get_latest_intelligence` | 每個實體的最新摘要 + freshness + warnings |
| `fie.entity_intelligence` | `get_entity_intelligence` | 單一實體完整智慧（score/payload/signals/evidence） |
| `fie.evidence` | `get_evidence` | 任一 evidence ref 的節點 + 有限連結 |
| `fie.freshness` | `get_freshness` | 單一實體新鮮度狀態與年齡 |

正式 tool 文件（用途/輸入/輸出/錯誤/freshness/授權範圍/唯讀聲明）位於
`docs/architecture/phase6-5/chatgpt-tool-contract.md`。

### Envelope 語義（PROVENANCE / FRESHNESS / DOMAIN 三區）

in-process reference adapter（`phase3.service.reference`）的 ok envelope：

```
schema_version / kind / request_id / principal_id / status
freshness        — FRESHNESS METADATA（頭等，與資料並列）
payload          — DOMAIN DATA
evidence_refs    — PROVENANCE / EVIDENCE
warnings         — 可用性提醒（非錯誤）
```

error envelope：`status=error` + 穩定 `error.code`（§5.2 七值清單）+ 消毒後 `message`，`payload=null`。

### 未來認證邊界

[ASSUMPTION] Phase 6.5 的 `RequestContext` 只攜帶 opaque `principal_id`/`request_id`/`scopes`（無 email、無 hostname、無路徑）。未來 ChatGPT 面的**驗證**（如何把一個 ChatGPT 使用者映射為 opaque principal）屬 Phase 6.6+ 的 auth 設計 — 邊界簽名已預留 `ctx`，無需破壞式變更。
[OPEN] scopes 詞彙尚未定義（Phase 6.5 全部操作同權限唯讀）；未來 user-owned data（portfolio）將需要 `scopes` 生效。

## Consequences

### 正面

- ChatGPT plugin/skill 實作變成純 adapter 工作（5 個 tool → service 呼叫 → envelope → 回覆），無持久層知識需求。
- freshness/warnings 讓模型「不會把舊資料說成新資料」（§8 誠實約束）。
- 唯讀聲明在型別層（query-only 連線）與文件層（tool 契約）雙重成立。

### 取捨

- [DECISION] 未在 Phase 6.5 發佈任何 ChatGPT plugin（§20 禁令）；tool 契約只存在於倉庫文件與測試中，待 Phase 6.6+ 驗證審查後才考慮發佈。