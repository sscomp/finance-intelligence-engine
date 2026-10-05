# Cloud Execution Contract (Phase 6.8C)

> **Status**: `REPOSITORY_EXECUTION_READY — NOT AN INTEGRATION`
> `CODEX_CLOUD_INTEGRATION_IMPLEMENTED=false` · `CODEX_CLOUD_RUNTIME_ENABLED=false` · `CODEX_CLOUD_DEPLOYED=false`
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

## 4. Isolated PostgreSQL provisioning assumption

- **預設（cloud）**：`scripts/test-cloud.sh` 在沒有 `FIE_TEST_PG_DSN` 與 PG tooling 時
  以 SQLite-only 跑（PG legs skip-with-reason，不會 fail）；有 PG tooling（`initdb`/
  `pg_ctl` on PATH 或 `/usr/lib/postgresql/*/bin`）時自动 provision：使用者級
  trust-auth cluster、`$TMPDIR` socket、per-run port（預設 54331）、run 結束即销毀。
- **Caller-provided**：`FIE_TEST_PG_DSN` 僅接受 `/tmp` socket 或 loopback 形式，
  且 preflight 以 production identity contract 證明非 Production（operator host 上
  契約檔存在 → identity 比對；DSN-free/missing → FAIL CLOSED）。
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
- 進入點保證：exit code 忠實反映 unittest 結果、failure 可見、無 mandatory test
  隱藏 skip、無 Production mutation、ephemeral resources 由 trap 清理。

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
NETWORK ACCESS（測試）                        = 不需要（suite deterministic, offline-safe）
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
```

下一階段（Phase 6.9A，需 Owner 另行授權）第一個 Cloud gate 只驗證
acquisition、bootstrap、build/test 與 artifact contract；仍不得授予 Production
credential 或 Production mutation capability。

---

*Change record: Phase 6.8C（2026-10-05）— repository productionization hygiene;
authoritative DB architecture 與 readiness 提案仍以 6.7B/6.8A/6.8B 已接受文件為準。*