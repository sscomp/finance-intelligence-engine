# Phase 6.6 可部署雲端參考執行時與傳輸閘門（reference runtime）

* [VERIFIED] 本文件描述的行為由 `tests/phase3/transport/` 針對真實
  HTTP 與真實 SQLite/PostgreSQL 執行時驗證；測試指令見文末。

## 1. 架構與傳輸/服務分離 (§23)

```
ChatGPT / 其他遠端唯讀消費者
        │  HTTP (GET only)
        ▼
phase3.transport  ── 本階段新增的「最薄」傳輸層 (ADR-011/012)
  解析、認證、request id、分派、錯誤→HTTP 映射、序列化、遙測
        │  IntelligenceService（Phase 6.5 凍結契約, ADR-005）
        ▼
phase3.service (read_only) ── get_health / get_latest_intelligence /
  get_entity_intelligence / get_evidence / get_freshness
        │  (Phase 6.3 定型的持久層；連線執行緒親和由
        │   _OneThreadExecutor 於傳輸側保護)
        ▼
SQLite / PostgreSQL（僅持久層 seams；無 SQL 在傳輸層）
```

* 傳輸層紀律（§3.1）：只有上述六類工作；無評分、無 SQL、無攝取、
  無排程、不重複 freshness 邏輯（freshness 仍由
  `phase3.service.freshness` over 未變更的治理策略計算）。
* 靜態稽核測試（`test_portability_audit.py`）逐行保證傳輸層只
  import 凍結服務面與持久層 seams。

## 2. HTTP 契約 (§5)

機器可讀契約：[`http-api-contract.json`](http-api-contract.json)。

| HTTP (GET／HEAD) | Phase 6.5 工具 | 服務操作 |
|---|---|---|
| `/healthz` | — (liveness，不觸及持久層) | — |
| `/readyz` | — (readiness) | `fie.health` → `get_health` |
| `/v1/health` | `fie.health` | `get_health` |
| `/v1/intelligence/latest?kind=&limit=` | `fie.latest_intelligence` | `get_latest_intelligence` |
| `/v1/intelligence/entity/{kind}/{entity_id}?as_of=` | `fie.entity_intelligence` | `get_entity_intelligence` |
| `/v1/evidence/{ref}?limit=` | `fie.evidence` | `get_evidence` |
| `/v1/freshness/{kind}/{entity_id}?as_of=` | `fie.freshness` | `get_freshness` |

* 回應體是 Phase 6.5 dispatch 信封**原樣**；`schema_version` 為
  `"6.5"`；`X-Request-Id` 回應頭與信封 `request_id` 一致（可由
  客戶端提供、經清洗；非法字元與連續 `.` 移除）。
* 非 GET 方法一律 `405`（`Allow: GET, HEAD`；互動平面唯讀 §3.5）。
* HTTP 狀態映射（§7）：INVALID_REQUEST→400、UNAUTHENTICATED→401、
  FORBIDDEN→403、NOT_FOUND→404、STALE_DATA→409、
  DATA_UNAVAILABLE/DEPENDENCY_UNAVAILABLE→503、INTERNAL_ERROR→500。
  DEGRADED 是 freshness 狀態，以 HTTP 200 + 元資料誠實呈現。

## 3. 認證 (§6)

* `FIE_AUTH_MODE=none`：anonymous（dev/test；預設綁定 127.0.0.1）。
* `FIE_AUTH_MODE=token`：`Authorization: Bearer <FIE_AUTH_TOKEN>`；
  缺失/無效 → **401** `UNAUTHENTICATED`；token 缺失時 fail-closed
  拒絕啟動；常數時間比較；主體取自 `FIE_AUTH_PRINCIPAL`。
* 憑證永不入源碼/映像/遙測/錯誤；詳細見
  [ADR-012](../../adr/adr-012-auth-boundary.md)。

## 4. 設定 (§9)

環境變數（優先序：明確參數 > 環境 > 安全預設；全部有測試）：

| 變數 | 預設 | 說明 |
|---|---|---|
| `FIE_HTTP_HOST` | `127.0.0.1` | 綁定主機（永不隱式公網綁定） |
| `FIE_HTTP_PORT` | `8787` | 綁定埠（`0` = 暫時埠） |
| `FIE_AUTH_MODE` | `none` | `none`/`token` |
| `FIE_AUTH_TOKEN` | （無） | token 模式憑證；僅環境，never default/never logged |
| `FIE_AUTH_PRINCIPAL` | `service-consumer` | token 呼叫者的不透明主體標籤 |
| `FIE_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `FIE_REQUEST_TIMEOUT` | `60` | 每連線逾時（秒） |
| `FIE_DATABASE_URL` | 可攜 SQLite 預設 | 持久層目標（Phase 6.3 契約） |

## 5. 健康與就緒 (§10)

* `/healthz`：程序存活（完全不觸及持久層）。
* `/readyz`：服務初始化 + 持久層可達（包裝 `get_health`）。
  不要求任何 live Yahoo/TWSE/RSS 抓取；stale/degraded 狀態
  以 `freshness` 元資料誠實呈現（§8）。

## 6. 遙測 (§11)

`fie.transport`（結構化 JSON：`{"event": "http_request", "request_id",
"principal_id", "operation", "method", "path", "http_status", "status",
"duration_ms", "freshness_state"?, "as_of"?, "error_code"?}`）與
`fie.service`（Phase 6.5 遙測）。
**永不**記錄：Authorization 值、token、查詢字串、payload、
含憑證的完整 DSN、堆疊追蹤（客戶端回應體亦然）。

## 7. 啟動 (§12)

任何 CWD、任何主機（封裝後）：

```bash
# 封裝內（建議）
python -m pip install -e .
fie-http-server                       # 127.0.0.1:8787, FIE_DATABASE_URL 環境
# 或
python -m phase3.transport.http
```

```
# 最小啟動冒煙
FIE_DATABASE_URL=sqlite:///tmp/fie66-smoke.db fie-http-server &
curl -s http://127.0.0.1:8787/healthz          # → {"status":"alive", ...}
curl -s http://127.0.0.1:8787/readyz | head -c 300
```

* `SIGTERM`/`SIGINT` → `stop.wait` → `server.shutdown()` →
  `server_close()` → `runtime.close()`（工作者執行緒擁有的 store
  在**它自己的**執行緒上關閉）→ 乾淨退出 0。
* SQLite 執行時測試閘（§14）：
  `python -m unittest discover -s tests/phase3/transport -t .`
  （含 `test_http_contract.py`）。
* PostgreSQL 閘門（§15）：見
  [pg 參考執行時與同構性](#8-postgresql-閘門與同構性-§15§16)，DSN
  同 Phase 6.4 一次性叢集配方；不落任何生產資料。

## 8. PostgreSQL 閘門與同構性 (§15／§16)

* 同一 HTTP 契約套件對一次性 PostgreSQL 執行
  （`tests/phase3/transport/test_pg_runtime.py`；叢集不可用時
  skip-with-reason，不造假）。
* 傳輸層 SQLite/PostgreSQL 同構：信封鍵完全一致；內容僅在
  倉庫核准的易變標準化後比較（生成 request_id、run_id、時間
  戳/時長、`snapshot_id`/`_score_id` 序號、`backend_kind` 自述、
  `ipr-<hex12>` 遮罩）——與 Phase 6.5 跨後端同構規則相同。
* 拆除驗證：每個測試類別結束時 server shutdown → store 於
  擁有它的執行緒上關閉 → 暫存 SQLite 檔刪除。

## 9. 容器（§13，參考構建）

```bash
docker build -t fie-reference-runtime:local .
docker run --rm -p 127.0.0.1:8787:8787 \
  -e FIE_DATABASE_URL=/data/intelligence.db \
  -v <host-data-dir>:/data:ro \
  fie-reference-runtime:local
curl -s http://127.0.0.1:8787/healthz
```

* 非 root（uid/gid 10001）、明確埠 8787、HEALTHCHECK 接
  `/healthz`、無內嵌憑證/生產資料/主機特定掛載；基礎映像摘要
  由部署端在部署時釘選（見 Dockerfile 註記）。
* [OPEN] 本主機的 docker socket 為 root:docker 660 而作業階段
  使用者不在 `docker` 群組且無免密 sudo——**動態 build/run 冒煙
  未在本工作階段執行**；靜態不變量由測試
  （`TestDockerfileStaticAudit`）保證。具備權限後可執行上述命令
  完成冒煙（記錄於報告 §13）。
* 本工單**不**部署到生產（§24）。

## 10. 乾淨房重現 (§21)

```bash
git clone <remote> /tmp/fie66-cleanroom -b feature/fie-phase6-6-cloud-runtime-transport
cd /tmp/fie66-cleanroom && git rev-parse HEAD   # 與 REMOTE 比對
python -m venv /tmp/fie66-venv && source /tmp/fie66-venv/bin/activate
pip install -e ".[test,postgres]" && pip check
python -m unittest discover -s tests -t .        # 全回歸
python -m unittest discover -s tests/phase3/transport -t .   # HTTP 契約
fie-http-server &                                 # 自任意 CWD 重複 §12 冒煙
```

（一次性 PostgreSQL 叢集建立配方見 Phase 6.3 報告；§21 禁用
A3 venv/DB、開發快取、Hermes 狀態。）

## 11. 安全假設與限制（§19）

* 生產身份提供者**不**在本階段提供；token 模式為單一共用憑證。
* 參考執行時預設僅回環綁定；公網部署需操作者明確設定
  `FIE_HTTP_HOST`（設定優先序有測試；安全稽核有測試）。
* 錯誤信封永不包含堆疊/SQL/DSN/路徑/內部表示（測試覆蓋）。
* 互動平面唯讀；任何寫入路徑不存在於 HTTP 層（405 + 靜態稽核）。

## 12. 非目標 (§24)

不生產部署、不資料遷移、不上排程器、不修改券商/Telegram 介面、
不改變評分節點身分、不自動啟動 Phase 6.7。