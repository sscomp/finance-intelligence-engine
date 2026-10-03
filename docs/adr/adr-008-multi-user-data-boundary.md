# ADR-008: Multi-User Data Boundary（Phase 6.5 工單 ADR-04）

## Status

ACCEPTED — 2026-10-03

## Context

FIE 的智慧產物（signals、scores、snapshots、evidence graph）是**共享市場智慧**：多個使用者看到同一份確定性計算結果是特性，不是問題。但 Phase 5 之後的個人層資料（watchlist、portfolio、情境設定）是**使用者擁有**的，未來任何多使用者雲端部署必須清楚區分兩者的可見性規則。

風險在於把兩類資料混在同一個存取面上（例如以 email 當資料庫主鍵、以 hostname/路徑推斷使用者），會把 ChatGPT 變成資料庫的擁有者（§1.2 禁令）。

[VERIFIED] `RequestContext` 已定義為 opaque identity（frozen dataclass：`principal_id` / `request_id` / `scopes` frozenset）；測試驗證無 email/hostname/path 進入 principal。

## Decision

[DECISION] 多使用者邊界契約（本階段定義、不實作細節）：

1. **共享智慧**（本階段全部讀取面）：任何有 scope 的 principal 看到相同的確定性結果 — evidence/freshness/score 完全一致，不因 principal 而異。
2. **不透明主體**：principal 是不透明字串（未來由 auth 層發行）。service 合約**禁止**以 email/使用者名稱/hostname/檔案路徑作為 identity 或隔離鍵；測試固定使用合成 principal（`synthetic-*`）。
3. **未來 user-owned data**（watchlist/portfolio 等）：必須以 principal-scoped 儲存隔離（不同的 keyspace/表/列範圍），且**只能**經由另一個明確的寫入邊界存取——不會混入 `IntelligenceService` 的唯讀操作。此設計屬 Phase 6.6+。
4. **scopes 骨架**：`RequestContext.scopes` 已存在但目前所有唯讀操作同權限；未來收緊（例如 future: portfolio read）不需要破壞 service 簽名。

[ASSUMPTION] 隔離機制（row-level security vs keyspace per tenant 等）未在本階段選定 — 這是 Phase 6.6+ 部署設計，先凍結的是 identity 形狀與「共享智慧 / 個人資料」分類。

## Consequences

### 正面

- ChatGPT 整合永遠不會成為「資料庫所有者」：它的身分是無意義的不透明 token。
- 之後新增 user-owned 面不會污染共享智慧讀取路徑；現有 5 個操作與其測試不變。
- 合成 principal 讓所有契約測試可重現、無 PII。

### 取捨

- [OPEN] 跨使用者的「個人化詮釋」由 ChatGPT 在會話側完成；FIE 只保證共享智慧的確定性。若未來需要 server-side 個人化記憶，需另立 ADR。