# ADR-006: Interactive vs Batch Runtime（Phase 6.5 工單 ADR-02）

## Status

ACCEPTED — 2026-10-03

## Context

FIE 資料面有兩種自然的執行模式：

- **interactive read**：client（ChatGPT/未來 client）隨時查詢已持久化的智慧 — 必須低延遲、純唯讀、永不觸發抓取或計算；
- **scheduled batch**：ingestion（adapter → signal_log）與 scoring（run_pipeline → snapshots + evidence graph）需要批次、可重跑、可在離峰時段執行。

Phase 4 之前兩者經由同一個 CLI/orchestrator 進出，語義上可混用：讀取側沒有結構性禁令阻止它去「順手」補資料。

[VERIFIED] 既有 domain 入口：`phase3.api.run_pipeline`（計算+持久化）與 adapter registry + `SignalRepository.upsert_many`（攝入）已分離且各自有生產驗證；缺的是把「批次進入點」明確化、與讀取邊界分開。

## Decision

[DECISION] 建立 `phase3.service.batch.BatchWorker` 作為排程計算的唯一支援進入弧：

- `run_ingestion(adapter_name, input_path)`：註冊表 adapter → `_signal_to_record` → `SignalRepository.upsert_many`（與 CLI 同一條生產轉換路徑）。
- `run_scoring(date_bucket, ...)`：包裝 `run_pipeline(persist=True, ...)` — snapshot + evidence graph 由 orchestrator 的持久化弧原子寫入。
- 建構參數與 interactive 邊界相同的 portable persistence specifier（`FIE_DATABASE_URL` / explicit DSN / SQLite path），兩個平面共享持久層抽象 — 但 process 生命週期獨立。

### 不變量（§12）

```
interactive read path != scheduler/batch orchestration path
```

- `phase3.service.boundary` 的每個操作都是對已持久化狀態的純讀取；**永不**為了回答查詢而呼叫 ingestion/scoring，也**永不**發起網路抓取（§8）。
- 反向亦然：批次路徑不經過 interactive service 邊界。
- [VERIFIED] 測試 (`tests/phase3/service/test_read_path_purity.py`) 以「只餵 fixtures、關閉網路型 adapter 不可達」的資料庫驗證讀取不需攝入。

## Consequences

### 正面

- 未來雲端排程器只需認得 `BatchWorker` 三個函數；ChatGPT 只需認得 5 個唯讀操作。兩個 client face 都極小且穩定。
- 讀取永不寫入 → 多租戶/只讀複本（未來）可直接服務 interactive 面。
- 批次可獨立重跑、暫停、遷移，不影響服務可用性。

### 取捨

- [OPEN] 本階段**不**供應排程器（§20 禁令）：`BatchWorker` 是 API，不是部署。排程器選擇（cron vs cloud scheduler）屬 Phase 6.6+。
- [ASSUMPTION] `run_pipeline` 保持「呼叫即完整管線」的形狀；若未來 scoring 需要分段提交，需擴充 `BatchWorker` 而非 interactive 邊界。