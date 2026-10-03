# ADR-013: SQLite 部署存取模式契約（immutable_snapshot / readonly / writable）

* 狀態：ACCEPTED (2026-10-03)
* 階段：Phase 6.6R4（唯讀 SQLite 部署契約修復；缺陷 FIE-6R2-001）
* [DECISION] 本 ADR 記錄 SQLite 工件生命週期、存取模式與部署契約。

## 背景

Codex Cloud 6.6R2 Final Acceptance 以**乾淨的 DB-only 唯讀部署**
（僅主 `intelligence.db`，`:ro` 掛載、非 root 消費者）驗證參考
執行時發現：`/healthz` PASS、`/readyz` FAIL（`sqlite3.OperationalError:
unable to open database file`）；同一工件以
`file:<path>?mode=ro&immutable=1` 開啟即可查詢；帶 SQLite
sidecar/WAL 狀態的對照部署則通過冒煙。

根因（本機矩陣重現，`tests/phase3/transport/test_66r4_readonly_sqlite.py`
針對同一機制覆蓋）：

1. **生產者側**：持久層寫入/批次路徑的 SQLite PRAGMA profile
   選 `journal_mode=WAL`（單一寫入者、讀者不擋寫者）。journal
   模式**持久寫入資料庫檔頭**（寫入/讀取版本位元組 0x20/0x20）；
   生產者乾淨關閉後 SQLite 檢查點並移除 sidecar，故正常定案的
   工件是單一 `.db` 檔（無 sidecar），但其檔頭仍是 **WAL 模式資
   料庫**。
2. **消費者側**：任何對 WAL 模式資料庫的開啟（含 `mode=ro`）都
   需要 wal-index 共享記憶體檔（`-shm`，SQLite 依需建立）；該建立
   對資料庫**所在目錄**需要寫入權限。嚴格唯讀目錄/掛載（宣告的
   參考容器部署）兩者皆不可得 → 開啟即
   `OperationalError`（就緒探測與查詢路徑同樣失敗）。
   對照情境成立的原因：**可寫**目錄中的 `mode=ro` 連線可以建立
   `-shm`/`-wal`（實測：開啟後 sidecar 出現並在關閉後留存）——
   這就是「帶 sidecar 狀態的部署通過冒煙」的機制，而非部署本身
   需要它們。
3. `immutable=1` 向 SQLite 宣告工件永不變動 → 省略鎖定與
   WAL/shm → 嚴格唯讀部署可開啟（實測 4/3 計數與領域查詢）。

## 決策

[DECISION] 參考執行時的 SQLite 工件生命週期為 **「immutable 已
發佈快照」模式（工作單 Model A）**（以文件證據選定，非由
`immutable=1` 成功反推）：引用證據 —

* `docs/architecture/phase6-6/reference-runtime.md` §9.1：宣告的
  參考容器部署即 `-v <host-data-dir>:/data:ro`（嚴格唯讀掛載 —
  掛載期間工件不可被更新，sidecar 不可建立）；
* ADR-006：互動讀取平面（純唯讀）與批次寫入平面（獨立行程生命
  週期）分離；批次路徑（`BatchWorker`）不經過唯讀服務邊界；
* `phase3.service.boundary`：服務為 query-only，**永不**建立
  ad-hoc schema（schema 由生產者遷移定案）。

同時 SQLite 存取模式作為顯式、經驗證的部署契約開關：

| 模式 | 語意 | 用途 |
|---|---|---|
| `writable`（預設） | 歷史讀寫開啟 + 完整 PRAGMA profile（`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout`） | 生產者/批次/CLI/留存/備份 |
| `readonly` | `file:...?mode=ro` URI；跳過 `journal_mode` PRAGMA；WAL 工件**需要可寫 sidecar 目錄**（顯式、可攜要求） | 有可寫檔案系統空間的讀者 |
| `immutable_snapshot` | `file:...?mode=ro&immutable=1`；跳過 `journal_mode` PRAGMA；sidecar 既不要求也不建立；工件在讀者存活期間**不得變動** | 宣告的唯讀容器部署（`:ro`） |

設定面：`FIE_SQLITE_ACCESS_MODE`（預設 `writable` — 完全向後相
容；非法值回退安全預設、有測試；不為其他後端擴散 —
PostgreSQL 只接受預設模式，其唯讀對應是
`set_query_only()`，由服務邊界獨立施加）。

### 生命週期明細（§10 項目）

```
1. 誰建立 DB  —— 生產者平面：批次/CLI/管線（SQLiteStore
   writable 預設模式；不存在則建立檔案）。
2. 誰擁有寫入 —— 只有生產者/批次平面；互動服務面為
   query-only（結構性禁令，ADR-006）。
3. 部署 DB 能否在服務存活期間變動 —— immutable_snapshot：
   否（契約禁令）；readonly：讀者參與 live 鎖定，可觀察
   有效更新（需可寫 sidecar 空間）；writable：同 readonly
   開啟語意加上寫入能力（生產者/批次自己）。
4. 支援的 SQLite 連線模式 —— 如上表；由
   FIE_SQLITE_ACCESS_MODE / open_store(access_mode=…)
   顯式選擇；未知值在建構/啟動時被拒（ValueError）。
5. journal/WAL 期望 —— 生產者以 WAL 寫入（讀者不擋單寫
   者）；journal 模式持久於檔頭；唯讀部署對 WAL 工件依
   immutable 語意開啟，或（readonly 模式）需要可寫 sidecar。
6. 工件定案 —— 生產者乾淨關閉全部連線：SQLite 自動檢查點
   並移除 `-wal`/`-shm`；要發佈的工件單檔；**不得**發佈帶
   殘留 sidecar 的目錄（不是有效定案工件）。
7. sidecar 要求 —— immutable_snapshot：不需要、不建立；
   readonly + WAL 工件：需要可寫的目錄供 SQLite 建立；
   writable：正常 SQLite 語意。
8. 唯讀檔案系統/掛載 —— 由 immutable_snapshot 支援（宣告
   的容器拓撲）；readonly 模式在嚴格唯讀目錄開啟 WAL 工件
   會如實失敗（OperationalError；不靜音、不降級）。
9. 就緒行為 —— /readyz（get_health：counts + schema 版本）
   與查詢路徑使用同一存取模式的同一連線語意；就緒錯誤
   如實回報 503，訊息經 6.6R1 消毒。
10. 營運限制 —— immutable 快照是固定時點；需即時更新的
    部署不得使用 immutable；工件在 immutable 讀者存活期
    間被修改屬操作者違約（偵測依賴鎖定的路徑不存在）。
```

## 後果

* 乾淨 DB-only 唯讀部署（Codex Cloud 失敗拓撲）在其宣告契約
  上成立：`/healthz`、`/readyz`、領域查詢全綠，部署目錄僅主
  工件、無任何 sidecar 建立或嘗試（容器級驗證見 6.6R4 報告）。
* 生產者/批次/CLI寫入路徑（Phase 3B/6.3 定型行為）零語意變動；
  預設模式即歷史行為，全部既有測試不動。
* PostgreSQL 後端不受影響（存取模式為 SQLite 部署契約；
  open_store 對非預設值 fail-closed）。
* [VERIFIED] 契約由 `tests/phase3/transport/test_66r4_readonly_sqlite.py`
  （清潔 DB-only 讀取、無 sidecar 依賴、唯讀掛載語意、/readyz
  成功 schema、代表性領域查詢、可寫工作流不變、非法/錯用
  immutable 防護、設定面串接）驗證；容器級由 6.6R4 動態驗收
  閘重現。