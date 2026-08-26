# Phase 2A 遷移 Runbook

> 範圍：Phase 2A「最小風險架構清理」— 純增量、零修改現有 .py / cron / SQLite schema
> 日期：2026-07-08
> 作者：M2

---

## 0. 為什麼寫這份 runbook

Phase 2A 的承諾是「**不破壞現有排程**」。為了讓這份承諾**可被驗證**、讓未來
Phase 2B 的人（可能是 M2 也可能是別人）有 SOP 可循，本 runbook 列：

- 改了什麼（additive）
- **沒改**什麼（frozen）
- 怎麼驗證「真的沒改」
- 怎麼完整 rollback
- Phase 2B 怎麼用這些 artifact

---

## 1. 改變清單（What Changed）

### 1.1 新增的目錄（9 個，全部已存在或新建空目錄）

```
macro-report/
├── metadata/
│   ├── tasks/           (空，Phase 2C 才放 tasks.db)
│   └── reports/         (空，Phase 2B 才放報告 archive)
├── templates/
│   ├── reports/         (5 份 .md.j2 placeholder)
│   └── prompts/         (1 份 research_interpretation prompt)
├── config/
│   ├── reports/         (4 份 yaml manifest draft)
│   ├── policies/        (2 份 retry + scoring draft)
│   └── publishers/      (1 份 telegram draft)
└── docs/
    ├── contracts/       (3 份 contract v0)
    └── runbooks/        (本檔 + Phase 2A summary)
```

> 注意：上述目錄中，部分（`config/`、`docs/`、`metadata/`、`templates/`
> 及其子目錄）在 Phase 2A 之前已存在但為空。Phase 2A 把它們「結構化」並填入
> draft 內容，但**沒有任何 production runtime 讀這些目錄**。

### 1.2 新增的檔案（17 份）

| 路徑 | 用途 | 是否被 production 讀 |
|---|---|---|
| `docs/contracts/report_contract_v0.md` | 報告識別 / 輸入 / 輸出 / 派送合約 | ❌ |
| `docs/contracts/task_lifecycle_v0.md` | task_id 格式 / 狀態機 / retry / query | ❌ |
| `docs/contracts/dispatcher_api_v0.md` | dispatcher HTTP/CLI API 設計 | ❌ |
| `config/reports/macro_daily.yaml` | macro_daily manifest draft | ❌ |
| `config/reports/company_monthly.yaml` | company_monthly manifest draft | ❌ |
| `config/reports/industry_weekly.yaml` | industry_weekly manifest draft | ❌ |
| `config/reports/constituents_quarterly.yaml` | constituents_quarterly manifest draft | ❌ |
| `config/policies/retry.yaml` | retry policy draft | ❌ |
| `config/policies/scoring.yaml` | scoring policy draft | ❌ |
| `config/publishers/telegram.yaml` | telegram publisher draft | ❌ |
| `templates/reports/macro_daily_v0.md.j2` | template placeholder | ❌ |
| `templates/reports/company_monthly_v0.md.j2` | template placeholder | ❌ |
| `templates/reports/industry_weekly_v0.md.j2` | template placeholder | ❌ |
| `templates/reports/constituents_quarterly_v0.md.j2` | template placeholder | ❌ |
| `templates/prompts/research_interpretation_v0.md` | research prompt placeholder | ❌ |
| `docs/runbooks/phase2a_migration_runbook.md` | 本檔 | ❌ |
| `PHASE2A_IMPLEMENTATION_SUMMARY.md` | 實作總結 | ❌ |

---

## 2. 沒改的東西（What Did NOT Change）

這是 Phase 2A 最重要的承諾。**以下檔案 / 設定 / 資料庫結構在 Phase 2A 期間未
被讀、未被寫、未被修改**：

### 2.1 Python 源碼（凍結）

```
macro-report/
├── macro_daily.py        (FROZEN)
├── industry_weekly.py    (FROZEN)
├── company_monthly.py    (FROZEN)
├── institutional.py      (FROZEN)
└── db.py                 (FROZEN)
```

### 2.2 入口 shell scripts（凍結）

```
macro-report/
├── run.sh        (FROZEN)
├── run_weekly.sh (FROZEN)
└── run_monthly.sh (FROZEN)
```

### 2.3 既有 config（凍結）

```
macro-report/
├── industry_config.json  (FROZEN)
└── taiwan50_config.json  (FROZEN)
```

### 2.4 既有 SQLite 資料庫（schema + 內容均凍結）

```
macro-report/
└── macro_history.db      (FROZEN — 3 張表 schema + 既有資料)
```

### 2.5 Cron schedule 與 prompt（凍結）

```
~/.hermes/cron/jobs.json
├── ed214c19c4ac 總體經濟晨報                 (FROZEN)
├── 60d92c57b826 產業趨勢週報                 (FROZEN)
├── 5eaa5fa9a50d 公司研究月報                 (FROZEN)
└── af64556bc8e9 季度成分股更新提醒           (FROZEN)
```

> Phase 2A 期間**完全未讀** `jobs.json`，**未觸碰**任何 cronjob 設定。

### 2.6 Telegram 派送邏輯（凍結）

- 4 個 cron 仍走「`print(stdout)` → Hermes cron agent → Telegram」路徑
- Phase 2A 沒有啟動任何 FastAPI 服務、沒有修改 hermes-runtime-bridge

### 2.7 venv / 套件（凍結）

- 不安裝新套件
- 不重建 `/home/ubuntu/macro-venv`
- 不引入 PyYAML、jsonschema、jinja2 等依賴

### 2.8 supervisord（凍結）

- 不新增 program
- 不修改 `/etc/supervisor/conf.d/openclaw.conf`

### 2.9 cloudflared（凍結）

- 不加新 tunnel
- 不改現有 config

---

## 3. 驗證步驟（Validation）

### 3.1 既保護檔案未變

對下列 11 個檔案，比對 sha256 + mtime，確認 Phase 2A 期間未變：

```bash
cd /home/ubuntu/macro-report

# 11 個關鍵檔案
for f in macro_daily.py industry_weekly.py company_monthly.py \
         institutional.py db.py \
         run.sh run_weekly.sh run_monthly.sh \
         industry_config.json taiwan50_config.json \
         macro_history.db; do
  echo "=== $f ==="
  stat -c "size=%s mtime=%Y sha256=" "$f"
  sha256sum "$f" | cut -d' ' -f1
done
```

> 預期結果：所有 sha256 與 Phase 2A 啟動前的 baseline 完全一致。
> baseline 已存於 `/tmp/phase2a_baseline.json`（M2 內部記錄）。

### 3.2 新增檔案就位

```bash
cd /home/ubuntu/macro-report

# 11 個目錄
for d in metadata/tasks metadata/reports templates/reports templates/prompts \
         config/reports config/policies config/publishers docs/contracts \
         docs/runbooks; do
  test -d "$d" && echo "OK $d" || echo "MISSING $d"
done

# 17 個檔案
for f in \
  docs/contracts/report_contract_v0.md \
  docs/contracts/task_lifecycle_v0.md \
  docs/contracts/dispatcher_api_v0.md \
  config/reports/macro_daily.yaml \
  config/reports/company_monthly.yaml \
  config/reports/industry_weekly.yaml \
  config/reports/constituents_quarterly.yaml \
  config/policies/retry.yaml \
  config/policies/scoring.yaml \
  config/publishers/telegram.yaml \
  templates/reports/macro_daily_v0.md.j2 \
  templates/reports/company_monthly_v0.md.j2 \
  templates/reports/industry_weekly_v0.md.j2 \
  templates/reports/constituents_quarterly_v0.md.j2 \
  templates/prompts/research_interpretation_v0.md \
  docs/runbooks/phase2a_migration_runbook.md \
  PHASE2A_IMPLEMENTATION_SUMMARY.md; do
  test -f "$f" && echo "OK $f" || echo "MISSING $f"
done
```

> 預期結果：全部 OK。

### 3.3 Cron 行為等價

```bash
# 4 個 cron job 跑 1 週（從 2026-07-08 起算）
# 預期：跟 Phase 2A 之前完全等價
# - exit code 0/1 行為不變
# - Telegram 訊息格式不變
# - SQLite 寫入行為不變

# 觀察 logs/2026-07-XX.json
tail -n 50 /home/ubuntu/macro-report/logs/$(date +%Y-%m-%d).json
```

### 3.4 生產行為不變的快速 spot check

```bash
# 手動觸發一次 macro_daily（與 Phase 2A 之前等價）
cd /home/ubuntu/macro-report
bash run.sh

# 預期：
# - 抓 yfinance 6 個 ticker
# - 計算 verdict
# - print 報告到 stdout
# - exit 0

# 不應出現：
# - yaml 載入錯誤（因為 .py 不讀 yaml）
# - jinja2 錯誤（因為 .py 不讀 j2）
# - dispatcher 連線錯誤（因為沒有 dispatcher）
```

---

## 4. Rollback 步驟

Phase 2A 全部是 additive，rollback 就是「刪掉新增的東西」。估計 **5 分鐘** 內
可還原。

```bash
cd /home/ubuntu/macro-report

# 1. 移除 17 個檔案
rm -f docs/contracts/report_contract_v0.md
rm -f docs/contracts/task_lifecycle_v0.md
rm -f docs/contracts/dispatcher_api_v0.md
rm -f config/reports/macro_daily.yaml
rm -f config/reports/company_monthly.yaml
rm -f config/reports/industry_weekly.yaml
rm -f config/reports/constituents_quarterly.yaml
rm -f config/policies/retry.yaml
rm -f config/policies/scoring.yaml
rm -f config/publishers/telegram.yaml
rm -f templates/reports/macro_daily_v0.md.j2
rm -f templates/reports/company_monthly_v0.md.j2
rm -f templates/reports/industry_weekly_v0.md.j2
rm -f templates/reports/constituents_quarterly_v0.md.j2
rm -f templates/prompts/research_interpretation_v0.md
rm -f docs/runbooks/phase2a_migration_runbook.md
rm -f PHASE2A_IMPLEMENTATION_SUMMARY.md

# 2. 移除 9 個空目錄（如果整個環境不再需要 Phase 2）
# 注意：這些目錄在 Phase 2A 之前可能不存在；rmdir 會略過非空目錄
rmdir config/reports 2>/dev/null
rmdir config/policies 2>/dev/null
rmdir config/publishers 2>/dev/null
rmdir templates/reports 2>/dev/null
rmdir templates/prompts 2>/dev/null
rmdir metadata/tasks 2>/dev/null
rmdir metadata/reports 2>/dev/null
rmdir docs/contracts 2>/dev/null
rmdir docs/runbooks 2>/dev/null

# 3. 驗證 rollback 後狀態
ls /home/ubuntu/macro-report/
# 預期：與 Phase 2A 啟動前完全相同
```

**rollback 不會動到**：
- 4 個 .py 源碼
- run*.sh
- 2 份 JSON config
- macro_history.db
- ~/.hermes/cron/jobs.json
- /home/ubuntu/macro-venv
- supervisord / cloudflared 設定

---

## 5. Phase 2B 怎麼用這些 artifacts

### 5.1 介面對齊的 source of truth

Phase 2B 寫 `reports/base.py` 時，3 個 abstract method 的**欄位名 / 欄位順序 / 必
填性**完全照 `docs/contracts/report_contract_v0.md` §10 的 mapping table 與
`config/reports/*.yaml` 的 `input_sources / analyzer / renderer` 段落。

### 5.2 task_id 產生器

Phase 2B 寫 `tasks/schema.py` 時，`task_id` 產生函式照 `docs/contracts/task_lifecycle_v0.md`
§2 規格：

```python
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

def make_task_id(report_type: str, when: datetime | None = None) -> str:
    tz = ZoneInfo("Asia/Taipei")
    when = when or datetime.now(tz)
    short = secrets.token_hex(3)
    return f"{report_type}_{when.strftime('%Y%m%d')}_{short}"
```

### 5.3 dispatcher 端點契約

Phase 2B 末 / Phase 2C 寫 `tasks/dispatcher.py` 時，6 個 HTTP endpoint 的
request/response shape 完全照 `docs/contracts/dispatcher_api_v0.md`。

### 5.4 Plugin 評分門檻

Phase 2B 寫 `assess_*` 包裝時，從 `config/policies/scoring.yaml` 讀門檻值（取代
硬編碼）。**Phase 2A 不動硬編碼**，Phase 2B plugin 改為「先讀 yaml，yaml 缺漏時
fallback 硬編碼」。

### 5.5 Plugin template 渲染（Phase 2B 末可選）

Phase 2B 末若要把 `format_*` 函式本體拆 5 段，可從 `templates/reports/*_v0.md.j2`
起點改。但**不強迫** Jinja2 化 — 純 inline Python 拼接也是合法選項。

### 5.6 Prompt 版本化（Phase 2C 起步）

Phase 2C 改寫 cron job prompt 時，可從 `templates/prompts/research_interpretation_v0.md`
取得 prompt template 結構概念。

---

## 6. 風險與假設

### 6.1 風險

| 風險 | 機率 | 影響 | 緩解 |
|---|---|---|---|
| LSP 對 yaml 報錯（`Missing property "terms"`） | 確定發生 | 無 | 純 LSP 工具誤判，runtime 不讀檔 → 無影響 |
| 鼎鼎忘記 Phase 2A 是純文件、手動執行 yaml | 低 | 無（yml 沒 .py reader） | 本 runbook + summary 反覆強調 NOT LOADED BY PRODUCTION |
| Phase 2B 急於把 yaml 接上現有 .py | 中 | 高 | Phase 2B 必須先寫 plugin wrapper，**不得直接改 macro_daily.py 等 4 個 .py** |
| `secrets.token_hex(3)` collision | 極低 | 低 | 6 hex = 16M 空間，當天 4 report × 多 rerun 仍 < 0.001% 碰撞 |
| 空目錄被 git 忽略 | 低 | 低 | .gitkeep 由 Phase 2B 評估是否加 |

### 6.2 假設

- 假設 1：Phase 2A 期間**不會**有人手動跑 `python -c "import yaml; yaml.safe_load(open('config/reports/macro_daily.yaml'))"` 並把結果丟進 production。
- 假設 2：Phase 2A 期間**不會**有人把 yaml 寫進任何 cron job 的 prompt。
- 假設 3：Phase 2A 期間**不會**有人寫 .py 去 import 這些 yaml 或 j2。
- 假設 4：M2 在 Phase 2B 啟動前**不會**忘記「freeze the core」原則。

### 6.3 什麼情況下要中止 Phase 2A

- 如果 `macro_daily.py` 任何一行的 mtime 在 Phase 2A 期間被改 → **立即中止**並
  還原。
- 如果 `macro_history.db` schema 變化（PRAGMA table_info 結果不同）→ **立即中止**。
- 如果任何 cron job exit 1 比率 > Phase 2A 之前 7 天平均 → 立即停止。

---

## 7. 下一步建議

請見 `PHASE2A_IMPLEMENTATION_SUMMARY.md` §「Next Recommended Tasks for Phase 2B」。

---

## 8. 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| 1.0 | 2026-07-08 | Phase 2A 初始 runbook。 |
