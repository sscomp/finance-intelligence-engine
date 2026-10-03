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
| `FIE_SQLITE_ACCESS_MODE` | `writable` | SQLite 部署存取模式（6.6R4/ADR-013）：`writable`（生產者/批次歷史行為）、`readonly`（`mode=ro` 讀取；WAL 工件需可寫 sidecar 空間）、`immutable_snapshot`（宣告的唯讀容器部署：僅主 `.db` 工件、讀者存活期間工件不得變動） |

## 5. 健康與就緒 (§10)

* `/healthz`：程序存活（完全不觸及持久層）。
* `/readyz`：服務初始化 + 持久層可達（包裝 `get_health`）。
  不要求任何 live Yahoo/TWSE/RSS 抓取；stale/degraded 狀態
  以 `freshness` 元資料誠實呈現（§8）。
* `/readyz` 與查詢路徑使用**同一宣告的存取模式**（6.6R4/ADR-013）：
  `FIE_SQLITE_ACCESS_MODE` 決定 SQLite 部署開啟語意，就緒探測與
  領域查詢共享同一連線語意（唯讀部署以 `immutable_snapshot` 開啟
  DB-only 工件；不存在的工件不會被自動建立；就緒錯誤如實回報
  503，訊息經 6.6R1 消毒 — 內部例外/SQL 細節不入回應體）。

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

### 9.1 一般構建與唯讀部署（標準公開 CA 信任 — Mode A）

```bash
docker build -t fie-reference-runtime:local .
docker run --rm -p 127.0.0.1:8787:8787 \
  -e FIE_DATABASE_URL=/data/intelligence.db \
  -e FIE_SQLITE_ACCESS_MODE=immutable_snapshot \
  -v <host-data-dir>:/data:ro \
  fie-reference-runtime:local
curl -s http://127.0.0.1:8787/healthz
curl -s http://127.0.0.1:8787/readyz
```

* **SQLite 部署工件契約（6.6R4/ADR-013）**：所部署的是*已定案的*
  主 `intelligence.db` 工件（單檔）；`immutable_snapshot` 模式下
  執行時以 `file:<path>?mode=ro&immutable=1` 開啟 —— 任何
  `-wal`/`-shm`/`-journal` sidecar **既不要求也不被建立**；唯讀
  掛載（`:ro`）即為宣告的參考部署拓撲。
* 部署工件由生產者（批次/CLI 寫入路徑）在乾淨關閉下定案：SQLite
  於最後一條連線乾淨關閉時檢查點並移除 sidecar；不要部署帶有殘留
  sidecar 的目錄（生產者中途當掉的情境不構成有效工件)。
* 若操作者提供**可寫**的資料目錄且需在服務存活期間供應即時更新，
  則不宣告 immutable 語意：可顯式選 `readonly`（WAL 工件需可寫
  sidecar 空間）或保留預設 `writable`。預設值維持歷史行為
  （`writable`），全部有測試（`test_66r4_readonly_sqlite.py`）。

* 非 root（uid/gid 10001）、明確埠 8787、HEALTHCHECK 接
  `/healthz`、無內嵌憑證/生產資料/主機特定掛載；基礎映像摘要
  由部署端在部署時釘選（見 Dockerfile 註記）。
* 本工單**不**部署到生產（§24）。動態容器冒煙（build/run/
  health/teardown）已於 Phase 6.6R closure gate 關閉。

### 9.2 Container CA 信任契約（Phase 6.6R3 — 雙模式、加法性）

TLS 驗證**永不關閉**。Docker build 階段跑在隔離容器裡，**不**繼承
建構主機的信任儲存或代理 CA 設定（host trust ≠ build-stage
trust）；且 pip 以其自帶的 certifi bundle 為預設信任，不讀發行版
系統 bundle。因此：

* **Mode A（預設；標準公開信任）**：不供應任何 CA 輸入時，pip 以
  自帶公開 CA bundle 直接驗證 PyPI（任何 TLS 驗證旁路 flag 一律
  不使用，靜態稽核有測試）；映像不帶任何自訂 CA 工件。
* **Mode B（選配；環境授權的額外 CA）**：當構建環境的出站 HTTPS
  會經過授權的私有/企業 CA 攔截時，由環境在構建時注入**一枚**
  額外 PEM CA 憑證（BuildKit secret；憑證由執行環境提供、非倉庫
  內容、須為 PEM 且為授權之信任錨）：

  ```bash
  DOCKER_BUILDKIT=1 docker build \
    --secret id=authorized_extra_ca,src=/authorized/path/ca.pem \
    -t fie-reference-runtime:local .
  ```

  該 CA 會經 `update-ca-certificates` **加入**系統信任束
  （`/etc/ssl/certs/ca-certificates.crt`——加法而非取代，標準公開
  CA 仍然受信），pip 以 `--cert` 指向該**增補後**的系統 bundle
  完成本次的套件安裝驗證；未供應 secret 時此分支完全跳過，
  一般構建不受影響。secret 機制使構建輸入不進 build context、
  環境變數與映像 metadata；映像僅在 Mode B 時刻意攜帶增補的
  信任材料（公開憑證，永不私鑰）。**建置快取注意**：BuildKit
  刻意不將 secret 內容納入 layer cache key（安全設計）——與先前
  Mode A 構建共用快取的 Mode B 構建會重用**未注入**的層；因此
  Mode B 構建一律以 `--no-cache` 執行（A3 動態驗證已實測此行為）。

* 無任何憑證/私鑰入 Git；測試之合成 CA 於執行期即時生成、用畢
  即刪（`tests/phase3/transport/test_ca_trust_audit.py`：靜態
  trust 契約稽核 + 合成 TLS fixture——未經注入遭預設驗證拒絕、
  經增補束接受且公開 CA 保留）。

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

* SQLite 部署生命週期（6.6R4/ADR-013）：`immutable_snapshot` 模式
  的讀者假設工件在其存活期間**永不變動**（SQLite `immutable=1`
  省略鎖定與 WAL/shm）；執行時不得修改該工件（寫入路徑不存在
  於服務/傳輸面），操作者也不得在讀者存活期間替換/更新檔案 —
  需要即時更新時請使用 `readonly` 或 `writable` 模式與可寫
  sidecar 空間。`readonly` 模式對 WAL 工件要求可寫的
  sidecar 目錄（顯式、可攜的要求，測試覆蓋該差異）。

* 生產身份提供者**不**在本階段提供；token 模式為單一共用憑證。
* 參考執行時預設僅回環綁定；公網部署需操作者明確設定
  `FIE_HTTP_HOST`（設定優先序有測試；安全稽核有測試）。
* 錯誤信封永不包含堆疊/SQL/DSN/路徑/內部表示（測試覆蓋）。
* 互動平面唯讀；任何寫入路徑不存在於 HTTP 層（405 + 靜態稽核）。

## 12. 非目標 (§24)

不生產部署、不資料遷移、不上排程器、不修改券商/Telegram 介面、
不改變評分節點身分、不自動啟動 Phase 6.7。