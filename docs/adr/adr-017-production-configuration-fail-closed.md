# ADR-017: 生產設定 Fail-Closed 契約（Phase 6.7B-R1）

* 狀態：ACCEPTED (2026-10-04)
* 階段：Phase 6.7B-R1（Production Configuration Fail-Closed Implementation）
* [DECISION] 本 ADR 記錄 FIE 參考執行時**設定決議**的 fail-closed 契約：
  環境/設定檔模型、「缺值」與「明確非法值」的語意區分、
  staging/production 的啟動拒絕語意、以及消毒後的錯誤契約。

## 背景

Phase 6.7B kickoff（HP-00/HP-04/HP-08/HP-09 與 OI-04）逐點驗證了
`load_transport_config` 的多個 fail-open 面：非法 `FIE_AUTH_MODE`
**靜默回落為 `none`（關閉認證）**、`FIE_DATABASE_URL` 未設定時
靜默選擇 CWD 相對 SQLite 路徑、timeout 設定呈「split-brain」（非數值
逸出 ValueError、`<=0` 靜默回落 60.0）等。本 ADR 建立單一、
決定論的設定契約取代這些行為。

## 決策

### 1. 設定檔模型

`FIE_SERVICE_ENV ∈ {local, test, staging, production}`；**缺值**
保留已接受的 `local` 預設。**明確提供但非法**的值（含近視拼字如
`prodution`、`prod`、`foo`）不得靜默變成 `local` — 一律以
`ConfigurationError`（碼 `UNKNOWN_SERVICE_ENV`）拒絕。大小寫折疊
（`Production` → `production`）允許；其他拼法一律拒絕。

### 2. 「缺值」與「明確非法」的區分（核心原則）

* **缺值** → 已接受的安全預設（本機相容性邊界，有行為不變測試）：
  `127.0.0.1`、`8787`、`none`、`INFO`、`60.0`、`writable`、
  可攜 SQLite 預設、`structured`。
* **明確供給的非法值**（參數或環境）→ 每一個設定檔（含 local/test）
  決定論地拒絕；永遠不靜默回落預設。各旋鈕：
  * `FIE_AUTH_MODE`：詞彙即認證邊界自身的 `AUTH_MODES`
    （`none|token`）；非法值碼 `UNKNOWN_AUTH_MODE`——**永遠不可能
    再以回落 `none` 靜默關閉認證**（HP-00 修復）。
  * `FIE_SQLITE_ACCESS_MODE`：組態層與儲存層**同一詞彙、同一行為**
    ——非法值（非 `writable|readonly|immutable_snapshot`）在組態層
    即拒絕（碼 `UNKNOWN_ACCESS_MODE`）；儲存層自身的 `ValueError`
    路徑不變（雙層分歧消除，詞彙持續由
    `tests/phase3/transport/test_66r4_readonly_sqlite.py` 交叉釘住）。
  * `FIE_HTTP_PORT`：非數值、負數、超出 `0..65535` 一律拒絕
    （碼 `INVALID_HTTP_PORT`）；`0` 保持為**已記錄且釘住的暫時埠**
    （暫時綁定供測試/潔室探測；非「非法值」）。
  * `FIE_LOG_LEVEL`：`DEBUG|INFO|WARNING|ERROR` 之外一律拒絕
    （碼 `INVALID_LOG_LEVEL`）。
  * `FIE_REQUEST_TIMEOUT`：**單一詞彙**——可解析、有限、
    嚴格為正的秒數；非數值/`0`/負數/`inf`/`nan` 全部決定論拒絕
    （碼 `INVALID_REQUEST_TIMEOUT`），split-brain 行為消除（HP-08）。

### 3. staging/production（production-like）fail-closed 閘

設定決議階段（**先於 socket bind**）、且僅對 production-like 設定檔：

* `FIE_AUTH_MODE=none` → 拒絕（碼 `AUTH_MODE_FORBIDDEN_IN_PRODUCTION`）；
* `token` 模式缺憑證 → 拒絕（碼 `AUTH_CREDENTIAL_MISSING`）；
* `FIE_DATABASE_URL` 缺失/空白 → 拒絕（碼 `DATABASE_URL_MISSING`）；
  可攜 CWD 相對 SQLite 預設不被接受（HP-04 修復）；
* SQLite DSN **必須為絕對路徑**（`postgres://` DSN 不受限）→ 否則
  拒絕（碼 `DATABASE_URL_INVALID`）。此**DSN 形狀閘位於組合根**
  （`phase3.transport.http.run_server`）——那是唯一允許
  `persistence.backend.resolve_spec` 解析完整 spec 的傳輸模組；
  6.7A 凍結匯入面審計因此維持原樣。

主控台進入點（`python -m phase3.transport.http`）在任何
`ConfigurationError` 上輸出**單行消毒 JSON**至 stderr 並以非零值
（`2`）結束；`run_server` 拒絕路徑在任何 runtime/socket 物件建立
之前生效。不存在 fallback 服務、降級未認證模式或部分啟動。

### 4. 消毒錯誤契約（穩定的機器可測類別）

`ConfigurationError(code, message)`（定義於
`phase3.service.runtime_config`，傳輸層再輸出）：

* `code`：閉集類別詞彙（`REFUSAL_CODES`，由測試釘住）——
  `UNKNOWN_SERVICE_ENV`、`UNKNOWN_AUTH_MODE`、
  `AUTH_MODE_FORBIDDEN_IN_PRODUCTION`、`AUTH_CREDENTIAL_MISSING`、
  `DATABASE_URL_MISSING`、`DATABASE_URL_INVALID`、
  `UNKNOWN_ACCESS_MODE`、`INVALID_HTTP_PORT`、`INVALID_LOG_LEVEL`、
  `INVALID_REQUEST_TIMEOUT`；
* `message`：單行、命名環境變數與原因；**設定值一律扣留**
  （"value withheld"）——bearer token、DSN/密碼、路徑、PII、
  敵意注入標記皆不可能穿越（leak-marker 測試覆蓋所有新錯誤面：
  message、`repr`、stderr、stdout）。
* DSN 形狀與缺值閘的 DSN 檢視僅以謂詞形式（`source`、空白性、
  backend 布林、絕對性）被消費——原始 DSN/路徑不進入診斷。

### 5. local/test 相容性邊界（不作假設，以測試證明）

local/test 設定檔保留全部已接受的缺值預設（含 `auth_mode=none`
開發匿名模式、可攜 SQLite 預設、相對路徑 DSN）與 `FIE_LOG_FORMAT`
既有 fallback 行為。只有**明確非法值**在 local/test 亦拒絕（決定論
不是設定檔限定的）。`FIE_LOG_FORMAT` 不在 R1 範圍，其既有
fallback 行為不變（殘餘項已記錄）。

### 6. 缺陷修復 FIE-R1-001（範圍內，因 R1 閘道而現形）

`phase3.persistence.backend._spec_from` 的舊 regex
`^sqlite:(?:///)?` 多吃一個斜線，把**文件記載的絕對形式**
`sqlite:///abs/path` 靜默改寫成 CWD 相對路徑（與其自身 docstring
契約相悖——ADR-016 類漂移；亦使絕對 DSN 經由相對路徑觸發
CWD 依賴）。修復：`sqlite://` 前綴剝離後保留其餘路徑原文
（`sqlite:///abs/db → /abs/db`）；`sqlite:relative → relative`
不變。由
`tests/phase3/persistence/test_backend_contract.py` 的正規化釘住。

### 7. 後續工作的所有權邊界（不聲稱已決）

* **R2**（認證/健康探針重設計）：`Authenticator` 邊界保持可替換；
  local/test 缺憑證仍在 authenticator 建構層 fail-closed；R1 不
  實作 OIDC/IdP、不改動 `/healthz` `/readyz` 存取政策、不觸
  Docker HEALTHCHECK 契約（HP-01 屬 R2）。
* **R3**（PostgreSQL/Schema）：DSN **可達性**驗證與 schema-version
  就緒閘仍屬 R3；R1 的 `DATABASE_URL_INVALID` 只驗證 DSN 形狀
  （絕對性），連線失敗維持既有 `/readyz` 503 領域路徑。
* **R4**（可觀測性）：`FIE_LOG_FORMAT` 非法值仍為既有安全回落
  （R1 未動）；如需決定論化記入 R4 範圍。
* HP-09（import-time config root）以文件記錄為主（沿用既有
  組態決議時點；無新 import 面擴張）。

### 8. 本 ADR 未解決之事（保留 OWNER 決定）

6.7B kickoff 的 OD-1..OD-6 owner 決定（認證後端、tenancy、
公開發行邊界、RPO/RTO 數值等）**全部保持 OWNER_APPROVAL_REQUIRED
狀態；本 ADR 不觸及、不預設**。