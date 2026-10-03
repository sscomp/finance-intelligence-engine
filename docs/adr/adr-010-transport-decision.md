# ADR-010: Transport Decision（Phase 6.5 工單 ADR-06）

## Status

ACCEPTED — 2026-10-03

## Context

工單 §11（HTTP Transport — Conditional）：transport-neutral 是**強制**，HTTP adapter 是**條件選項**——只有當它能「改善 contract clarity 且不引入生產承諾」時才納入。Phase 6.5 的目標是驗證邊界本身，不是部署服務（§20：生產部署、ChatGPT plugin 發佈、provider 承諾全部禁止）。Phase 6.6+ 若採 HTTP，選擇必須基於本階段的架構報告。

[VERIFIED] `phase3.service` 的 5 個操作、型別化合約與錯誤分類，全部可在單一 in-process 呼叫弧驗證 — 合約的正確性不依賴任何網路層。

## Decision

[DECISION] Phase 6.5 **刻意維持 transport-neutral**，提供的唯一 transport 是 in-process reference adapter：

```
client code (tests, future adapters)
        |  dispatch(service, operation, params, ctx)
        v
phase3.service.reference  —  JSON envelope：ok / error
        v
IntelligenceService（transport-neutral 核心）
```

- `dispatch()` 是契約驗證的「參考 transport」：未知操作 → INVALID_REQUEST；ServiceError → error envelope；未知例外 → INTERNAL_ERROR（僅型別/訊息，無 stack）。
- **不實作 HTTP**，原因：
  1. 需要新增 web framework 依賴（本階段依賴面刻意不擴大）；
  2. HTTP 隱含認證/TLS/服務生命週期決策 — 這些正是 Phase 6.6+ 依 §25 需要基於架構報告另行決策的項目；
  3. 所有 contract 測試可完全離線重現（clean-room gate §19 全綠，無網路）。

[OPEN] Phase 6.6+ 應基於 6.5 報告評估：直接 ChatGPT plugin（in-process 於 plugin runtime）vs HTTP 微服務 + 部署供應商。本 ADR 不預選。

## Consequences

### 正面

- 核心合約已可在無網路、無依賴增量下驗證；HTTP adapter 的加入變成純增量（同一個 Protocol 之上的薄層）。
- 不做 provider/framework 承諾（§11「不承諾供應商」）。

### 取捨

- 缺一個真實網路層的整合證據 — 由 Phase 6.6 的部署參考 runtime 補上；在此之前，`dispatch()` envelope 即為未來任何 transport 必須重現的線路格式（wire-format 種子，帶 `schema_version`）。