# ADR-019: 可觀測性、超時與執行時閉合契約（Phase 6.7B-R4）

* 狀態：ACCEPTED (2026-10-04)
* 階段：Phase 6.7B-R4（Observability, Timeout, and Runtime Closure）
* [DECISION] 本 ADR 記錄 R4 的四項執行時契約：(1) 生產安全的結構化
  可觀測性、(2) 有界請求/依賴超時語意、(3) 外部錯誤分類不含內部拓撲、
  (4) PostgreSQL 生產構件與 SQLite 部署矩陣。R1/R2/R3 的行為全部保持
  不變；本 ADR 只新增/收緊 R4 工作單 §4 明確裁定的缺口。

## 1. 外部錯誤拓撲消毒（裁定 §4.3 的機器測試化）

外部/API 面錯誤信封**只攜帶穩定分類與依賴「族」**，永遠不攜帶原始
驅動例外文字（它可命名內部主機名稱、容器/服務名、使用者名或拓撲）：

* 依賴不可用：`DEPENDENCY_UNAVAILABLE`（HTTP 503），
  `details = {"dependency": "<sqlite|postgres>"}` — 封閉的「族」詞彙，
  以及 `details.reason` 只有穩定分類標記（如 `DEPENDENCY_TIMEOUT`）；
  有界的依賴超時（statement/busy timeout 到期）**同一碼** — 重試語意
  完全相同，不另立狀態/碼（工作單 §5.3 的「dependency timeout」由
  `details.reason = DEPENDENCY_TIMEOUT` 穩定表達）。
* 原始驅動細節（含 sanitize 後的訊息）只進入**伺服器端日誌**
  （`fie.service`，URL/DSN/路徑/例外類名已消毒）— 符合「detailed
  diagnostics MAY exist in server-side logs only」。
* 適用點：`get_health`（就緒計數失敗）、freshness 觀測不可讀、以及
  dispatch 的通用例外分支（bounded-window 驅動錯誤分類為
  `DEPENDENCY_UNAVAILABLE` + `reason=DEPENDENCY_TIMEOUT`，其餘維持
  `INTERNAL_ERROR`）。R3 的 open item（`fie-r3-pgdb` 服務名進入信封）
  由本節修復。
* `SCHEMA_INCOMPATIBLE` 信封不變（R3 契約：`details.reason` = 閘門碼，
  本來就不含驅動文字）。

## 2. 請求層/派發超時（§5.2）

`FIE_REQUEST_TIMEOUT` 是**單一超時旋鈕**（ADR-017 詞彙：可解析、
有限、嚴格為正；R1 已拒絕所有非法值）。R4 將同一旋鈕綁定到它命名
的第二個面：

* handler 連線讀取視窗（既有行為，R1 起）；
* **有界派發等待**（新增）：域作業在單一持久層 worker
  （`_OneThreadExecutor`）上執行；等待超過該旋鈕 → 穩定分類
  `OPERATION_TIMEOUT`（HTTP 503，`details.reason =
  DISPATCH_DEADLINE_EXCEEDED`）。被放棄的作業是**只讀**面（§3.5），
  由其依賴自身的視窗（PG statement_timeout、SQLite busy_timeout）
  收束；transport 與 store 都**永不隱式重試/重放**它。
* 依賴視窗本身有限且經 R3 驗證（connect_timeout 10 s、
  statement_timeout 60 s；SQLite busy_timeout 5 s + connect 30 s）。
  「過大的合法值」無上限策略：有限值即可滿足「無無限等待」不變量，
  上限屬 OWNER 決定（本 ADR 不臆測）。
* machine contract 新增 `x-dispatch-timeout-contract` 声明；
  `ErrorEnvelope` 的 code 詞彙新增 `OPERATION_TIMEOUT`（唯一新增的
  公開碼），狀態映射保持與 `HTTP_STATUS_BY_CODE` 完全相等（67A
  對帳測試持續成立）。

## 3. 可觀測性契約（§5.1）

不引入任何新遙測依賴 — 延伸既有 stdlib-logging JSON 事件流：

```text
fie.transport  http_request            既有（每請求；新增 reason 欄位）
               server_started          既有（新增 backend 族欄位）
               shutdown_signal         新增（SIGTERM/SIGINT 名稱）
               server_stopped          新增（clean 布林 + shutdown 耗時）
fie.service    (dispatch 記錄)         既有（每服務呼叫）
               readiness_dependency_failure / freshness_dependency_failure
                                       新增（SERVER-SIDE：消毒後細節）
               dependency_timeout      新增（SERVER-SIDE：消毒後細節）
fie.persistence.postgres  connect/reconnect 警告   既有（masked DSN）
```

* `http_request` 記錄的 `reason` 欄位**由契約構造即穩定**（閘門碼/
  分類標記；驅動文字已由 §1 從信封移除）。
* 生命週期事件（startup/shutdown signal/stopped）使容器編排可看見
  關閉原因與有界清理；`server.shutdown()` 的既有有界輪詢語意保留。
* 欄位名稱確定論、無 secrets、無原始 DSN、無查詢字串、無 Authorization。
* 決定論化收尾（ADR-017 §7 授權 R4）：`FIE_LOG_FORMAT` 非法值由
  靜默回落改為 `INVALID_LOG_FORMAT` 拒絕 — 設定契約自此**無任何
  靜默回落面**；詞彙 `{structured, plain}` 與缺值預設 `structured`
  不變。

## 4. PostgreSQL 生產構件（裁定 §4.2）

Dockerfile 重排為多段：`runtime-base`（不變：PyYAML-only 參考構件、
CA-trust 模式 A/B、uid 10001）→ `runtime-postgres`（**新增目標**：
同一構件 + build 時安裝 `psycopg[binary]>=3.2,<4` — 宣告的
`[postgres]` extra）→ `runtime`（最後段，保持目標-less 預設建置
= PyYAML-only 參考構件，行為不變）。

```bash
docker build --target runtime-postgres -t fie-runtime-postgres:local .
```

* **無執行時 pip install**：psycopg 在映像建置時入圖（構建時的
  普通相依宣告，非容器變異）；
* 兩個最終段僅相依集合不同；HEALTHCHECK/ENTRYPOINT/uid 全同；
* `.[postgres]` 與 `.[test,postgres]` 的 extra 安裝維持
  已接受的部署先決條件（R3 open item #2 的裁決實現）。
* 缺 psycopg（故意不完整構件）→ `open_store` 的既有明確
  ImportError（含 remediation 指引）— 由測試釘住。

## 5. SQLite 部署矩陣（裁定 §4.1 — 機器測試化，不重寫 ADR-017）

| 案例 | local/test | staging/production |
|---|---|---|
| 明確的**絕對** SQLite 路徑（含 `sqlite:///abs`、`immutable_snapshot`） | 允許 | **允許**（ADR-017 §4.4：DSN 形狀閘只拒絕相對路徑） |
| 相對 SQLite 路徑 | 允許（本機相容邊界） | 拒絕 `DATABASE_URL_INVALID` |
| 缺 `FIE_DATABASE_URL`（可攜預設） | 允許 | 拒絕 `DATABASE_URL_MISSING` |
| `FIE_DATABASE_URL=postgres://…` | PostgreSQL | PostgreSQL（唯一 PG 路徑） |
| 未支援 scheme（`postgrex://`、`db://`、`file://`） | SQLite 相對路徑語意 | 拒絕 `DATABASE_URL_INVALID` |
| PG 連線失敗後回落 SQLite | **不存在回落路徑** | **不存在回落路徑**（驅動錯誤帶出；不建立 SQLite 檔案） |

機器測試：`tests/phase3/persistence/test_67b_r4_sqlite_policy.py`。
ADR-017 本體**未被改寫**（無實質矛盾；R3 的解讀經本 ADR 正式裁定）。

## 6. 停機生命週期（§5.6）

既有順序（SIGTERM/SIGINT → stop event → `server.shutdown()` →
`server_close()` → runtime close：store + worker 結束）保留；
新增 `shutdown_signal` / `server_stopped` 事件使清理可觀測。
`runtime.close()` 自本 ADR 起為**冪等**：stop 哨兵入隊後不再於其後
入隊任何工作（否則該工作可能被丟棄且其呼叫者等待到派發期限）。
沒有等待逾時的清理線程被發明 — 現有語意已是有界的
（`serve_forever` 輪詢間隔 + 依賴視窗）；未來若域面擴張為可變面，
需另立 idempotency 契約後才可談重試（本 ADR 不預設）。