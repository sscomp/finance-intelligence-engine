# ADR-012: 認證邊界——身分提供者位於 IntelligenceService 之外

* 狀態：ACCEPTED (2026-10-03)
* 階段：Phase 6.6
* [DECISION] 本 ADR 記錄 §6 的認證邊界設計。

## 背景

§6 要求：不透明主體模型；身分提供者在 `IntelligenceService`
之外；測試/開發環境安全認證；拒絕缺失/無效憑證；來源碼中
絕不內嵌憑證；絕不記錄祕密或原始 Authorization 值；生產身分
提供者可替換；**絕不**在本工單中提供生產 OAuth/OIDC 佈建。

## 決策

1. [DECISION] 服務層不識別「人」，只識別
   `RequestContext.principal_id`（不透明字串）。身分驗證在
   傳輸邊緣發生（`Authenticator` Protocol，認證先於任何領域
   分派），服務層永遠保持「一個已驗證的唯讀主體」世界觀。
2. [DECISION] 參考實作兩種模式（`FIE_AUTH_MODE`）：
   * `none` — `NoopAuthenticator`，主體 `anonymous`。僅適用
     開發/測試與本機回環（預設綁定 127.0.0.1，§9 安全預設）。
   * `token` — `BearerTokenAuthenticator`，token 只從環境
     （`FIE_AUTH_TOKEN`）解析；**fail-closed**：token 缺失時
     執行時拒絕啟動（console 進入點回傳 2；建構子直接拒絕）。
     比較使用 `hmac.compare_digest`（常數時間）。
3. [DECISION] 憑證紀律：
   * 憑證永不寫入來源/倉庫/容器映像（§13 靜態稽核測試）。
   * 憑證永不進入遥測記錄、錯誤信封或 `to_dict()` 序列化
     （`TransportConfig.auth_token` 為 `repr=False` 且排除於
     log-safe 視圖；測試覆蓋）。
   * 401 錯誤訊息不含提交的憑證片段。
4. [DECISION] 生產替換性：`Authenticator` 是可替換協議；
   生產 OAuth/OIDC/JWT 提供者作為**另一個** Authenticator
   實作接入傳輸層，服務層零變更。本工單不提供任何生產佈建
   （§6/§24），僅保證介面與主體模型可承接。
5. [VERIFIED] 覆蓋測試：`tests/phase3/transport/test_auth.py`
   （單元 + 端到端 401 路徑、認證先於領域、憑證不入遥測）。

## 後果

* 服務契約測試不依賴網路/IAM；批次平面不受影響。
* 已知限制 [OPEN]：token 模式是單一共用憑證（無每主體速率
  控制、無撤銷清單）；生產部署需替換為具身分提供者的
  Authenticator，這是 Phase 6.7+ 的事項。