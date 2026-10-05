# Codex Cloud Integration Readiness（FIE 外部執行提供者整合之架構與 API 準備）

> **Status**：
>
> ```text
> PROPOSED / FUTURE — READINESS ARCHITECTURE ONLY
> CODEX_CLOUD_INTEGRATION_IMPLEMENTED = false
> CODEX_CLOUD_RUNTIME_ENABLED        = false
> CODEX_CLOUD_DEPLOYED               = false
> ```
>
> 本文件 **不是** 已存在之官方 OpenAI / Codex Cloud API 規格。所有本文件定義的
> contract、schema、欄位、狀態模型，在 repository 內均為 **FIE proposed abstraction**
> （PLACEHOLDER / NOT IMPLEMENTED）。若未來官方 interface 出現在可驗證的官方
> contract 中，本文件須重新以官方 contract 對齊後再升級為 implementation 設計。
> **目前沒有任何程式碼、credential、endpoint、deployment 與此文件對應。**
>
> 與既有 boundary 的差異：`docs/architecture/phase6-5/chatgpt-tool-contract.md`
> 描述的是 **inbound tool serving**（FIE 作為被呼叫的工具）；本文件描述的是
> **outbound external execution**（FIE 作為外部執行提供者的 client／adapter）。
> 兩者方向相反，不得混同。

---

## 1. 目的與非目標

**目的**（本文件交付 = Task E, WO 6.8B）：

- Architecture readiness — logical integration boundary 已定型。
- API contract readiness — submission / status / result / cancellation 概念欄位與狀態模型已提案。
- Security boundary — adapter 不得取得 Production DB credentials、不得直寫 Production。
- Execution model 與 failure semantics — UNKNOWN != SUCCESS 等原則已定義。
- Future implementation boundary — 任何實作需另案 work order。

**非目標**：live integration、credential provisioning、遠端 job、deployment dependency、
任何 runtime 行為變更。本文件不改變任何程式碼。

## 2. Integration topology（proposed）

```text
FIE Runtime
   |
   v
Codex Cloud Adapter（FIE-owned；唯一的整合面；PROPOSED, NOT IMPLEMENTED）
   |   - 不持有 Production DB credentials
   |   - 不繞過 FIE authorization / runtime guard
   |   - 對外僅送 redacted task payload
   v
External Execution Provider（Codex Cloud；PROPOSED target，未接觸）
```

**禁止的 topology**（明確 anti-goal）：

```text
External Agent → unrestricted Production        （FORBIDDEN）
```

Adapter 形態偏好（future implementation 選項，非承諾）：獨立 worker process
或 FIE service 內 bounded module，兩者皆須通過 §6 security boundary 檢核。

## 3. Submission contract（E1 — PROPOSED schema）

```jsonc
{
  "request_id":        "<client_request_id, UUID v4, idempotency key>",
  "job_type":          "<bounded executor profile enum; FIE-side vocabulary>",
  "repository_ref":    "<commit SHA or immutable ref; never a mutable branch tip>",
  "execution_profile": "<sandbox/tool/capability profile reference>",
  "input_artifacts":   [{"ref": "<artifact id>", "sha256": "<digest>"}],
  "timeout_policy":    {"soft_s": 0, "hard_s": 0},   // PROPOSED fields
  "metadata":          {"created_by": "<FIE job record>", "correlation_id": "..."}
}
```

規則：

- 所有欄位為 **proposed**；本文件未經官方 contract 驗證。
- `repository_ref` 必須是不可變 ref（commit SHA）；branch tip 不可作為執行依據。
- `input_artifacts` 只接受 FIE-side artifact refs＋digest，不接受任意 host path。

## 4. Execution status model（E2 — FIE PROPOSED abstraction）

```text
PENDING → ACCEPTED → RUNNING → SUCCEEDED
                             → FAILED
                             → CANCELLED
                             → TIMED_OUT
```

- 此狀態模型為 **FIE 本地抽象（proposed）**；**不得**宣稱為 OpenAI 官方 Codex Cloud
  state model。若官方模型存在且不同，adapter 層須負責映射。
- 狀態轉移必須冪等（見 §5）；任何狀態查詢失敗時維持 `UNKNOWN` 本地狀態，
  不得推斷為成功或失敗（§7）。

## 5. Idempotency / correlation identity（E4 — PROPOSED）

- `client_request_id`：client 產生的 UUID，為 submission idempotency key。
- duplicate submission（同 `client_request_id` 再送）：PROPOSED 語意 = provider 回傳
  與首次 submission 相同的 job identity，**不得產生第二個並行 job**。
- `correlation_id`：跨 FIE audit / log / artifact / provider job 的追蹤鍵，
  進入 FIE-side journal（§8）。
- 邊界：idempotency 的保證範圍是 submission；execution 本身的重試語意由 §7 決定。

## 6. Security boundary（E5 — mandatory, future implementation gate）

Codex Cloud adapter **不得**：

- 直接取得任何 Production DB credentials（DSN password、`PGPASSWORD`、auth token）。
- 直接讀寫 Production database（PostgreSQL raw/intelligence 皆否）。
- 繞過 FIE authorization（auth boundary ADR-012）或 runtime guard
  （`runtime_contract.py` / rehearsal guard / raw-layer boundary）。
- 接收未 redacted secrets（任何 payload 送出前須 redaction pass；
  scanner boundary 沿用 6.7B secret-handling 規則）。
- 自動觸發 Production deploy / restart / schema migration。

Adapter 與 execution provider 之間只攜帶：task 描述、immutable refs、
redacted artifacts、digests。Result 套用回 FIE 的路徑僅允許（future）：
以 bounded import tooling 將 artifact 寫入 **explicit 非生產 target**，再由
operator-approved 流程評估進入 production。

## 7. Failure / retry contract（E6 — fail-safe 原則）

```text
UNKNOWN != SUCCESS
TIMEOUT   != FAILURE_WITH_CONFIRMED_NO_SIDE_EFFECT
```

| Situation | Required semantics（PROPOSED） |
|---|---|
| submission failure（連線/5xx） | 本地狀態維持 `PENDING`；可安全 retry（idempotent by `client_request_id`） |
| authentication failure | 不重試超過 bounded 次數；絕不自動改 credential；alert + stop |
| provider unavailable | `PENDING` 保持；任何已有 `job_id` 的須先查詢真實狀態再決定下一步 |
| timeout（hard timeout 觸發） | 記 `TIMED_OUT`；**不得**假設 provider 側已停止 — 遠端狀態必須以查詢確認 |
| duplicate request | 回傳既有 job identity；不得建立第二個 job |
| lost response（查詢結果遺失） | 本地維持 `UNKNOWN`；重查詢，不得由 timeout 推斷結果 |
| partial artifact retrieval | 已取得 artifacts 記 digest；缺口以 re-fetch 補齊；不得以本地合成補齊 |
| cancellation race | `CANCELLED` 只是本地意圖；provider 側可能已 `SUCCEEDED` — 以查詢為準，兩者不一致時以 provider 為準並記錄 conflict |
| retry ambiguity | retry 前**必須**先確定原 job 狀態；無法確定 → 保持 `UNKNOWN` 並上報 |
| unknown remote state | 只能選擇：(a) 等待並重查詢 (b) 上報 owner；不得自動視同成功或失敗處置 |

## 8. Observability / audit trail（proposed）

- FIE-side journal：每次 submission / 查詢 / 狀態轉移記 `(correlation_id, timestamp,
  state, provider_ref, digest)`，含 sanitized provider 回應（redact）。
- 與 `phase3` 現有結構化 JSON logging 對齊；不新增 log destination。
- 任何 adapter 事件不得寫入 Production database（§6）。

## 9. Result / artifact retrieval contract（E3 — PROPOSED）

```jsonc
{
  "request_id":     "<client_request_id>",
  "job_id":         "<provider job identity>",
  "status":         "<SUCCEEDED|FAILED|CANCELLED|TIMED_OUT|...>",
  "started_at":     "<RFC3339>",          // 皆為 provider 時間戳
  "completed_at":   "<RFC3339>",
  "result_summary": "<bounded text summary>",
  "artifact_refs":  [{"ref": "...", "sha256": "..."}],
  "error":          {"code": "...", "message": "<redacted>"}
}
```

- artifact 一律以 digest 識別；取得後須驗 `sha256` 才可入 FIE-side journal。
- `error.message` 在進入 journal 前必須通過 redaction pass。

## 10. Cancellation / timeout semantics（PROPOSED）

- cancellation 是 **best-effort request**，不是同步保證（見 §7 cancellation race）。
- `soft_s` 過後 adapter 可查詢並紀錄進度；`hard_s` 觸發後本地記 `TIMED_OUT`
  並停止讀取 provider output，但遠端狀態仍須以查詢收斂。

## 11. Readiness checklist — 從 readiness 到 implementation 之間

任何 implementation work order **必須**先具備並逐項覆核：

```text
[ ] 官方可驗證 contract 存在（無則維持 PROPOSED）
[ ] credential provisioning 方案（不落 repo、不落 evidence、0600 外部檔）
[ ] redaction pass 規格已定義並可測試
[ ] adapter 與 FIE guards 的整合測試（raw layer / rehearsal guard 不被繞過）
[ ] §7 每一列 failure semantics 有對映的 harness case
[ ] idempotency key 衝突 / duplicate 行為有斷言
[ ] journal 與既有 observability 對接且不寫 Production DB
[ ] owner authorization（獨立 work order；本 readiness 文件不授權任何實作）
```

## 12. Scope statement

Phase 6.8B 為 documentation publication only。本文件之存在不表示、不授權、
不排程任何 Codex Cloud 整合。任何「integration ready」的表述僅指
architecture / contract readiness。implementation、enablement、deployment
皆須另行授權。
---

## 13. 6.9A-R3 addendum — execution-infrastructure readiness (test side)

本文件的 integration 契約（§1–§12）**不變**（PROPOSED / NOT IMPLEMENTED；
本 addendum 亦不授權任何 live integration）。僅補充一個與之正交的事實紀錄：

**6.9A-R3 已將 isolated test 基礎設施補齊至 fresh-clone re-entry-ready**：

- `scripts/provision-test-postgres.sh` = ONE canonical repository-owned
  ephemeral PostgreSQL provisioning boundary（discovery > pinned、
  sha256-verified portable distribution from Maven Central）。
- `scripts/test-cloud.sh` source 該唯一實作;fresh clone 冷 cache 實證
  acquire → provision → validate → teardown。
- **Codex Cloud execution 仍 NOT VALIDATED**——remediation 是
  repository-ready / re-entry-ready,但一個後續 fresh cloud job 獨立
  rerun 全套 validation 之前不得宣稱 support。credential/integration
  面維持 §6 禁止項不變。
