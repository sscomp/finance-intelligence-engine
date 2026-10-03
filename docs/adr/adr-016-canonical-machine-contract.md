# ADR-016: 正典機器契約與漂移偵測政策（Phase 6.7A）

* 狀態：ACCEPTED (2026-10-03)
* 階段：Phase 6.7A（Contract and Machine Schema Reconciliation）
* [DECISION] 本 ADR 記錄「執行時與契約文件不一致時以何者為準」與
  漂移的機械偵測政策。

## 背景

Phase 6.7 規劃（與 6.7A 開始前的逐點重驗證，見
`FIE_PHASE6_7…REPORT.md` §10 與 6.7A evidence `contract-inventory.json`）
確認機器可讀契約 `docs/architecture/phase6-6/http-api-contract.json`
與可執行行為之間存在多處漂移（freshness 可為 null、latest payload
為陣列、HEAD 已服務但未宣告、per-path 401/405/500 與 /readyz 503
信封參照錯誤、405 文案與 Allow 頭不一致、transport 信封類別未文案化）。
6.6R1 修復時的 request-ID/消毒契約則已與行為一致。

## 決策

[DECISION] 衝突解决序位（source-of-truth hierarchy）：

1. **可執行服務契約語意 + 經測試的執行時行為**——行為由
   Phase 6.5/6.6 已接受的測試套件釘住；
2. **入倉的機器可讀契約檔**（`http-api-contract.json`）——機器
   消費面（generated clients/tool 定義）的唯一宣告；
3. **人寫架構/API 文件**（reference-runtime、README、ADR 正文）；
4. 範例/歷史報告（僅參考）。

例外（不「順勢祝福執行時」）：若執行時行為**牴觸**已核准的
Phase 6.5/6.6 安全/傳輸需求（消毒、唯讀、request-ID、憑證衛生），
該行為視為**實作缺陷**——修復向已核准需求靠攏，絕不把不安全行為
正規化進契約（本階段檢查結果：無此類牴觸；6.6R1 契約完整保留）。

漂移偵測（機械化）：
`tests/phase3/transport/test_67a_contract_reconciliation.py`
以標準庫 mini-validator 直接對**入倉契約檔**驗證真實 HTTP 行為：

* T1（宣告 ⇒ 可重現）：每條 path 的每個宣告狀態，皆有
  證據錨定的觸發路徑（live probe 或結構性 catch-all 論證）；
* T2（可重現 ⇒ 已宣告）：對每條路徑以探測電池
  （成功/壞參數/未知 ref/POST/HEAD/壞憑證）驅動真實執行時，
  觀察到的狀態碼集合必須 ⊆ 該 path 宣告的集合；
* 信封形狀：所有探得的回應體逐欄對契約 components 驗證
  （Envelope / ErrorEnvelope / LivenessEnvelope / FreshnessMetadata）；
* 路徑集合相等：契約 paths == 執行時 `_route()` 可達的宣告集合；
* `x-http-status-mapping` 與 `phase3.transport.errors` 逐項相等。

因此：**行為、測試、機器契約、人寫文件不一致時**——以執行時語意為
準（類 1），並由測試把漂移暴露為失敗；機器契約（類 2）必須與行為
一致（6.7A 已完成一致化）；人寫文件（類 3）為衍生視圖，不再重述
信封形狀。未來任何行為變更需與契約檔變更同 commit（change-locking）。

## 後果

* 本次修正：契約檔新增 FreshnessMetadata（nullable freshness 槽）、
  payload 物件|陣列、/readyz 503 → ErrorEnvelope、per-path
  401/405/500(/503) 宣告、x-method-behavior（HEAD 映射）、
  x-envelope-classes、x-auth-gate、x-response-content-type；執行時
  405 文案與 `Allow` 頭/HEAD 支援一致化（「only GET or HEAD」）；
  人寫文件改述雙信封類別。
* 行為語意零變動：freshness 誠實語意、唯讀面、消毒、request-ID、
  對等契約全部保留（由既有 + 新增測試釘住）。
* 機器契約檔仍為 OpenAPI 3.0/3.1 風格的**契約清單**（非完整
  JSON Schema 文件）；測試以其為真實資料源驗證，格式變更需再過
  ADR。