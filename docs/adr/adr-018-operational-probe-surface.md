# ADR-018: 營運探針表面（Operational Probe Surface）契約（Phase 6.7B-R2）

* 狀態：ACCEPTED (2026-10-04)
* 階段：Phase 6.7B-R2（Authentication and Health Probe Runtime Hardening）
* [DECISION] 本 ADR 記錄 HP-01（token 模式下 Docker HEALTHCHECK 無憑證、
  `/healthz` 回 401、容器永久不健康）的修復契約：將 `GET/HEAD /healthz`
  分類為**唯一的公開營運存活探針**，在不引入任何認證旁路機制的前提
  下讓已認證的容器可被健康監控。

## 背景

Phase 6.6 進來的契約明確宣告：token 模式的認證閘**先於路由**、
作用於**所有路徑**（含 `/healthz`、`/readyz`；machine contract
`securitySchemes.bearerAuth` / `x-auth-gate`）。Dockerfile 的
HEALTHCHECK 依正確的設計原則**不內嵌任何憑證**，僅以無憑證請求
探測 `/healthz`——於是 token 模式下探針得 401（`UNAUTHENTICATED`），
重試三次後容器被宣告永久不健康（HP-01）。已認證的容器反而無法被
有意義的健康監控。

## 決策

### 1. `GET /healthz` — 公開營運存活探針（唯一成員）

`/healthz`（routing op `liveness`）被分類為狹義、公開的營運探針，
在**每個 profile**（local/test/staging/production、`none`/`token`）
下於認證之前即服務。分類資格（工作單 §6 的逐條檢驗）：

* 處理器 `_liveness_payload` 只由常數組成固定最小信封
 （`schema_version` / `kind=healthz` / `request_id` / `status=alive`）；
  **不觸及**資料庫、檔案系統、業務服務（永不到達 `dispatch`）與任何
  受保護資料；
* **不呼叫 Authenticator**——探針無法成為「憑證有效性預言機」：
  缺憑證、無效憑證、有效憑證的回應逐位元組相同；
* 分類是**封閉集合** `OPERATIONAL_PROBE_OPERATIONS = frozenset({"liveness"})`，
  只能由路由表以規範形式（URL 解碼、空段正規化後精確等於
  `["healthz"]`）到達；無任何 header、query 參數、環境變數、
  來源位址、User-Agent 或探針 token 可以擴大此集合或在其他任何
  路徑跳過認證；
* 方法閘先於探針：`POST/PUT/PATCH/DELETE /healthz` → 405；
  HEAD 鏡射 GET（空本體）；
* 集合被凍結並由 R2 專測套件釘住
  （`tests/phase3/transport/test_67b_r2_auth_health.py`）：把任何
  受保護作業加入此集合即失敗。

### 2. `GET /readyz` — 維持已認證（相容性優先）

工作單 §8 指示：先確定既有語意，除非修 HP-01 必要，否則保留相容
行為。HP-01 只要求存活探針可無憑證成功，而 `/readyz` 會觸及
持久層接縫（`get_health`：read-only counts、backend 族、schema 版本）
並回報服務中繼資料——**維持既有已認證政策**：token 模式下無憑證
呼叫者照例得 401；需要無憑證平台探針的 orchestrator 使用
`/healthz`；需要完整健康細節的已認證客戶端使用 `/v1/health`。
liveness（程序存活）與 readiness（持久層可達）維持概念分離。

### 3. 明確禁止（本 ADR 的否定面）

* 不得存在可重用旁路：header（如 `X-Healthcheck`）、query 參數、
  環境變數（如 `FIE_SKIP_AUTH_FOR_HEALTHCHECK`）、來源 IP 假設、
  User-Agent 字串、magic probe token 一律不得停用認證；
* 探針不得接觸受保護資料、不得變更狀態、不得建立業務交易；
* 探針回應不得含機密、DSN、檔案路徑、內部例外字串或 PII；
* Docker HEALTHCHECK 不得內嵌真實憑證（映像層、程序參數、
  healthcheck 指令文字皆不得）。

## 後果

* token 模式的已認證容器可正確回報 healthy（HP-01 修復；認證閘
  其餘行為不變——缺/誤憑證照例 401）。
* machine contract（ADR-016）同步更新：`/healthz` 宣告狀態集縮為
  `{200, 405}`（401/500 不再可達）、`x-auth-gate` / `securitySchemes`
  改寫、新增 `x-operational-probe-surface` 宣告；
  `tests/phase3/transport/test_67a_contract_reconciliation.py` 一併
  重校（宣告＝產出的雙向閘）。
* 未來 OIDC/IdP 整合不變難：探針表面在 `Authenticator` 之外、以
  單一封閉集合存在於 transport 邊緣；認證策略仍只有一個呼叫點。
* 認證失敗時 `/healthz` 仍存活（炸彈式 `Authenticator` 例外不再
  影響 liveness）——程序健康不因認證子系統故障而被誤判為死亡。

## 範圍

只改 `/healthz` 的可存取性與文件/測試/契約對帳；認證元件本身
（`auth.py`，含 `hmac.compare_digest` 常數時間比對）不變；無
OIDC、無 RBAC、無部署變更（Phase 6.7B-R2 工作單 §31）。