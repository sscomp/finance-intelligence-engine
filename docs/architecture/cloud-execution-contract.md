# Cloud Execution Contract (Phase 6.8C; updated 6.9A-R3, 6.9A-R4-R2-R1)

> **Status**: `REPOSITORY_EXECUTION_READY — NOT AN INTEGRATION`
> `CODEX_CLOUD_INTEGRATION_IMPLEMENTED=false` · `CODEX_CLOUD_RUNTIME_ENABLED=false` · `CODEX_CLOUD_DEPLOYED=false`
> **6.9A-R3 update**: isolated PostgreSQL provisioning is now repository-owned
> (portable acquisition path). Remediation is repository-ready / re-entry-ready,
> but Codex Cloud execution is **NOT VALIDATED** until a subsequent fresh cloud
> job independently passes. 本文件**不**使用「Codex Cloud supported」或等效詞彙。
>
> 本文件定義 **repository 本身的 acquisition / bootstrap / test / artifact 契約**，
> 使任何全新、無本機歷史狀態、無 Production credential 的 ephemeral execution
> environment（未來包括 Codex Cloud，此時僅為 **future consumer example**）可以
> isolated validation。Codex Cloud 的 API/adapter 行為均未經確認，相關段落標記
> `PROPOSED / TO_BE_VALIDATED_IN_PHASE_6_9 / NOT_IMPLEMENTED`。
> 架構層 readiness 提案見
> [codex-cloud-integration-readiness.md](codex-cloud-integration-readiness.md)（Phase 6.8B）。

---

## 1. Repository acquisition

| 項目 | 契約 |
|---|---|
| Source | `https://github.com/sscomp/finance-intelligence-engine.git` |
| Branch | `master`（唯一 supported ref；本文件對應 Phase 6.8C 發佈之後的 master HEAD） |
| Acquisition 方式 | `git clone`（zero-history fresh clone 即可成立；**不需要**任何本機殘留狀態、session memory、cron/wrapper 環境） |
| Submodules / LFS | 無 |
| 完整性 | clone 後 `git rev-parse HEAD` 必須等於本次 publication 記錄的 `remote_after_sha`（見 work order receipt） |

## 2. Bootstrap（canonical entrypoint）

**6.9A-R4-R2-R1 Python 解析契約（ONE contract）**：`scripts/fie_python_resolver.sh`
是 **唯一** 的 interpreter 解析實作（static contract test pin 全部 consumer）：

```text
1. FIE_PYTHON explicit override   — hard contract：設了但不可用
                                    （不存在/非執行檔/<3.11）→ exit 78
                                    "refusing to substitute"，永不靜默替換
2. $VIRTUAL_ENV/bin/python3
3. <repo>/.venv/bin/python3
4. PATH python3（需 ≥ 3.11，child 內 probe）
5. fail closed（exit 78 + 診斷）
```

wrappers（run.sh / run_weekly.sh / run_monthly.sh）一律經由此 contract 解析
interpreter — **不再**依賴任何 operator-host interpreter 路徑
（R4-R2 blocker #1 根因移除；operator 建議以 `FIE_PYTHON` pin 目標 interpreter）。

```bash
bash scripts/bootstrap.sh --all
```

- 建立 `<repo>/.venv`（idempotent）並安裝 **exactly** `pyproject.toml` 宣告項：
  runtime `PyYAML>=6,<8` + `yfinance>=0.2`；test extra `pytest>=8` + `pandas>=2,<4`；
  postgres extra `psycopg[binary]>=3.2`。
- requires-python **>= 3.11**。
- 結束時執行離線 import 驗證（`phase3.cli` import-safe，不需要網路）。
- **不需要**：operator home、shell alias、untracked `.env`、 Production credential。

## 3. Required / optional / forbidden environment

### Required（for test profile：由 `scripts/test-cloud.sh` 自動設定，caller 無須供應）

| 變數 | 值 | 說明 |
|---|---|---|
| `FIE_SERVICE_ENV` | `test` | rehearsal-class mode；Production-class target 一律拒絕 |
| `PYTHONPATH` | repository root | unittest discovery |
| `FIE_TEST_PG_DSN` | （可選） | 唯一 test PG 目標選擇；未提供 → entrypoint 自行 provision ephemeral cluster，或 PG legs 以明確原因 skip |

### Optional（portable defaults）

`FIE_HTTP_PORT`（entrypoint 預設 `18720` 僅為避免與服務並存主機上的 8787 衝突；transport
tests 本身 bind ephemeral 端口，非必要）、`FIE_VENV_DIR`、`FIE_PYTHON`。

### Forbidden（cloud validation 環境一律不得提供）

| 變數 / 來源 | 理由 |
|---|---|
| Production DSN / `PGPASSWORD` / production contract env files（0600 operator 資產） | Production credentials 在 cloud validation **forbidden**；誤注入會在 preflight 證明失敗 → FAIL CLOSED（exit 78），而非路由進 Production |
| `FIE_PRODUCTION_DB_CONTRACT` 指向真實 operator 檔案 | production identity 只允許 synthetic fixture；「DSN-free / 不可讀」契約集一律 fail-closed（6.8C 修復強制） |
| caller-provided 非 loopback / 非 `/tmp` socket 的 test DSN | target identity 無法證明非 Production → FAIL CLOSED |

## 4. Isolated PostgreSQL provisioning (6.9A-R3 — repository-owned)

- **Canonical boundary（ONE implementation）**：`scripts/provision-test-postgres.sh`
  （sourceable library + standalone shape；詳見
  [ephemeral-postgresql-provisioning.md](ephemeral-postgresql-provisioning.md)）。
  `scripts/test-cloud.sh` **source** 此實作 — 沒有第二個 provisioning 路徑。
- **預設（有 caller DSN）**：`FIE_TEST_PG_DSN` 僅接受 loopback TCP 或 `/tmp`
  socket 形式，且 preflight 以 production identity contract 證明非 Production
  （operator host 上契約檔存在 → identity 比對；DSN-free/missing →
  **FAIL CLOSED exit 78**）並驗證 `server_encoding == UTF8`（否則 exit 78）。
- **預設（無 caller DSN）**：entrypoint 呼叫 canonical provisioner 的
  `fie_test_pg_start`：
  1. discovery first（`FIE_TEST_PG_BIN` > `/usr/lib/postgresql/<N>/bin` > PATH `initdb`）；
  2. 無系統 tooling 時 → **portable distribution** 自 Maven Central，
     version pinned `18.4.0`（== Production major.minor）、published sha256
     sidecar 驗證、cache 經 **R4-R1 canonical writable-runtime resolver**
     （`FIE_TEST_PG_CACHE_DIR` explicit override（不可用 → fail-closed 97）
     > `XDG_CACHE_HOME` > `$HOME/.cache/fie/test-postgres`（probe-proven；
     `$HOME` 存在≠可寫 — R4 root cause）> job-local temp fallback
     （`${TMPDIR:-/tmp}` 下 per-invocation `mktemp -d` 唯一、marker-proven；
     `$HOME/.cache` 不可寫的 managed filesystem（如 Codex Cloud）不需要 owner
     端環境自訂即可取得）；每次 HIT 重新驗證 jar sha256；cold acquisition
     走 `.partial.*`/`.stage.*` → validate → atomic promotion；persistent
     cache 以 flock serialize）、no sudo、no interactive prompt、no global
     daemon、no system PG config mutation（**Codex Cloud 上沒有
     `initdb`/`pg_ctl` 的 R2 根因就此補齊；R4 的 cache-path blocker
     已於 R4-R1 修補**）；
  3. platform 支援面：Linux x86_64/aarch64；其他 →
     `POSTGRESQL_PLATFORM_UNSUPPORTED`（exit 90），**fail-closed，無 fallback，
     永不降級到 Production**；
  4. job-owned identity：per-run TMPDIR、隨機 synthetic user/db/password
     （0600 job TMPDIR 內）、loopback-only bind（有 per-line proof）、UTF-8
     pinned、ownership marker、per-run random port。**任何 caller PG DSN
     candidate → exit 78（值永不列入）**；
  5. readiness（process + TCP scram auth + DB identity + UTF-8 + guard）
     全部通過才 export passwordless socket DSN；teardown 是 ownership-proven、
     idempotent，**只**移除本 job 的 cluster/dir（雙 cluster NC 證明）。
- **Provisioning 失敗的下游語意**：PG legs skip-with-reason（classification
  明示）。**永不**靜默 SQLite fallback（SQLite 是 rollback/recovery only）、
  **永不** Production fallback。
- Production DSN ≠ test DSN、Production hostname/data/credential 均不需要。

## 5. Test commands（canonical entrypoints）

```bash
bash scripts/test-cloud.sh          # focused cloud-readiness set + 負向控制
bash scripts/test-cloud.sh --full   # full hermetic regression + 負向控制
```

- 等效 raw 形式（與既有文件一致）：`PYTHONPATH=. FIE_SERVICE_ENV=test python3 -m unittest discover -s tests --top-level-dir=.`
- `scripts/cloud_negative_controls.py` 在 entrypoint 內**實際執行**（8 個負向控制：
  missing target / production DSN 注入 / alias 等價 / 矛盾 target / SQLite fallback
  拒絕 / DSN-free contract / unknown service env / shell guard exit 78）。
- **6.9A-R4-R2-R1 scrubbed-child 網路上下文契約**：測試 child 一律以
  **explicit allowlist** 受網路上下文（`HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY`
  大小寫共 8 名 + `SSL_CERT_FILE`/`SSL_CERT_DIR`/`REQUESTS_CA_BUNDLE`/
  `CURL_CA_BUNDLE`），**沒有 blanket env 繼承**；secret boundary denylist
  （PGPASSWORD/POSTGRES_PASSWORD/DATABASE_URL/FIE_INTELLIGENCE_DB/FIE_DB_PATH/
  FIE_DATABASE_URL/FIE_DB_TARGET_*/FIE_TEST_PG_DSN/token/雲端憑證類）永不
  進入 child；值一律 opaque，redaction helper 永不攜帶任何值。實作與測試共用
  ONE helper：`tests/cloud_child_env.py`（N1–N8 執行證明：scrubbed child +
  approved context ⇒ sidecar 取得成功；缺 CA ⇒ 91；缺 no_proxy（blackhole
  proxy）⇒ 91）。
- **6.9A-R4-R2-R1 cache visibility**：測試側**唯一** cache 解析途徑是
  `tests/cloud_child_env.py::canonical_cache_state()`（執行 provisioner 的
  `--cache-state`），與 runtime 同源 — 持久 cache 位元組級一致（NC16）。
- focused 清單含 `tests.test_bootstrap_cli_contract`（NC03 zero-arg /
  unknown-option fail-closed）。
- **6.9A-R4-R4-R1 canonical SQL client + fixture lifecycle**：repository
  validation 的 SQL 執行一律走 `scripts/sql_exec.py`（psycopg）— **hermetic
  validation 永不執行 host `psql`**（fresh Cloud 無 psql;portable artifact
  僅 server binaries;詳見 ephemeral-postgresql-provisioning.md §12）。
  zero-write fixture lifecycle 為 exception-safe:teardown 責任在任何 fallible
  setup 步驟前註冊、任一失敗點執行 ownership-aware 還原並 re-raise 原始例外、
  teardown idempotent、provision subshell 環境 scrub 掉 per-job globals。
  FI-01..FI-10 失敗注入矩陣 + 順序獨立性見
  `tests/test_fixture_lifecycle_exception_safety.py`（focused/full 皆執行）。
- 進入點保證：exit code 忠實反映 unittest 結果、failure 可見、無 mandatory test
  隱藏 skip、無 Production mutation、ephemeral resources 由 trap 清理。

## 5A. Acceptance ordering contract（6.9A-R4-R6-R1）

**Root cause being remediated（R4-R6 FAIL_NEEDS_REMEDIATION）**：predecessor WO
要求在 **bootstrap 之前** 取得 canonical PRE Production fingerprint，但 canonical
fingerprint 路徑經 `scripts/sql_exec.py`（psycopg），而 psycopg 只能由 canonical
bootstrap（`--all`）provision——依賴循環使 fresh Cloud run 停擺。修正**不是**
削弱 fingerprint、不是 host psql 替代、不是 ad-hoc pip install，而是**修正順序**：

```text
A. git/environment baseline
B. PRE-BOOTSTRAP PRODUCTION ACCESS SAFETY PREFLIGHT   （stdlib-only，無 psycopg/PG/app deps）
C. canonical zero-arg bootstrap（--all）
D. bootstrap postconditions（RUNTIME_PYTHON_PORTABLE / POSTGRESQL_PORTABLE_PROVISIONING / psycopg）
E. PRE Production fingerprint                          （canonical 路徑，經 scripts/sql_exec.py）
F. focused → G. FI → H. full regression
I. POST fingerprint → J. PRE==POST → K. cleanup/secret-scan/seal
```

- **PRE fingerprint 語意（corrected contract）**：captured **after isolated
  runtime bootstrap** and **before any acceptance/test workload** — NOT
  "before dependency/runtime bootstrap"。
- **ONE implementation**：preflight/ordering/provenance contract =
  `scripts/acceptance_contract.py`（stdlib-only；importing it 永不需要
  psycopg）。非法 transition / 前置不滿足一律 **exit 78**，refusal message 標明
  `ORDERING_PREREQUISITE`，**永不**將 ordering 拒絕誤報為 Production contact。
- **§12 ordering state machine（單一合法鏈）**：`INIT → BASELINE_CAPTURED →
  PRODUCTION_ACCESS_PREFLIGHT_PASSED → BOOTSTRAP_PASSED →
  PRE_FINGERPRINT_CAPTURED → FOCUSED_PASSED → FAILURE_INJECTION_PASSED →
  FULL_REGRESSION_PASSED → POST_FINGERPRINT_CAPTURED → FINGERPRINT_MATCHED →
  CLEANUP_PASSED → EVIDENCE_SEALED`（job-local JSON state file；atomic save）。
- **Acceptance context declaration**：caller 以 `FIE_ACCEPTANCE_STATE_FILE`
  宣告 acceptance-job context；`scripts/test-cloud.sh` 在該 context 內先跑
  preflight，再驗證 ordering prerequisite（focused 需 ≥ `PRE_FINGERPRINT_CAPTURED`、
  `--full` 需 ≥ `FAILURE_INJECTION_PASSED`），PASS 後記錄
  `FOCUSED_PASSED` / `FULL_REGRESSION_PASSED`。未宣告 state file → 行為與以往
  byte-identical（developer run 非 acceptance job）。
- **PRE/POST fingerprint**：同一 canonical zero-write hermetic
  Production-shaped fixture（`tests.test_rehearsal_guard_zero_write_invariant`）
  經同一 canonical SQL client；exact key-space 比較，**無 masking/normalization**；
  target 是 hermetic fixture，**不是**真實 Production、無 zero-write invariant
  削弱。

### Acceptance ordering contract — fresh-job provenance（6.9A-R4-R6-R1）

**fresh_codex_cloud_job_provenance** 的兩部分契約：

| 欄位 | 語意 |
|---|---|
| `owner_fresh_job_assertion` | **外部/操作者斷言**（env `FIE_OWNER_FRESH_JOB_ASSERTION`：`true`/`false`/**unset→null**）；程式碼**永不**合成此值（O10） |
| `runtime_freshness_consistency_verified` | job 內可機器驗證的一致性：expected published HEAD、無 predecessor remediation artifacts、無 inherited FIE job-owned PG processes/temp dirs（XDG persistent download cache 明示允許）。PG-process 殘留掃描以 **ownership-time** 歸因錨定：state file 於 job 開始記錄 `job_start_utc`；早於 job start 的 marker-carrying PG-family process = inherited residue（矛盾），job 期間產生者為本 job 自身 workload（observation）。未宣告 job start 錨點時掃描結果記為 inconclusive observation（比照 platform provenance 缺席規則） |
| `platform_fresh_job_provenance_available = false` | 不假設 platform `/new` provenance API；**其缺席本身不是 failure**（O8）；矛盾 runtime evidence **是** failure（O9） |

Worktree cleanliness 語意：tracked worktree 的 dirty 狀態**僅在**明示宣告的
fresh-job check（`--require-clean-worktree`）下是 contradiction（真實 fresh Cloud
acceptance run 一律帶此 flag）；remediation workspace（合法攜帶未提交工作）預設
只記為 observation、不阻擋。值從不落入任一分類的 `PASS` 形式：NOT_RUN 欄位保持
null（O12）。

### Acceptance ordering contract — regression coverage

`tests/test_acceptance_ordering_contract.py`（focused 清單成員）執行 §13
O1–O12：O1 舊順序拒絕（REJECTED_WITH_ORDERING_PREREQUISITE，非 Production contact
誤報）、O2 preflight psycopg-free、O3 preflight→bootstrap→PRE 順序、O4 acceptance
workload 不得先於 PRE fingerprint、O5 PRE/POST 同一 canonical 路徑、O6/O6b
Production-shaped DSN/credential carrier 注入 bootstrap context 失敗封閉（synthetic
only）、O7 bootstrap child env scrub（`tests/cloud_child_env.py` 唯一 scrub 實作）、
O8 platform provenance 缺席非 failure、O9 矛盾 freshness evidence 阻擋、O10 owner
assertion 永不合成、O11 mandatory failure 阻止全部 workload、O12 receipt null
語意保留。

## 6. Artifact outputs / exit status

| 項目 | 契約 |
|---|---|
| 測試 artifact | 測試不寫入 repository 外持久 artifact；DB 目標為 `:memory:`/temp/ephemeral PG；unittest stdout 即結果 |
| Exit status | `0` = PASS；unittest exit code 原樣傳播；`78` = fail-closed 拒絕（DB target / preflight）；`2/3/5` = bootstrap / 依賴 / 環境錯誤；failures 一律在 stderr 明示 |
| Cleanup | 自我 provision 的 cluster 與 `$TMPDIR` 目錄由 `EXIT` trap 停止並刪除；caller-provided DSN 的清理責任屬 provisioner（本契約不承擔） |

## 7. Security boundary（cloud validation 適用的不變量）

```text
PRODUCTION_DB_MUTATION (via test profile)   = forbidden（identity preflight + guard 強制）
PRODUCTION_CREDENTIAL_IN_CLOUD_PROFILE      = forbidden（不需要；preflight 驗證不接觸真實值）
SILENT SQLITE FALLBACK                      = forbidden（SQLite = ROLLBACK/RECOVERY ONLY，
                                              缺 target 一律 refuse，永不 fallback）
FORCE PUSH / HISTORY REWRITE                = forbidden（repository 層，另見 work order）
NETWORK ACCESS（測試）                        = 不需要（suite deterministic, offline-safe）；
                                              portable acquisition（首次無系統 tooling 時）
                                              = 需要 Maven Central 一次取得（cached 之後離線）
```

## 8. Failure semantics

任何 gate failure（bootstrap、preflight、negative controls、tests）→
exit code 非零、stderr 明示原因；execution environment 不得「人工補齊」後宣稱
PASS（G7 紀律）。缺陷回報 owner，不得 push / deploy / 改寫歷史。

## 9. Network assumptions

測試套件 deterministic 且 offline-safe（adapters 以 fixture payload 執行；
transport tests bind loopback ephemeral ports）。runtime fetch 腳本
（`macro_daily.py` 等 cron 路徑）需要 yfinance/TWSE/RSS 網路，但 **不屬於
cloud test profile**。`pip install` 需要套件索引網路（bootstrap 一次性）。

## 10. Codex Cloud status（boundary restatement）

```text
Codex Cloud live integration: NOT_IMPLEMENTED         （本階段不實作）
Codex Cloud runtime/adapter behavior: PROPOSED, TO_BE_VALIDATED_IN_PHASE_6_9
Codex Cloud credential provisioning: FORBIDDEN（本階段不進行）
Repository execution readiness: READY（本契約 §1–§9 已由 fresh-clone 驗證）
Isolated PG provisioning (6.9A-R3): REPOSITORY-READY / RE-ENTRY-READY
  （portable acquisition + canonical provisioner；fresh-clone 於 operator
   host 以冷 cache 實證取得、provision、validate、teardown）
Codex Cloud execution validation: NOT VALIDATED
  （直到一個後續的 fresh cloud job 獨立 rerun 全套 validation；本文件
   不標稱 "Codex Cloud supported" — Remediation 仍為 repository 遺產）
Next gate (owner-initiated): 6.9A-R4 fresh cloud re-entry —
  acquisition → bootstrap → provision → tests → teardown, fully isolated
```

下一階段（Phase 6.9A，需 Owner 另行授權）第一個 Cloud gate 只驗證
acquisition、bootstrap、build/test 與 artifact contract；仍不得授予 Production
credential 或 Production mutation capability。

---

*Change record: Phase 6.8C（2026-10-05）— repository productionization hygiene;
authoritative DB architecture 與 readiness 提案仍以 6.7B/6.8A/6.8B 已接受文件為準。
Phase 6.9A-R3（2026-10-05）— §4 rewritten: canonical repository-owned
ephemeral PostgreSQL provisioning（portable acquisition path）replaces the
inline best-effort provisioning; §7 network note and §10 status updated.
Codex Cloud execution remains NOT VALIDATED until fresh re-entry.
Phase 6.9A-R4-R5-R1（2026-10-06）— fresh-clone class cleanup / cache lock
ownership / PG zombie attribution（§4 resolver 與 §12 註記保持不變）.
Phase 6.9A-R4-R6-R1（2026-10-06）— §5A added: corrected acceptance ordering
（PRE fingerprint AFTER bootstrap, before acceptance workloads）+ ordering
state machine + acceptance-context gate in scripts/test-cloud.sh +
fresh-job provenance contract（owner assertion nullable、never synthesized;
platform provenance assumed absent）. Root cause：R4-R6 的 pre-bootstrap
fingerprint 要求與 canonical SQL client 之間的依賴循環。*