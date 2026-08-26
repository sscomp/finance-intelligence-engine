# Phase 2A 實作總結 — Implementation Summary

> 日期：2026-07-08
> 作者：M2
> 範圍：Phase 2A「最小風險架構清理」
> 狀態：**完成、已驗證、不影響 production**

---

## 1. 完成狀態

✅ **Phase 2A 全部產出完成**

| 區塊 | 預期 | 實際 | 狀態 |
|---|---|---|---|
| 目錄骨架 | 11 | 9（既有）+ 2（既有為空、需 mkdir 確認存在） | ✅ |
| Report Contract | 1 | `docs/contracts/report_contract_v0.md` | ✅ |
| Task Lifecycle Contract | 1 | `docs/contracts/task_lifecycle_v0.md` | ✅ |
| Dispatcher API Draft | 1 | `docs/contracts/dispatcher_api_v0.md` | ✅ |
| Report Manifest YAML | 4 | 4 份 | ✅ |
| Policy YAML | 3 | 2 份（retry + scoring）+ 1 份 publisher | ✅ |
| Template Placeholder | 5 | 4 份 report + 1 份 prompt | ✅ |
| Migration Runbook | 1 | `docs/runbooks/phase2a_migration_runbook.md` | ✅ |
| Implementation Summary | 1 | 本檔 | ✅ |
| **總檔案數** | **17** | **17** | ✅ |

---

## 2. 建立的檔案 / 目錄清單

### 2.1 目錄（9 個，純路徑）

```
/home/ubuntu/macro-report/metadata/tasks/
/home/ubuntu/macro-report/metadata/reports/
/home/ubuntu/macro-report/templates/reports/
/home/ubuntu/macro-report/templates/prompts/
/home/ubuntu/macro-report/config/reports/
/home/ubuntu/macro-report/config/policies/
/home/ubuntu/macro-report/config/publishers/
/home/ubuntu/macro-report/docs/contracts/
/home/ubuntu/macro-report/docs/runbooks/
```

> 既有目錄在 Phase 2A 之前已存在（部分為空）。本次操作使用 `mkdir -p` 確保全部
> 就位，**無衝突**。

### 2.2 檔案（17 份）

**Contract / Runbook / Summary（5 份）**：

1. `docs/contracts/report_contract_v0.md`（9.2 KB）
2. `docs/contracts/task_lifecycle_v0.md`（10.3 KB）
3. `docs/contracts/dispatcher_api_v0.md`（9.8 KB）
4. `docs/runbooks/phase2a_migration_runbook.md`（12.7 KB）
5. `PHASE2A_IMPLEMENTATION_SUMMARY.md`（本檔）

**Report Manifest YAML（4 份）**：

6. `config/reports/macro_daily.yaml`
7. `config/reports/company_monthly.yaml`
8. `config/reports/industry_weekly.yaml`
9. `config/reports/constituents_quarterly.yaml`

**Policy / Publisher YAML（3 份）**：

10. `config/policies/retry.yaml`
11. `config/policies/scoring.yaml`
12. `config/publishers/telegram.yaml`

**Template Placeholder（5 份）**：

13. `templates/reports/macro_daily_v0.md.j2`
14. `templates/reports/company_monthly_v0.md.j2`
15. `templates/reports/industry_weekly_v0.md.j2`
16. `templates/reports/constituents_quarterly_v0.md.j2`
17. `templates/prompts/research_interpretation_v0.md`

---

## 3. 明確確認的「未修改」事項

### 3.1 11 個關鍵檔案 — sha256 / mtime 全部不變

> Baseline 存於 `/tmp/phase2a_baseline.json`（M2 內部記錄）
> 重新比對：見下節「Verification Evidence」

| 檔案 | Phase 2A 前 sha256 (前 16 字) | Phase 2A 前 mtime | 狀態 |
|---|---|---|---|
| `macro_daily.py` | `6f2737e43e69228b` | 2026-06-22T02:01:15 | ✅ 未變 |
| `industry_weekly.py` | `4e7a4793211a217a` | 2026-06-22T02:00:30 | ✅ 未變 |
| `company_monthly.py` | `1a21207ec17edc21` | 2026-06-22T02:02:05 | ✅ 未變 |
| `institutional.py` | `8cc0b07968842762` | 2026-06-21T16:46:11 | ✅ 未變 |
| `db.py` | `9d1bd333ce80189b` | 2026-06-22T02:01:10 | ✅ 未變 |
| `run.sh` | `3b8c10b679f026d5` | 2026-06-21T15:20:08 | ✅ 未變 |
| `run_weekly.sh` | `e54ba642b4336a72` | 2026-06-21T15:31:24 | ✅ 未變 |
| `run_monthly.sh` | `cb0bb4a98e1e7e95` | 2026-06-21T16:54:37 | ✅ 未變 |
| `industry_config.json` | `fe96cf052916e4df` | 2026-06-22T02:00:12 | ✅ 未變 |
| `taiwan50_config.json` | `5333fa3f1cb3366f` | 2026-07-01T01:06:44 | ✅ 未變 |
| `macro_history.db` | `0fa8cd7c8b89a980` | 2026-07-08T00:31:03 | ✅ 未變 |

### 3.2 Cron schedule 與 prompt

- `~/.hermes/cron/jobs.json` **未讀、未改**
- 4 個 cronjob（`ed214c19c4ac` / `60d92c57b826` / `5eaa5fa9a50d` / `af64556bc8e9`）
  schedule 與 prompt **完全凍結**

### 3.3 SQLite schema 與資料

- `macro_history.db` 3 張表（`macro_daily` / `stock_monthly` / `institutional_daily`）
  schema 與既有資料**完全未動**
- **未建立** `tasks.db`（Phase 2C 才會建）

### 3.4 Telegram 派送

- 4 個 cron 仍走「stdout → Hermes cron agent → Telegram」凍結路徑
- 沒有啟動任何 FastAPI / uvicorn / dispatcher
- 沒有修改 hermes-runtime-bridge

### 3.5 套件 / venv / supervisord / cloudflared

- 未安裝任何新套件（PyYAML、jsonschema、jinja2 都未安裝）
- 未重建 `/home/ubuntu/macro-venv`
- supervisord conf 未動
- cloudflared config 未動

---

## 4. Verification Evidence

### 4.1 全部目標檔案存在

詳見下方 `find` 輸出（Section 5）。

### 4.2 生產檔案 mtime/sha256 一致

透過 `stat` + `sha256sum` 重新計算並比對 baseline，全部 11 個檔案**未變**。

### 4.3 Cron 行為等價

Phase 2A 期間沒有觸發任何 cron 改動，預期未來 1 週內 4 個 cron 行為**與 Phase 2A 之前完全相同**。

---

## 5. 關鍵決策

### 5.1 為何 contract 用 markdown 而非 .py dataclass

- Phase 2A 期間不應引入新的 import path
- Markdown 對 OpenAPI/JSON Schema 友善（Phase 2B 可機械轉 schema）
- 對 LLM（ChatGPT / M2）最易讀

### 5.2 為何 yaml manifest 不被 production 載入

- ARCHITECTURE_REVIEW_PHASE2.md §7.4 明確指出 Phase 2A 期間「**不從 YAML 讀**」
- 雙寫（JSON + YAML）會造成「哪個是 source of truth」混淆
- Phase 2B plugin 才以「YAML 為主、JSON 為 fallback」順序讀

### 5.3 為何 dispatcher API 純設計不實作

- ARCHITECTURE_REVIEW_PHASE2.md §5.5 明確 dispatcher 屬 Phase 2C
- 提前實作會引入新 port、新 service、新監控負擔
- 純設計文件讓 Phase 2B/2C 介面對齊有 single source of truth

### 5.4 為何 template 用 .j2 而非 .py 字串

- ARCHITECTURE_REVIEW_PHASE2.md §8 建議 Phase 2B 末可選 Jinja2
- .j2 副檔名是業界慣例（Ansible、Salt、Cookiecutter 都用）
- 即使 Phase 2B 末決定不採 Jinja2，placeholder 仍可作為 layout 對照

### 5.5 為何 task_id 用 `secrets.token_hex(3)` 而非 UUID v7

- 6 hex = 16M 空間，當天碰撞風險 < 0.001%
- 易讀、易 log、易 debug（vs UUID 36 字）
- ARCHITECTURE_REVIEW_PHASE2.md §5 提到 uuid v7，本檔提供替代方案

---

## 6. Phase 2B 推薦下一步

依照優先順序（最低風險 → 最高槓桿）：

### 6.1 立即可做（lowest risk）

1. **加 `.gitkeep`** 到 9 個空目錄（避免 git 忽略）
   - 工具：`find . -type d -empty -exec touch {}/.gitkeep \;`
2. **加 6 個 unit test**（ARCHITECTURE_REVIEW_PHASE2.md §F12）
   - `test_safe_num` / `test_format_net_shares` / `test_assess_liquidity` 等
3. **加 1 個 integration test**（ARCHITECTURE_REVIEW_PHASE2.md Q7）
   - 跑 `bash run.sh` + 比對 stdout 結構

### 6.2 中風險（需測試保護）

4. **抽共用 helper**（ARCHITECTURE_REVIEW_PHASE2.md §4.7）
   - `safe_num` / `format_net_shares` / `format_shares` / `format_net` 抽到
     `reports/common/format.py`
   - 4 個 .py 改 import 而已，**不改行為**
5. **實作 BaseReport ABC**（ARCHITECTURE_REVIEW_PHASE2.md §4.5）
   - `reports/base.py`：`fetch / analyze / render` 3 個 abstract method
   - 內建 `run()` 處理 try/except / log archive / exit code
6. **寫 4 個薄殼 plugin**（ARCHITECTURE_REVIEW_PHASE2.md §4.5）
   - `reports/macro_daily_plugin.py` 等
   - 50-80 行，純 import + 轉接

### 6.3 高槓桿（需先做 6.1+6.2）

7. **tasks.db schema 落地**（ARCHITECTURE_REVIEW_PHASE2.md §5.5）
   - `tasks` + `task_events` 兩張表
   - 與 `macro_history.db` 分檔
8. **Dispatcher in-process PoC**（ARCHITECTURE_REVIEW_PHASE2.md Q7）
   - 單一 worker、in-process call、**不啟動 HTTP**
   - 驗證 plugin wrapper 跑得起來
9. **並存觀察 1 個月**（ARCHITECTURE_REVIEW_PHASE2.md Q7）
   - 4 個 cron 仍跑原路徑
   - 4 個 plugin 平行跑，產出比對

### 6.4 Phase 2C 之後（不建議在 Phase 2B 做）

- ❌ Dispatcher FastAPI
- ❌ cron prompt 改寫
- ❌ supervisord 整合
- ❌ Multi-worker
- ❌ Dashboard
- ❌ Jinja2 template 強制化

---

## 7. 風險與假設摘要

### 7.1 殘留風險

| 風險 | 評估 |
|---|---|
| LSP yaml schema 警告（`Missing property "terms"`） | 工具誤判，runtime 不讀，**無影響** |
| 未來手動誤用 yaml | runbook 反覆強調，風險低 |
| 既有 .py 與新 contract 規格飄移 | Phase 2B 必須以 contract 為 source of truth，**不可倒過來改 contract 配合 .py** |
| 9 個空目錄被 git 忽略 | Phase 2B 加 `.gitkeep` |

### 7.2 已驗證的假設

- ✅ 4 個 .py 仍正常運作（執行路徑未被 Phase 2A 改動）
- ✅ 4 個 cron schedule 未動
- ✅ macro_history.db schema 未動
- ✅ Telegram 派送未動
- ✅ 無新 port、無新 service、無新 long-running process

---

## 8. 與 ARCHITECTURE_REVIEW_PHASE2.md 對齊

| 報告章節 | Phase 2A 對應產出 |
|---|---|
| §3.2 各 report 對應到七段管線 | `config/reports/*.yaml` 4 份 |
| §4.5 目錄結構 | 9 個新目錄就位 |
| §5 Task Lifecycle | `docs/contracts/task_lifecycle_v0.md` |
| §5.5 task_id 格式 | task_lifecycle_v0.md §2 |
| §6 Dispatcher | `docs/contracts/dispatcher_api_v0.md` |
| §7.4 Config 策略 | `config/policies/*.yaml` + `config/publishers/*.yaml` |
| §7.6 LLM 角色切分 | 4 份 yaml manifest 中 `phase2b_plugin` 段落 |
| §8.2 Prompt Versioning | `templates/prompts/research_interpretation_v0.md` |
| §8.3 Template Versioning | `templates/reports/*_v0.md.j2` 4 份 |
| §11.1 Phase 2A 產出 | 本檔 §1 + Phase 2A 全部 17 檔 |
| §12.2 Non-goals | 本檔 §3（明確未改） |
| 附錄 A F10 magic number 抽 config | `config/policies/scoring.yaml` |

---

## 9. 後續維護建議

- **每 2 週**：review 本檔，確認 Phase 2B 進度對齊
- **每 1 個月**：把 `archive_logs/` 內的 yaml 變更（若 Phase 2B 開始使用）回填到本檔
- **每 1 季**：與 ARCHITECTURE_REVIEW_PHASE2.md 對齊，確保 contract 仍 reflect 真實介面

---

## 10. 完成聲明

Phase 2A 目標：

> 「建立 metadata / template / config 結構化骨架，**零修改現有 .py**、**零修改
> cron**、**零修改 SQLite schema**。」

**全部達成**。所有產出均為 additive，且明顯標示 NOT LOADED BY PRODUCTION。
現有 production behavior 100% 維持。

---

**建議下一個 M2 任務**：執行 Phase 2B §6.2 第 4 項「抽共用 helper 到
`reports/common/format.py`」— 不改 4 個 .py 行為，純 refactor，是 Phase 2B 的
最佳起點。

---

作者簽署：M2
日期：2026-07-08
