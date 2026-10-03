# ADR-C01: Shared vs User-Scoped Data

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

FIE 目前為單租戶端到端：一個主機佈局、一個扁平 metadata namespace、無任何 identity/auth，portfolio 檔案由 CLI 傳入（`cli.py` portfolio 子命令），從不落 DB；watchlist/thesis/preferences/analysis history 完全沒有儲存機制（6.0 audit §15，grep 零儲存程式碼）。既有 schema（`signal_log`、`score_snapshot`、`graph_nodes`/`graph_edges`、`macro_daily`、`stock_monthly`、`institutional_daily`）均以 entity/date 為鍵，**任何表都沒有 user 欄位**（`phase3/persistence/schema_v1.py:40-170`、`db.py:60-104`）。

雲端目標情境（Phase 6.2 工單 §18/§21）是多位使用者共享同一份市場情報、各自擁有獨立的 portfolio/watchlist/thesis/preferences/analysis history。

## Decision

1. **資料分為兩層邏輯域，未來對應到同一個受管資料庫內的兩個 schema（`market`/`derived` vs `user`）**：
   - **Shared（market + derived）**：市場原始觀測（macro_daily、stock_monthly、institutional_daily、日曆/參照資料）、derived intelligence（signal_log、score_snapshot、evidence graph、pipeline 輸出）。全體使用者共享一份，**不得 per-user 複製**。
   - **User-scoped**：portfolio、positions、watchlist、thesis、preferences、alerts、analysis history、saved views/reports。綠地資料模型（今日不存在任何儲存），每一列帶 `user_id` FK；隔離以 row-level 歸屬 + API 層 ownership 檢查達成，**不用 per-user database**。
2. **單一使用者今日的狀態視為 user-scoped 的 1 人特例**：現有的/portfolio 檔案 JSON 在遷移時歸入 user schema 的第一個使用者，而非流入 shared 域。
3. **Derived intelligence 何時會變成 user-specific**：僅當輸入含 user 狀態（例如以 user 的 portfolio 計算的風險指標、以 user watchlist 聚合的產業視圖）時。此類衍生物一律寫入 user domain、其輸入引用 shared 域的實體 id；shared 域內不得出現任何依賴 user 輸入的衍生物（否則污染共享情報的正確性）。

## Alternatives

- **A. 每 user 一份完整資料複製**：隔離徹底但成本與資料重複、無法保證所有使用者看到同一份情報 → 拒絕（工單 §18 明示不複製）。
- **B. 只靠 row-level security、不分 schema**：可行，但 RLS 直接繫死單一供應商的 policy 機制，且 shared/user 的可變性與留存政策本質不同 → 採 schema 分域為主、RLS 為未來供應商層的加分防禦。
- **C. 維持單租戶**：與 §21 家庭情境直接矛盾 → 拒絕。

## Consequences

- 6.0 audit 對「shared derived data 無 scope」的全表清單可直接沿用為 shared 域的遷移清單；user 域是全新實作（Phase 6.6）。
- API 層必須在 user 域強制 ownership 檢查（見 ADR-C09）。
- shared 域表結構（無 user 欄位）不需任何修改即可上雲 → 遷移風險集中於 user 域綠地設計與 legacy family 的 seam（ADR-C03）。

## Open Questions

- user 域的 portfolio/positions 結構化程度（JSON payload vs 正規化表）——建議 Phase 6.6 先 JSON payload + 最小正規化欄位，證明查詢模式後再正規化。
- 是否需要 organization/tenant 中間層（家庭情境 2 人之下不需要；若未來開放群組再重估）。