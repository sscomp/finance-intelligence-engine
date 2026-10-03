# Phase 6.5 — Service Planes（§21 架構圖）

Status: frozen 2026-10-03。圖中「既有」= 本階段未變動的生產代碼；
「Phase 6.5」= 本階段新增。

## 互動（讀取）平面

```
CLIENT PLANE
ChatGPT / future clients                      [現今: 契約文件 + 測試, §20 禁止發佈]
        |
        v
SERVICE PLANE — Phase 6.5 新增
phase3.service
  IntelligenceService (Protocol, 5 唯讀操作)
  typed contracts / error taxonomy / freshness vocabulary
  reference adapter (dispatch → JSON envelope)
        |
        v
APPLICATION / DOMAIN PLANE — 既有確定性邏輯, 未變動
phase3 scorers / orchestrator / freshness policy / graph writer
        |
        v
PERSISTENCE PLANE — Phase 6.3 抽象
DatabaseStore seam + repositories / migrations
SQLite (dev/test)  |  PostgreSQL (cloud target)
```

## 排程（批次）平面 — 獨立執行弧

```
SCHEDULING PLANE
future cloud scheduler                        [本階段不供應排程器, §20]
        |
        v
BATCH WORKER — Phase 6.5 新增
phase3.service.batch.BatchWorker
  run_ingestion / run_scoring(=run_pipeline persist)
        |
        v
same APPLICATION / DOMAIN + PERSISTENCE contracts
```

不變量（§12）：互動讀取路徑 ≠ 排程/批次路徑 — 兩個平面共享持久層
抽象，但永遠不共用呼叫弧：讀取永不觸發攝入/計算，批次永不經過
唯讀服務邊界。

## 明確標記（§21 要求）

```
A3          = engineering workstation only / NOT REQUIRED
              (Phase 6.5 無任何 A3 執行期依賴; 可攜性測試涵蓋)
AEE         = NOT REQUIRED
              (AEE runtime bridge 與 AEE worktrees 完全未動 — 位置不入本 repo 文件)
abacus-claw = existing deployment outside Phase 6.5 scope
              (不連線、不檢查、不讀取、不以此設計)
```

## Runtime 注入面

```
environment (provider-neutral)
  FIE_DATABASE_URL / FIE_DATA_DIR / FIE_CONFIG_DIR / FIE_ARTIFACT_DIR
  FIE_SERVICE_ENV / FIE_LOG_FORMAT
        |  load_runtime_config()  [explicit > env > 可攜 default]
        v
service plane + batch worker (共享 resolve_spec / open_store seam)
```

機密僅經環境注入；config 的可記錄檢視只含消毒後 DSN（ADR-009）。