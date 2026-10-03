# ADR-C09: Multi-User Identity Boundary

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

- 今日 FIE **沒有任何 identity/auth 概念**（6.0 §15 VERIFIED）：portfolio 由 CLI 檔案傳入不落 DB、Hermes task 檔案是扁平 namespace、無 user 欄位存在於任何表。
- 工單 §19 要求：優先使用 opaque FIE user identity，不儲存不必要的 ChatGPT/帳號個資；指定 user_id、認證映射、授權、租戶隔離、ownership 檢查、審計、帳號刪除/匯出的**未來需求**但不實作。
- Phase 6.1 已把現存個人資料（Telegram chat ID/暱稱）趕出 runtime config——這個先例是本 ADR 的態度：**FIE 只記它營運上必需的識別**。

## Decision

1. **FIE 內部主體 = opaque `user_id`（uuid，FIE 產生）**。FIE 不儲存：ChatGPT 帳號 id、email、姓名、頭像、token 等個資——除非某服務營運必需（如通知目標，屆時按 6.1 先例放 user-scoped 加密/secrets 區，不進一般表）。
2. **認證映射（future）**：外部 IdP/平台（ChatGPT Plugin 的 OAuth 或 API gateway）驗身份後換發 FIE `user_id`；FIE 只存映射鍵的雜湊/最小引用。此設計使 ChatGPT 訂閱/Plugin 的供應商變動不觸碰 FIE 資料模型（工單 §21 的供應商中立要求）。
3. **授權（future）**：粗粒度即可——`user` 域的每列帶 `user_id`，API 層 ownership 檢查（使用者只能讀寫自己的 user 域列）；shared 域全體可讀、僅排程服務可寫（service account 概念）。
4. **租戶隔離**：單 instance、schema 分域（ADR-C01）+ row ownership；家庭規模（§21）不需要 per-tenant database 或獨立部署。
5. **審計**：`ops.jobs` 記 job 發起者（scheduled service 或 user_id）；user 域寫入（update_portfolio 等）記 actor 與時間——滿足「誰改了什麼」的最小稽核。
6. **帳號刪除/匯出（future 需求）**：user 域的列必須可按 user_id 完整列出並匯出/刪除（GDPR 型操作）；刪除**不觸碰** shared 域。設計約束：user 域不得引用 shared 域之外的不可刪資料。
7. **本階段不實作任何 auth**；僅此邊界文件化。

## Alternatives

- **A. 直接存 ChatGPT user identifiers**：把 FIE 與特定平台耦合，且儲存不必要的第三方個資（工單 stop condition：要求儲存非必要敏感 profile 資料即 STOP）→ 拒絕。
- **B. 每個 user 一套 schema/DB**：家庭規模過重；跨 user 的 shared 讀取多餘跳接 → 拒絕。
- **C. 無 identity、以「檔案/token 即身份」**：無法支援 §21 的兩個獨立 ChatGPT 帳戶情境與審計 → 拒絕。

## Consequences

- 6.6 實作 user 域時只需：一張 `user_identity` 最小表（user_id、外部映射雜湊、建立時間、狀態）+ user 域各表的 `user_id` FK。
- API 每個 user-scoped interface 多一個隱含參數（caller identity 由 gateway 注入，非請求參數）。
- 通知（Telegram 等）目標屬 user 域偏好+secrets——接續 6.1 的 env-injection 先例。

## Open Questions

- IdP 選擇（ChatGPT Plugin OAuth vs 獨立 OIDC）——延至 6.6/6.7。
- family 情境中是否需要共享 watchlist 的分享機制（初值：不需要，各自獨立；分享屬未來功能）。