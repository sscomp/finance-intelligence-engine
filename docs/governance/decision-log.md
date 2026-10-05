# 決策軌跡

本文件記錄 FIE 演進過程中的關鍵決策，依時間順序排列。每項決策包含問題背景、決策內容、後果與取捨。

---

## 2026-07-08 — Phase 3A 架構：分層評分 + 純函式 Scorer

**Context**：既有報表腳本的 scoring 邏輯散落在各腳本中，無法跨層引用、無法解釋、無法重放。

**Decision**：建立獨立的 `phase3/scoring/` 模組，所有 Scorer 為純函式 `score(context) -> ScoreResult`，config 驅動，不寫死於程式碼。

**Consequences**：Scorer 可獨立測試、可重放、可平行。代價是需要額外的 Adapter 層將原始資料轉為 Signal。Phase 3 不 import 既有腳本，確保零破壞。

**Related**：`phase3/scoring/macro.py`、`phase3/scoring/industry.py`、`phase3/scoring/company.py`

---

## 2026-07-08 — Phase 3B：SQLite 持久層 + Signal Adapter 模式

**Context**：需要將信號持久化以支援歷史查詢與圖譜建構。

**Decision**：以 SQLite 為持久層，建立 `signal_log`、`score_snapshot`、`graph_nodes`、`graph_edges` 四張表。信號擷取透過 Adapter 模式（yfinance、T86、RSS、fixture）統一轉換。

**Consequences**：輕量、無外部依賴。Adapter 模式允許新增資料來源不需修改核心。代價是 SQLite 的並發限制（適合單機、不適合多寫入者）。

**Related**：`phase3/persistence/`、`phase3/signals/adapters/`

---

## 2026-07-11 — Phase 4：研究圖譜查詢模組化

**Context**：圖譜查詢（lineage、blast radius、cross-layer impact）最初委派給 EvidenceTracer 的 BFS，但方向語意在不同 edge type 上不一致。

**Decision**：每個查詢家族獨立模組，各自維護 per-edge-type direction policy table，不依賴 EvidenceTracer 的 delegation。

**Consequences**：查詢行為可預測、可測試。代價是 direction policy 的重複定義，但每個查詢的語意更清晰。

**Related**：`phase3/graph/blast_radius.py`、`phase3/graph/lineage.py`、`phase3/graph/cross_layer_impact.py`

---

## 2026-07-11 — Phase 4：Shadow-Run 兩層重放模式

**Context**：需要驗證 Pipeline 的確定性，但歷史 artifact 可能不包含 scorer 的完整輸入。

**Decision**：`structured_copy`（預設，從 artifact 直接複製決策）與 `real_replay`（從記錄的 inputs 重新執行 scorer）兩種模式。缺少 inputs 時自動 fallback 到 structured_copy。

**Consequences**：確保向後相容（舊 artifact 仍可重放），同時支援更嚴格的重新執行驗證。schema bumped 1→2（additive）。

**Related**：`phase3/pipeline/shadow_run.py`、ADR [ADR-001](../adr/adr-001-real-score-bridge.md)

---

## 2026-08-09 — FIE Real-Score Bridge：macro_history.db → signal_log

**Context**：Phase 3 的評分引擎完整實作且通過測試，但生產 cron 腳本未將資料餵入 Pipeline，導致 artifact 只有 macro 分數、company/industry 分數為空。

**Decision**：建立 `phase3/bridge/seed_signals.py`，將 `macro_history.db` 的三張表（macro_daily、stock_monthly、institutional_daily）映射為 Phase 3 Signal 記錄。使用 `resolve_as_of_dates` 實作確定性的 as-of 日期解析。

**Consequences**：Pipeline 可讀到真實的 company/industry 信號，產出非預設分數。代價是新增了一個 Bridge 層需要維護映射邏輯。

**Related**：`phase3/bridge/seed_signals.py`、[ADR-001](../adr/adr-001-real-score-bridge.md)

---

## 2026-08-09 — Phase 5 M2-M6：投資組合決策引擎

**Context**：需要將評分引擎的輸出轉化為可操作的投資組合建議。

**Decision**：建立 `phase3/portfolio/` 子套件，依序實作領域模型（M2）、風險指標（M3）、配置引擎（M4）、執行規劃（M5）、報告（M6）。每個里程碑附帶安全守護（TD7/TD8）。

**Consequences**：完整的投資組合決策鏈，從分數到配置到執行規劃。安全守護確保不會產出不合理的配置（集中度過高、回撤過大等）。

**Related**：`phase3/portfolio/`、[M6 里程碑](milestones/m6.md)

---

## 2026-08-09 — Phase 5 M7：工程完成驗證

**Context**：M2–M6 全部完成後，需要一個 gate milestone 驗證整體工程品質。

**Decision**：M7 為 gate milestone（無程式碼交付），驗證 4-tier test matrix 全部 PASS + 0 生產安全違規 + SSOT 升級。

**Consequences**：確認 Phase 5 工程完成，解除 M8 阻塞。代價是 M7 本身不產出新功能，但提供了整體品質保證。

**Related**：[M7 里程碑](milestones/m7.md)

---

## 2026-08-10 — FIE 資料新鮮度守護

**Context**：系統無統一的新鮮度/過時定義，每個 cron job 有隱含的更新週期但無顯式的 stale 判定。

**Decision**：實作 `phase3/freshness.py`，提供交易日感知的 staleness 分類（FRESH / STALE / HARD_EXPIRED），CLI `--freshness-check` 旗標在 Pipeline 執行時自動檢查。

**Consequences**：使用者可以在執行前知道資料是否過時。假日感知避免誤判。代價是需要維護 `config/holidays.json`。

**Related**：`phase3/freshness.py`、`config/holidays.json`、[ADR-002](../adr/adr-002-freshness-staleness-guards.md)

---

## 2026-08-10 — FIE 歷史資料回填策略

**Context**：`macro_history.db` 的歷史覆蓋很淺（macro_daily 僅 33 個交易日），需要回填以建立有意義的歷史視窗。

**Decision**：實作 `backfill` CLI 工具，支援 gap 偵測、dry-run 規劃、受控回填。回填永遠只寫入臨時 DB，拒絕寫入 `macro_history.db`。`stock_monthly` 因 yfinance API 限制標記為 BLOCKED，不造假資料。

**Consequences**：安全的回填路徑，不會意外損壞生產 DB。代價是 stock_monthly 的歷史回填無法自動化。

**Related**：`phase3/backfill.py`、[ADR-003](../adr/adr-003-historical-backfill-policy.md)

---

## 2026-08-18 — M8 R3 驗證語意修正

**Context**：M8 觀測的 R3 檢查（jobs.json 不變性）使用 whole-file SHA-256 比較，但 jobs.json 是共享排程器狀態檔，任何並發 cron 觸發都會改變 volatile 欄位，導致 false-positive。

**Decision**：改用 scoped signature 比較，排除 12 個 volatile 執行時欄位（`last_run_at`、`next_run_at`、`state`、`fire_claim` 等），僅比較語意/配置欄位。後續發現 `repeat.completed` 嵌套欄位也需排除。

**Consequences**：R3 能正確區分「cron 正常活動」與「M8 觀測導致的語意變更」。代價是簽名計算邏輯更複雜。

**Related**：`tests/phase3/test_m8_r3_scoped_validation.py`、[ADR-004](../adr/adr-004-m8-r3-validation-semantics.md)

---

## 2026-08-26 — 公開 Repository + 治理文件整理

**Context**：專案需要公開到 GitHub，但治理歷史散落在大量內部報告中，不適合直接複製。

**Decision**：建立 `docs/governance/`、`docs/architecture/`、`docs/adr/` 三層文件結構，從治理報告中萃取 durable 內容，以正常化、去敏感化的方式整理。原始內部報告保持不動。

**Consequences**：公開文件專注於「為什麼」和「如何演進」，排除 runtime 證據、敏感資訊、機器特定細節。代價是文件維護需要同步更新。
---

## 2026-10-05 — PostgreSQL 生產權威定型 + SQLite 降級為 rollback source

**Context**：2026-10-05 SQLite→PostgreSQL controlled production cutover 已依
cutover / final-acceptance work orders 執行並 accepted（wrapper PG env stanza
commit `35c0b3e`）。Repository 內多份歷史文件仍以 SQLite 時代語意描述持久層
（SQLite 預設、fallback），Phase 6.8A 已廢除 implicit defaults 但 repository
缺一份 current-state 生產架構敘事。

**Decision**：Production authoritative datastore 定型為 **PostgreSQL**；SQLite
正式角色固定為 **explicit rollback / recovery source only**（保留不動、操作需
explicit rollback contract、非 silent fallback、無 retirement）。新增 current-state
架構文件 `docs/architecture/postgresql-production-architecture.md`；在 6.7B
baseline 補 §18 amendment 標註 D3 supersession；README 增加 current-state
entrypoint 與針對性 normalization 註記（不做 global rewrite，歷史文件保留）。

**Consequences**：新讀者可由 README → 架構文件取得正確生產敘事；歷史記錄的
forensic 價值保留、以 amendment/banner 分級。代價是部分歷史文件仍含舊語意，
須靠索引註記導流。

---

## 2026-10-05 — Codex Cloud 整合：readiness 文件 only，未實作

**Context**：未來可能以 Codex Cloud 作為外部執行 provider（outbound execution；
與既有 Phase 6.5 inbound ChatGPT tool boundary 方向相反）。為避免「readiness
文件」被解讀為「已整合」，需要一個明確的準備狀態決策。

**Decision**：僅建立 readiness architecture 文件
`docs/architecture/codex-cloud-integration-readiness.md`（PROPOSED/FUTURE）：
logical boundary（submission/status/result/cancellation）、FIE-proposed 狀態
模型、idempotency（`client_request_id`）、security boundary（adapter 禁取
Production DB credentials、禁直寫 Production、禁繞過 guards、payload redaction）、
fail-safe 語意（`UNKNOWN != SUCCESS`；`TIMED_OUT != confirmed no side effect`）。
**NOT IMPLEMENTED / NOT ENABLED / NOT DEPLOYED**；任何實作需另案 work order
＋§ readiness checklist 覆核。

**Consequences**：未來實作有可審查的 boundary 與 gate；禁止的 topology
（external agent → unrestricted production）被事先記錄。代價是 contract
目前為提案狀態，official contract 出現時須重新對齊。

---

## 2026-10-05 — Repository execution baseline productionization（Phase 6.8C）

**Context**：6.8B publication 之後，repository 需成為可由全新 ephemeral
environment（未來 Codex Cloud；現在僅 readiness）安全 acquisition/bootstrap/
test/驗證的 self-contained baseline。Hidden-dependency audit（Task B）發現
4 個 blocking 類別全部位於 cron-wrapper / production-contract 鏈（operator
venv 預設、operator-owned wrapper env file、host-literal production fallback
store、operator 路徑 production contract 預設），全部 fail-closed 但 wrapper
鏈不可由 repo + non-secret config 滿足；另發現一個 fail-closed 缺口：
**DSN-free production contract file 讓 rehearsal guard 在無法證明非 Production
的情況下接受 target**（與 `load_production_identities` 自身 docstring「missing/
unreadable/DSN-free fail-closed」相矛盾）。

**Decision**：
1. Wrapper 鏈修復（保留 D5/§21 生產語意）：`PYTHON_BIN` 預設不變但 fail-fast
   （exit 78、可讀訊息）；wrapper env file 缺失時輸出 operator bootstrap 提示
   （exit 語意不變仍走 guard fail-closed）；D5 production fallback store 改由
   `PROJECT_ROOT` 推導（operator host 上等值，不再 host-literal）。
2. runtime_contract fail-closed 強化：readable-but-DSN-free contract set 一律
   `FAIL_CLOSED_PRODUCTION_IDENTITY_UNAVAILABLE`（unknown != safe 落實為程式碼）。
3. 建立 canonical 入口：`scripts/bootstrap.sh`（idempotent venv + pyproject 依
   賴 + 離線驗證）、`scripts/test-cloud.sh`（scrubbed env + ephemeral isolated PG
   + caller DSN preflight + 負向控制執行 + trap 清理）、
   `scripts/cloud_negative_controls.py`（8 負向控制 executable proof）。
4. 新增 operator env 範本 `examples/fie-wrapper.env.example`（全 synthetic
   placeholder；真實契約仍在 git 外 0600 檔案）。
5. 新增 provider-neutral
   `docs/architecture/cloud-execution-contract.md`（acquisition/bootstrap/
   test/cleanup/exit 契約；Codex Cloud 僅 future consumer example）。
6. `.gitignore` 收斂：`*.db-journal`、`.env` / `*.env`（含 `!*.env.example`
   回補）、`*.passphrase`、`*.secret`、`*.token`、`.ruff_cache/`、cloud-agent
   目錄；`tests/phase3/test_raw_layer_pg.py` 的 `pg_isready` 硬編碼路徑改由
   `pg_runtime_preflight._pg_bin` 探測 + `FIE_TEST_PG_DSN` 真正生效。

**Consequences**：fresh clone 以 repo + documented non-secret config 即可完成
bootstrap / isolated test / 負向控制；cron 生產鏈語意不變（同一 wrapper 在
operator host 上行為等效、僅錯誤訊息與 portable 路徑推導更清晰）。
`CODEX_CLOUD_INTEGRATION_IMPLEMENTED / RUNTIME_ENABLED / DEPLOYED` 維持 false；
任何 live Cloud 工作需 Owner 另行授權（Phase 6.9）。
