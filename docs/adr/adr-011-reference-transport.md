# ADR-011: 參考 HTTP 執行時採用標準庫 ThreadingHTTPServer 與執行緒親和執行器

* 狀態：ACCEPTED (2026-10-03)
* 階段：Phase 6.6（可部署雲端參考執行時與傳輸閘門）
* [DECISION] 本 ADR 記錄傳輸層技術選型與執行緒模型。

## 背景

Phase 6.5 交付了與傳輸方式無關的唯讀服務邊界
（`phase3.service`）。Phase 6.6 需要在其上提供一個「最薄」的
HTTP 參考執行時（§4），且不得引入新依賴（§4：minimize new
dependencies）、必須保持 A3 獨立性（§3.3）、且不得修改
Phase 6.3 定型的持久層執行緒行為。

## 決策

1. [DECISION] 採用 Python 標準庫 `http.server.ThreadingHTTPServer`
   作為參考執行時。零新增依賴、供應商中立、可直接由
   `console_scripts` 進入點 `fie-http-server`（或
   `python -m phase3.transport.http`）啟動。**不**引入 FastAPI/
   Starlette/uvicorn：它們會增加可攜性負擔並暗示特定廠商雲端形態。
   未來如需 ASGI，可在同一 `IntelligenceService` 邊界上另寫薄適配層
   （傳輸層可替換性見 ADR-010）。
2. [DECISION] 執行緒模型：SQLite 連線具有執行緒親和性
   （`sqlite3.check_same_thread`，Phase 6.3 定型行為），而 HTTP
   伺服器在每請求自己的執行緒上服務。傳輸層因此引入
   `_OneThreadExecutor`：單一持久化工作者執行緒擁有 store/service
   （**建構**也發生在該執行緒上，保證連線親和），所有領域呼叫經
   佇列轉發、例外在呼叫端執行緒重現。這是**傳輸側排程**而不是
   持久層修改（§3.1 紀律；ADR-011 註記）。
3. [DECISION] 路由表（每個 HTTP 操作都有顯式的 Phase 6.5 契約
   對應，§4）：

   | HTTP | Phase 6.5 工具 | 服務操作 |
   |---|---|---|
   | GET /healthz | （僅 liveness） | 不觸及持久層 |
   | GET /readyz  | （readiness） | `get_health` 呼叫包裝 |
   | GET /v1/health | `fie.health` | `get_health` |
   | GET /v1/intelligence/latest | `fie.latest_intelligence` | `get_latest_intelligence` |
   | GET /v1/intelligence/entity/{kind}/{entity_id} | `fie.entity_intelligence` | `get_entity_intelligence` |
   | GET /v1/evidence/{ref} | `fie.evidence` | `get_evidence` |
   | GET /v1/freshness/{kind}/{entity_id} | `fie.freshness` | `get_freshness` |

4. [DECISION] HTTP 回應體即 Phase 6.5 dispatch 信封**原樣**
   （`schema_version`／`kind`／`request_id`／`principal_id`／
   `status`／`freshness`／`payload`／`evidence_refs`／`warnings`
   或錯誤形），HTTP 狀態碼是確定性映射（ADR-005/契約 JSON），
   不取代領域錯誤碼。
5. [VERIFIED] 機器可讀契約：
   `docs/architecture/phase6-6/http-api-contract.json`（OpenAPI 3.1
   形式），由 `tests/phase3/transport/test_http_contract.py` 與
   `test_pg_runtime.py` 針對真實 HTTP 資料驗證。
6. [ASSUMPTION] 單一持久化工作者執行緒足以承載參考執行時的
   互動式讀取負載（ChatGPT 工具/人工查詢量級）。若未來需要平行讀，
   應擴充 `IntelligenceService` 的工廠模式（一連線一服務）而非
   在傳輸層快取領域資料。

## 後果

* 傳輸層零依賴、可從任意 CWD 啟動、容器映像極小。
* 領域例外跨越執行緒邊界原樣重现；服務層可測性不變。
* 非目標（§24）：不提供平行讀取擴充、不承諾任何雲端廠商、
  不在傳輸層做批次/攝取。