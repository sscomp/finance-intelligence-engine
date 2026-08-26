# Task 生命週期 v0 — Task Lifecycle Contract

狀態：**DRAFT / 非執行階段文件**（Phase 2A 增量產出）
作者：M2
日期：2026-07-08
對應章節：`ARCHITECTURE_REVIEW_PHASE2.md` §5、§6

---

## 0. 適用範圍

本合約定義 Phase 2 將引入的 task lifecycle metadata。**Phase 2A 期間 production
runtime 不產生任何 task_id、不寫 tasks.db、不需要 dispatcher**。本合約純作為
Phase 2B/2C 的 reference contract。

現狀補充：今天（Phase 2A 之前）的「task 識別」隱含在 Hermes cronjob 的
`name + last_run_at` 與 `logs/<job>-<date>.json` 檔名內。Phase 2 不取代此機制，
只是在它之上疊一層 metadata。

---

## 1. 為什麼要自有的 task_id（不依賴 OpenAI / 其他 LLM provider 的 run_id）

- 報告 render 由 Python 模組產出，**未必經過 LLM**（目前 4 個 .py 全部 deterministic，
  無 LLM 介入）。即使未來加 LLM 評論，`run_id` 也只代表「那一次 LLM 呼叫」。
- 一份報告可能跑多個 LLM step（summarize + sentiment + footer），每個 step 都
  有自己的 run_id，**沒有一個是「整份報告」的識別**。
- provider 換了（OpenAI → Anthropic → Ollama），run_id 格式就斷，**audit 線索
  失效**。
- task_id 是 dispatcher / 報告 archive / logs 共有主鍵，必須自有體系。

---

## 2. task_id 格式

```
<report_type>_<yyyymmdd>_<short_uuid>
```

| 部分 | 規則 | 範例 |
|---|---|---|
| `report_type` | enum 4 選 1 | `macro_daily` |
| `yyyymmdd` | Asia/Taipei 觸發日（非 UTC） | `20260708` |
| `short_uuid` | `secrets.token_hex(3)`，6 個 hex char，全域唯一 | `a3f9c2` |

完整範例：`macro_daily_20260708_a3f9c2`

特性：
- **可推導**：給定 report_type + date + 當天序號可重組
- **可排序**：lexicographic = 時間序（同 report_type 內）
- **跨 LLM 穩定**：不依賴 provider
- **collision 機率**：6 hex = 16M 空間，當天 4 個 report × 多 rerun，碰撞風險
  < 0.001%
- **可讀**：人眼能看出「這是 2026/7/8 早上跑的那次 macro_daily」

禁止事項：
- ❌ 不內嵌 LLM provider 名稱
- ❌ 不內嵌 user_id / chat_id
- ❌ 不內嵌 cron_job_id（那是 metadata 欄位）
- ❌ 不使用大寫（避免 log 比對大小寫錯誤）

---

## 3. 狀態機（State Machine）

### 3.1 狀態定義

| 狀態 | 說明 |
|---|---|
| `created` | task 已被建立（dispatcher 收到 POST /tasks 寫入 tasks.db），但尚未排入 worker queue |
| `queued` | 進入 worker queue，等待被撈取 |
| `running` | worker 已撈取，plugin 正在執行（fetch → analyze → render） |
| `waiting_external` | plugin 正在等外部資源（yfinance / RSS / T86 / 瀏覽器），timeout 設定見 §5 |
| `rendering` | 報告文字已生成，準備寫 archive + 送 publisher |
| `publishing` | publisher（目前 = cron agent 送 Telegram）正在送出 |
| `completed` | exit 0 + stdout 非空 → 成功；archive 寫完、publisher 送完 |
| `partial` | exit 0 但資料部分缺失（例如 RSS 當天沒抓到）；archive 寫完、publisher 送出（標記 ⚠️） |
| `failed` | exit ≠ 0 或 dispatcher 層 timeout / exception；error event 寫入 `task_events` |
| `cancelled` | 使用者 / orchestrator 明確取消；已是 terminal 狀態 |

### 3.2 允許的轉換

```
created → queued → running → waiting_external → running → rendering → publishing → completed
                                                          ↘                 ↘
                                                           (失敗)            (失敗)
                                                            ↓                 ↓
                                                          failed           failed

任意非 terminal 狀態 → cancelled（由 orchestrator / 維運觸發）
completed / partial / failed → rerun（建立新 task_id，舊的留 audit）
```

明確**禁止**的轉換：

| 從 | 到 | 原因 |
|---|---|---|
| `completed` | `running` | terminal 不可逆；rerun 開新 task_id |
| `failed` | `running` | 同上 |
| `cancelled` | 任何非 terminal | terminal 不可逆 |
| `created` | `running` | 必須經 queued（讓 worker queue 監控有意義） |

### 3.3 終態（terminal）與非終態

- **非終態**：`created` / `queued` / `running` / `waiting_external` / `rendering` / `publishing`
- **終態**：`completed` / `partial` / `failed` / `cancelled`

---

## 4. 重試 / 重跑語義（Retry / Rerun Semantics）

### 4.1 自動重試（worker 內）

- 觸發條件：`waiting_external` timeout（預設 90s）或 `running` 內 transient exception
- 預設：`max_attempts = 2`、`backoff = 60s`（見 `config/policies/retry.yaml` draft）
- 重試**不開新 task_id**，而是同一 task_id 的 `attempt` 欄位 +1
- attempt 上限由 manifest `retry.max_attempts` 覆寫
- 達上限 → 自動 `failed`，並寫 `task_events` 記錄最後一次錯誤

### 4.2 手動重跑（orchestrator / 維運觸發）

- **永遠開新 task_id**（避免 audit 線索混淆）
- 在 `task_events` 寫 `event=rerun_requested, source=orchestrator, original_task_id=...`
- 新 task 的 `metadata.rerun_of` 欄位指向舊 task_id
- 舊 task 狀態不變（保留 audit），但 UI 可加「已被 rerun」標記

### 4.3 rerun 與 idempotency

- rerun 用 `<report_type>_<yyyymmdd>_<new_short_uuid>` → **新 primary key**
- 同一天的 archive 檔可以共存（logs/2026-07-08.json 是 append 模式）
- SQLite 寫入用 `INSERT OR REPLACE` on `task_id` primary key，避免重複

---

## 5. Timeout 規則

| 階段 | 預設 timeout | 來源 |
|---|---|---|
| `queued` 等待 worker 撈取 | 60 秒 | dispatcher 內部 |
| `running` 全程 | 300 秒（5 分鐘） | manifest `timeouts.running_total` |
| `waiting_external` | 90 秒 | manifest `timeouts.external_per_step` |
| `publishing` | 30 秒 | manifest `timeouts.publishing` |

超時一律轉 `failed`，並寫 `event=timeout, stage=<which>`

---

## 6. Status Query Model（Orchestrator GPT 查詢介面）

### 6.1 查詢粒度

Orchestrator GPT 需要回答「月報跑完了沒」、「上次 macro_daily 為什麼失敗」等
問題。介面（dispatcher API 詳細見 `dispatcher_api_v0.md`）：

| 查詢 | 對應 endpoint | 回傳 |
|---|---|---|
| 某 task 狀態 | `GET /tasks/{task_id}` | 完整 task 物件 |
| 某 report_type 最新一次 | `GET /tasks?report_type=macro_daily&latest=1` | 最新一筆 |
| 某 report_type 某一天 | `GET /tasks?report_type=macro_daily&date=2026-07-08` | 該日 list |
| 失敗任務列表 | `GET /tasks?status=failed&since=24h` | 過去 24h 失敗 |
| 報告全文 | `GET /tasks/{task_id}/artifact` | `report_text` 字串 |

### 6.2 回傳欄位（每個 task 物件）

```json
{
  "task_id": "macro_daily_20260708_a3f9c2",
  "report_type": "macro_daily",
  "status": "completed",
  "created_at": "2026-07-08T08:30:00+08:00",
  "started_at": "2026-07-08T08:30:00+08:00",
  "finished_at": "2026-07-08T08:30:14+08:00",
  "duration_ms": 14000,
  "exit_code": 0,
  "attempt": 1,
  "cron_job_id": "ed214c19c4ac",
  "triggered_by": "cron",
  "metadata": {
    "plugin_version": "1.0.0",
    "template_version": "inline:v1",
    "prompt_version": "1.0.0",
    "data_freshness_days": 1,
    "rerun_of": null
  },
  "error": null,
  "artifact_url": "/tasks/macro_daily_20260708_a3f9c2/artifact",
  "events_url": "/tasks/macro_daily_20260708_a3f9c2/events"
}
```

### 6.3 給 GPT 的「狀態口語化」規則

Phase 2B 之後在 dispatcher 加 `status_summary` 欄位：

| 狀態 | 給 GPT 看的口語 |
|---|---|
| `completed` | 「✅ 報告已產生，產生時間 14 秒前，stdout 長度 2.8 KB」 |
| `partial` | 「⚠️ 報告部分完成：RSS feed 1 個 timeout，但其他 7 個正常」 |
| `failed` | 「❌ 失敗：yfinance API timeout 90s（attempt 2/2）」 |
| `running` | 「⏳ 正在跑（已 25s），目前階段：analyze」 |
| `queued` | 「📋 排隊中（前面還有 0 個 task）」 |
| `waiting_external` | 「⏸ 等待外部 API（yfinance 25/50 完成）」 |
| `cancelled` | 「🚫 已取消（by orchestrator 14:32）」 |

---

## 7. Metadata 欄位（task 物件附件）

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `plugin_version` | semver | Y | BaseReport plugin 版本（Phase 2B 起算） |
| `template_version` | semver | N | Jinja2 template 版本（Phase 2B 末可選） |
| `prompt_version` | semver | Y | cron prompt 版本（Phase 2C 起算） |
| `data_freshness_days` | int | Y | 資料落後 N 天（例：週末 macro 落後 2 天） |
| `rerun_of` | task_id | N | 若此 task 是 rerun，指向原 task_id |
| `cron_job_id` | string | Y | Hermes cronjob ID |
| `triggered_by` | enum | Y | `cron` / `manual` / `rerun` / `orchestrator` |
| `report_text_length` | int | N | stdout 字元數，dispatcher 寫入時計算 |
| `exit_code` | int | Y | 0=ok、1=fail、2=timeout |
| `attempt` | int | Y | 從 1 開始，retry 加 1 |
| `parent_task_id` | task_id | N | 子任務（例：multi-region fetch 的 split） |

---

## 8. 與 OpenAI / 其他 LLM provider 的 run_id 解耦

- **不在 task_id 內嵌** LLM run_id
- LLM 呼叫若發生（Phase 2B 之後 analyst commentary、Phase 2C 之後 dispatcher
  agent loop），把 provider run_id 寫到 `task_events.event_metadata.provider_run_id`
- audit 查詢時，**先以 task_id 為主鍵**，再 join task_events 找 LLM 互動紀錄
- 範例：

```json
{
  "task_id": "macro_daily_20260708_a3f9c2",
  "events": [
    {
      "ts": "2026-07-08T08:30:05+08:00",
      "stage": "running",
      "event": "fetch_completed",
      "meta": {"source": "yfinance", "duration_ms": 2400}
    },
    {
      "ts": "2026-07-08T08:30:10+08:00",
      "stage": "rendering",
      "event": "llm_call",
      "meta": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "provider_run_id": "chatcmpl-AbCdEfGh123456",
        "purpose": "footer_commentary"
      }
    }
  ]
}
```

---

## 9. 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| 0 | 2026-07-08 | Phase 2A 初始 draft。task_id 格式、9 個狀態、轉換規則、retry / rerun 語義、timeout、status query model、metadata、provider 解耦策略。 |
