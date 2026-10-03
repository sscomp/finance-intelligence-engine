# ADR-C08: ChatGPT Integration Boundary

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

- FIE 引擎在設計上 **LLM-free**（6.0 §14 VERIFIED：scoring 純函數、no I/O、無隨機性；唯一 prompt artifact `templates/prompts/research_interpretation_v0.md` 自述 "NOT LOADED BY PRODUCTION"）。唯一非確定性是可注入的 run id/時戳。
- 工單 §20 定義分工：FIE 負責確定性事實/信號/評分/證據/組合狀態/歷史情報/provenance；ChatGPT 負責時下脈絡、推理、解釋、對話、排程的對話式派送。
- 6.0 確認未來介面自然位置：`phase3/api.py` 是乾淨的 in-process API（run/observe/resume），缺 HTTP 層/authz/回應契約。

## Decision

1. **FIE 作為 ChatGPT 的後端 tool/backend，不作為自主 agent**：FIE 不產生推理/建議/敘事；它回傳結構化、帶 provenance 的資料物件；ChatGPT 在其上做解釋與討論。這保持了確定性引擎邊界（可移植至任何 LLM 前端，供應商中立）。
2. **回應封套（typed envelope）**——每個回傳項目必須可機判分類：

``` text
{ kind: FACT | FIE_DERIVED_SIGNAL | FIE_SCORE
       | STALE_DATA_WARNING | MISSING_DATA | USER_STATE,
  as_of: <資料時點（顯式，ISO8601 UTC）>,
  freshness: FRESH | STALE | HARD_EXPIRED | UNKNOWN,
  provenance: { source_ids, run_id, engine_version, config_hash },
  payload: { ... } }
```

- `FACT`：市場原始觀測（macro_daily/stock_monthly/institutional_daily 的值）。
- `FIE_DERIVED_SIGNAL` / `FIE_SCORE`：engine 衍生物，必帶 evidence 引用。
- `STALE_DATA_WARNING` / `MISSING_DATA`：freshness 引擎/ADR-C07 的輸出，**ChatGPT 必須原樣傳達、不得掩蓋**（工具規格中聲明）。
- `USER_STATE`：user-scoped 資料（ADR-C01/C09）。
- API 邊界的完整 read/write 介面清單、授權與同步/非同步分類見報告 §14。
3. **ChatGPT 不獲得 raw DB/SQL 存取**：只有 typed API（read interfaces + 明示 write job 介面）；證據請求回傳 evidence 物件（含 hash），不是查詢結果。
4. **暫不實作 Plugin/HTTP**：本 ADR 定契約；實作屬 6.5/6.7。

## Alternatives

- **A. FIE 內建 LLM 解釋（planned phase3.explain.llm_optional）**：違反「分數確定性」產品承諾與職責分離；把成本與不確定性帶進引擎 → 拒絕現階段；保留為敘事層備援（ChatGPT 不可用時的 server-side 選項）。
- **B. 讓 ChatGPT 直接讀 SQL（如 MCP to Postgres）**：洩 schema/其他 user 資料的風險；無 provenance/envelope 控制 → 拒絕。
- **C. 把 FIE 做成自主 agent（自行決定何時查詢/回報）**：排程與治理在 ADR-C06 的 job 模型，FIE 不做自主決策 → 拒絕。

## Consequences

- envelope 的 `kind`/`as_of`/`freshness` 欄位成為 6.5 API 的驗收面——每個 read interface 都要能填。
- 「ChatGPT 必須轉達 warnings、不得淡化」是工具描述（tool spec）責任，屬 6.7 的實作註記。
- 供應商中立性：契約不含 OpenAI 专属概念（用 generic tool-call 形狀），遷移到其他 frontend 成本低（工單 §21 要求）。

## Open Questions

- `USER_STATE` 回傳的欄位級遮蔽（例如組合中的金額對家庭成員可見性）——6.6 identity 設計定。
- 大 payload（完整 evidence tree）的分頁/摘要協議（建議 depth limit + hash 引用，6.5 細化）。