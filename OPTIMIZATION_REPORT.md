# 投資情報三層系統 — 優化分析報告

分析日期：2026-07-07
分析對象：Macro Economy Morning Report / Industry Trend Weekly Report / Company Research Monthly Report / Quarterly Constituents Update Reminder 四個排程與對應程式碼
分析者：M2（純研究，無程式碼變更）

---

## 0. 摘要（TL;DR）

四個排程皆已上線並穩定運作，SQLite 歷史庫 11 筆 macro + 50 筆 stock + 50 筆 institutional，整體「能跑、可讀、有存檔」。但目前處於「功能優先，品質債累積」階段，主要問題集中在五個面向：

1. 排程表達力不足：cron job prompt 是純文字，無結構化契約，LLM 容易「多做事」（reformat / 加評論）造成報告漂移。
2. 失敗靜默：cron agent 把 stdout 整份送出，沒有 exit-code gate、沒有 retry、沒有告警 threshold。
3. 資料流沒有冪等性：yfinance / TWSE 沒有 cache layer，相同 50 檔在 1 次月報中重抓 N+ 個欄位，T86 連打 35 天、每次 0.3s sleep。
4. 技術債散落：safe_num / format_net_shares / format_shares 三套相似函式，magic number 散在 12 處，沒有型別/dataclass，沒有 unit test。
5. 觀測性為零：沒有結構化日誌、沒有 metric、沒有 health endpoint，排程失敗只能靠鼎鼎主動發現。

本報告依「快速改善（1-2 天）／中期重構（1-2 週）／長期架構（1 季+）」分層提出 18 項建議，含優先順序與預估工作量。

---

## 1. 目前盤點

### 1.1 排程總覽（Hermes Cronjob 實際狀態）

| 排程 ID | 名稱 | Schedule（Asia/Taipei） | 上次狀態 | Deliver | 工具集 |
|---|---|---|---|---|---|
| ed214c19c4ac | 總體經濟晨報 | `30 8 * * 1-6`（週一至六 08:30）| ok 2026-07-07 08:31 | telegram | terminal |
| 60d92c57b826 | 產業趨勢週報 | `0 8 * * 1`（週一 08:00）| ok 2026-07-06 08:01 | telegram | terminal |
| 5eaa5fa9a50d | 公司研究月報 | `0 8 12 * *`（每月 12 號 08:00）| ok 2026-07-12 待跑 | telegram | terminal |
| af64556bc8e9 | 季度成分股更新提醒 | `0 9 1 1,4,7,10 *`（1/4/7/10 號 09:00）| ok 2026-07-01 09:10 | telegram | （無）|

觀察點：
- 三支都是 `deliver="telegram"`（自動 telegram toolset），但 prompt 沒有用 telegram tool 而是要求「將輸出原樣送出」→ 隱含走默認 channel 5132341473，**沒有指定 chat_id**，相當依賴當下 context（cronjob 預設回 origin）。
- 04d8197ba6dab 晨報是另一個獨立 cron（`morning-brief-delivery`），8:00 派送 weather+行程+夢境建議。**總體經濟晨報 8:30 會撞在一起**，鼎鼎會在 30 分鐘內收到兩份長文。
- 月報只到 12 號，6/12 沒跑、跳到 7/12（7/1 才跑 manual 觸發）。**檢查後發現月報上次 last_run_at 是 2026-06-22 00:56**，6/12 似乎漏跑 — 雖屬 ok 但實質沒在預定時間觸發，**schedule 合約沒有被嚴格保證**。

### 1.2 程式碼地圖

```
/home/ubuntu/macro-report/
├── macro_daily.py         (9.0K) 總體 — yfinance × 6 ticker
├── industry_weekly.py     (10.2K) 週報 — RSS × 8 產業
├── company_monthly.py     (22.7K) 月報 — yfinance × 50 + T86 × 35 天
├── institutional.py       (4.6K)  TWSE T86 累積
├── db.py                  (8.3K)  SQLite 3 張表
├── industry_config.json   (6.0K)  8 產業 + 6 個 DIGITIMES feeds + 1 鉅亨網
├── taiwan50_config.json   (4.8K)  50 成分股
├── run.sh / run_weekly.sh / run_monthly.sh（皆為 venv 啟動 wrapper）
└── logs/                  ~50 個 JSON
```

venv：`/home/ubuntu/macro-venv/`（yfinance 1.4.1、pandas 3.0.3、requests 2.34.2）。

### 1.3 執行流程（Layer 1 為例）

```
cron trigger @ 08:30
  → cron agent 啟動（enabled_toolsets=["terminal"]）
  → prompt 寫「1. terminal: bash run.sh  2. 將輸出原樣送給鼎鼎」
  → run.sh: source venv && python3 macro_daily.py 2>&1
  → fetch_indicators() → assess_liquidity() → format_report()
  → stdout 全文（~50 行）→ stderr：「✅ 已寫入歷史資料庫」
  → cron agent 收到 stdout，照 prompt 把整份送 telegram
  → 結束
```

`company_monthly.py` 流程相同，但多了 T86 loop（35 天 × sleep 0.3s ≈ 10s sequential）。

### 1.4 資料現況

- `macro_daily`：11 筆（6/21 - 7/6），6/28 缺漏（週日無跑，預期內）。
- `stock_monthly`：50 筆（7/1 寫入，來自 7/1 09:10 manual 觸發）。
- `institutional_daily`：50 筆（7/1，22 trading days）。
- **覆蓋率**：macro 連 11 天、stock 只 1 個 snapshot、institutional 只 1 個 snapshot。**`get_foreign_streak` / `get_pe_percentile` 等查詢函式因樣本不足（< 3）目前永遠回傳 N/A** — 技能文件承諾的功能實際上還沒被驗證過。

---

## 2. 失敗點與技術債（按嚴重度排序）

### 🔴 P0 — 必須修，會在月內出問題

**F1. 月報 6/12 漏跑、5/12 也沒紀錄**
- 排程是 `0 8 12 * *`，next_run_at 為 2026-07-12T08:00:00。但 last_run_at 為 2026-06-22T00:56（manual），**沒有任何一次是 cron 自動觸發的紀錄**。
- 可能原因：Abacus 容器 reset 後 cron 沒被排到 first run，或時區/容器時間漂移。
- 風險：鼎鼎每月期待 12 號收到月報，目前其實是靠 manual 觸發在補。
- 建議驗證：馬上手動 `cronjob(action='run', job_id='5eaa5fa9a50d')` 並觀察 next_run_at 變化。

**F2. yfinance 沒有 cache，連跑會被 throttle**
- 7/2 → 7/3 期間 US2Y 三筆同值（3.895999...），USDTWD 也呈現週末 stale（沒 close → 沿用前值）。
- yfinance 對 Yahoo 端的請求沒有去重，Layer 3 一次月報打 50 個 ticker 每個 .info + .quarterly_income_stmt，**單次月報至少 50-100 個 HTTP round-trip**。
- 風險：Yahoo 限流後丟 HTTPError 整批失敗，currentPrice 變 None，dist_from_high 變 N/A，月報「🚀 Top 5 全部缺值」。

**F3. T86 連打 35 天 × 0.3s sleep = 10.5s sequential + 35 次 round-trip**
- 沒有平行化、沒有斷點續傳、沒有 cache。**如果中途一筆 stat != "OK" 就丟棄整日資料**（line 81 `continue`），不重試。
- 風險：TWSE 偶發 503，10 個交易日就漏；連打容易觸發 WAF。

**F4. cron prompt 沒有 exit-code gate**
- prompt 寫「1. terminal: bash ...  2. 將 stdout 原樣送給鼎鼎」。如果 `python3 macro_daily.py` 因 DB 失敗或 yfinance timeout raise Exception，cron agent 拿到 `❌ 報告生成失敗: ...` 整段錯誤訊息**一樣照送**（符合 prompt 2）。
- 結果：鼎鼎 8:30 收到一份「❌ 報告生成失敗」訊息，**被當成正常報告**。
- 應該：prompt 必須先 `if exit_code != 0: send `⚠️ X 排程失敗：` + 短摘 + exit_code；else: send stdout verbatim`。

**F5. US2Y/DXY 週末 stale 沒標示**
- yfinance 在週末會回傳前一週五 close，report 的「變動」其實是週五 vs 週四。
- 但 `data["date"]` 是 `latest.name.date()`，已是週五 — 報告 footer 雖然印 `⏱ 資料日期: 2026-07-06`，但**沒有「距今 X 天」的提示**，鼎鼎可能誤以為是當天資料。
- 已有 skill 註明「週末資料 staleness」是 pitfall，但**程式沒實作 staleness 警告**。

### 🟠 P1 — 一個月內修，影響資料可信度

**F6. 8:00 晨報 vs 8:30 總體經濟晨報撞期**
- 兩份長文相隔 30 分鐘，鼎鼎要分兩次看，注意力切換成本高。
- 建議：合併或錯開到 9:00（避開 morning brief 主體）。

**F7. institutional_daily 用 PK=(date, code) 存累積值，邏輯反直覺**
- 存的是「近月累積」，不是「當日 delta」。同一支股票 7/1 跟 8/1 各一筆，8/1 會 `INSERT OR REPLACE` 7/1 那一筆（如果同 key），**歷史累積值被覆蓋**。
- `get_foreign_streak` 拿每日 snapshot 的差值當「當日買賣超」其實是**字面上對、語意上錯** — 它把「累積淨額的當日變化」當「當日 delta」，但 daily snapshot 寫入頻率是 1/月（cron 觸發時），**streak 計算實質上完全沒意義**（中間日子的值是同一份 snapshot 的重複）。

**F8. 沒有 rate-limit / retry / backoff for T86**
- 0.3s sleep 是禮貌，但沒有 exponential backoff，沒有 retry 機制。T86 偶爾 503 就丟整天。
- 應該：3 次重試 + exponential backoff + 至少 80% 交易日成功才視為合法累積。

**F9. format_report 函式超過 100 行，混排資料 + 評分 + 渲染**
- `format_monthly_report` 一個函式 200+ 行做 5 個區塊（總覽、Top 5 列表、sector 群組、法人排行、footer）。
- 任何修改（例如想加「殖利率 > 5% 標星」）都要在長函式內找插入點。
- 應該：拆成 `format_summary()` / `format_rankings()` / `format_sector_block()` / `format_institutional_rankings()` / `format_footer()`。

**F10. magic number 散落**
- 評分 threshold（ROE 25/15/8、營收成長 20/0/-10、殖利率 4、VIX 15/20/30）在 4 個函式重複。**改一個要改 4 處**。
- 應該：抽 `config/scoring.py` 或 `policy.yaml`，版本控管可見。

### 🟡 P2 — 三個月內修，提升可維護性

**F11. industry_weekly.py 跨 feed 沒有去重 timestamp 標準化**
- DIGITIMES feed 的 pubDate 是 RFC822 格式，財訊是另一個，鉅亨網又是另一個。`parsedate_to_datetime` 對某些邊角格式會回 None → `filter_by_date` 走 fallback 路徑「無日期直接收」→ 過期新聞也混進去。
- 應該：所有 pub_date 一律 `datetime.now(TZ_TAIPEI) - timedelta(days=N)` 內才收，否則丟棄（不要 fallback）。

**F12. 沒有 unit test**
- 整個專案 0 個 test。`safe_num("%" vs "pct")` 的歷史 bug（-8.37 → -837%）就是沒 test 才會發生。
- 應該：至少 6 個 unit test 覆蓋 safe_num、format_net_shares、assess_stock、assess_liquidity、filter_by_date、analyze_supply_chain_impact。

**F13. 沒有結構化日誌**
- 全部 `print(..., file=sys.stderr)`。要 trace 一支股票的失敗原因只能讀 source code。
- 應該：用 `logging` module + 結構化欄位（`logger.info("stock_fetched", extra={code, sector, duration_ms, error})`）。

**F14. config 沒有 schema 驗證**
- `industry_config.json` / `taiwan50_config.json` 改壞一個 key（例如 `digitimes_feeds` 拼成 `digitimes_feed`）要等到執行才發現。
- 應該：用 pydantic 或 jsonschema 在 `load_config()` 開頭驗證，壞資料 fail-fast。

**F15. 沒有去識別化 / sanitize layer**
- 報告 footer 寫「製作: M2」OK，但 log JSON 完整存了 `report` 全文（macro_daily 寫進 logs/YYYY-MM-DD.json），`/home/ubuntu/` 是共用的家目錄，**其他 profile / process 可以讀**。
- 應該：log 存 metadata + verdict + signals，不存完整 report 文本。

**F16. 沒有 type hint、沒有 docstring 一致性**
- 部分函式有 docstring、部分沒有；`fetch_stock_fundamentals` 的 return dict 沒標 type，整個 codebase 沒 mypy。

**F17. 沒有 dependency lock**
- `uv venv` 安裝時沒 freeze 版本（雖然現況版本穩）。一個月後重灌 venv 可能 yfinance 升級到 2.0 有 breaking change。
- 應該：`uv pip freeze > requirements.lock.txt` 跟著 git。

**F18. quarterly reminder 只 ping 沒驗證**
- `af64556bc8e9` prompt 只說「去元大投信看 0050 公告」+「更新 config」。**沒有實際去查公告，也沒有驗證 taiwan50_config.json 是否真的有變**。
- 風險：鼎鼎忘記更新 → 下次月報用舊的 50 檔名單 → report 標的 0050 但少 4 檔。

---

## 3. 優化建議（Quick Wins → Long-Term）

### 3.1 🚀 Quick Wins（1-2 天，1 個 PR 量級）

| # | 建議 | 工作量 | 效益 | 優先 |
|---|---|---|---|---|
| Q1 | cron prompt 改成「exit code gate」：先 `if [ $? -ne 0 ]; then send error; else send stdout` | 1 hr | 消除 F4（失敗靜默）| **P0** |
| Q2 | 手動 `cronjob action=run` 觸發 5eaa5fa9a50d 看下次 next_run_at 是否正確 | 10 min | 驗證 F1 | **P0** |
| Q3 | 月報 + 晨報排程錯開：總體經濟晨報改 `0 9 * * 1-6`（避開 morning brief）| 5 min | 消除 F6 | P1 |
| Q4 | macro_daily.py 加 staleness 偵測：如果 `data["date"]` 距 today > 1 天 → 報告加 `⚠️ 資料落後 N 天` 警語 | 30 min | 消除 F5 | P1 |
| Q5 | db.py 把 institutional 改為「當日 delta」PK（新增 `delta_date` 欄位），或加 `snapshot_type` 區分 | 2 hr | 修 F7 | P1 |
| Q6 | quarterly reminder prompt 改成「先 fetch 元大投信公告頁 → diff config → 若有變動發提醒」 | 1 hr | 修 F18 | P1 |
| Q7 | 加 `uv pip freeze > macro-venv.lock.txt` 進 git | 5 min | 預防 F17 | P2 |
| Q8 | 把 `assess_stock` / `assess_liquidity` 的 magic number 抽到 `config/scoring.py` | 1 hr | 修 F10 | P2 |
| Q9 | 改 4 個 wrapper（run*.sh）統一成 `run.sh` 加 subcommand（`run.sh daily/weekly/monthly`），減少維護點 | 1 hr | DRY | P3 |

**預估總工作量：1 人天。**

### 3.2 🔧 Medium-Term Refactors（1-2 週）

| # | 建議 | 工作量 | 效益 |
|---|---|---|---|
| M1 | **引入 SQLite 結果快取層**：每個 ticker 每天只抓一次（`SELECT * FROM price_cache WHERE code=? AND date=?`），yfinance 失敗有 fallback 機制 | 2 天 | 修 F2、降低 80% outbound |
| M2 | **T86 重構**：平行化（ThreadPoolExecutor × 5 worker）、exponential backoff、續傳（如果 80% 交易日拿到就視為完整）、寫進 SQLite 之前先 dedupe | 3 天 | 修 F3、F8 |
| M3 | **format_monthly_report 拆 5 個小函式**（summary / rankings / sector / inst-rankings / footer） | 0.5 天 | 修 F9 |
| M4 | **pytest 基礎建設**：6 個 unit test 覆蓋 critical path（safe_num, format_net_shares, assess_stock, assess_liquidity, filter_by_date, supply_chain_impact） | 1 天 | 修 F12 |
| M5 | **結構化 logging**：用 `logging` module 取代 stderr print，加 `extra={stock, sector, duration_ms}` | 0.5 天 | 修 F13 |
| M6 | **config schema 驗證**：用 pydantic v2 驗 `industry_config.json` / `taiwan50_config.json`，壞資料 fail-fast | 1 天 | 修 F14 |
| M7 | **健排程包進 supervisord**：把 4 個 cron 改成 supervisord 排程（如果 Hermes cron 不可靠 → 退路方案），或加 watchdog | 1 天 | 修 F1、F4 |
| M8 | **Telegram 失敗退路**：如果 cron 沒送出 → fallback 寫進 SQLite 一個 `outbox` 表，下次 manual 觸發時 re-send | 1 天 | 補丁 |

**預估總工作量：1 人週。**

### 3.3 🏗️ Long-Term Architecture（1 季+）

| # | 建議 | 影響範圍 |
|---|---|---|
| L1 | **抽成 `macro-report` Python package**：目錄改 `src/macro_report/`，加 `pyproject.toml`、`__init__.py`、可 import 的 module（取代直接跑 `.py`） | 提升可測試性 / 可重用性 |
| L2 | **dashboard layer**：用 Streamlit 或 Gradio 在 `127.0.0.1:8501` 開個本機 dashboard，讀 SQLite → 即時看 macro/stock 歷史曲線（vix 趨勢、PE percentile、外資 streak） | 把「資料庫」變成「決策工具」 |
| L3 | **策略信號引擎**：cron 報告外加一份「今日要關注的 3 件事」（例如：VIX > 20 + 外資連 5 日賣超台積電 → 觸發警訊），用 rules engine（不靠 LLM） | 從「資料 → 決策」的最後一哩 |
| L4 | **資料源多元化**：yfinance 是單點風險，加 MacroMicro web_scrape fallback（已標在 skill 但沒實作）、加 FinMind API（台股專用，有月營收/月 EPS 官方） | 抗 Yahoo 限流 + 補強台股資料 |
| L5 | **Cloudflare Tunnel observability**：把 cron log 推送到 Hermes Dashboard（已上線），鼎鼎可在 web 看到 7 天成功率、報告長度、最後執行時間 | 補齊 observability |
| L6 | **報告 templating**：抽成 Jinja2 template（`templates/daily.md.j2`），LLM 只負責解釋，格式由 template 控 | 防 cron agent reformat |
| L7 | **L1↔L2↔L3 交叉引用**：L1 macro verdict 寫進 L2 / L3 報告的 header（"今日 macro 偏寬鬆 → 偏多看待"），L2 抓到某產業新聞 → 自動加進 L3 該產業 stock card | 升級成「層」而不只是「3 個獨立 report」 |
| L8 | **可移植性**：macro-report 整包 zip 可在另一台乾淨的 Abacus 機 30 分鐘內 rebuild（`bash install.sh`：裝 venv + 還原 DB + 註冊 cron + smoke test） | 降低「系統 reset 需手動恢復」風險 |

**預估總工作量：1-2 人月。**

---

## 4. 排程改善（Scheduling Improvements）

### 4.1 當前排程問題

| 問題 | 影響 |
|---|---|
| 8:00 morning brief 與 8:30 macro 撞期 | 鼎鼎 30 分鐘看 2 份長文 |
| 月報 `0 8 12 * *` 沒有 override 機制 | 12 號若遇週末/假日，月報不自動順延到下一個交易日 |
| weekly `0 8 * * 1` 週一 08:00 是美股週日收盤後、台股開盤前 — 資訊密度高（週末累積新聞 + 開盤前），OK |
| quarterly reminder `0 9 1 1,4,7,10 *` 沒驗證 component 真的有變動，純 ping | 鼎鼎要主動記得去做 |
| cron 沒排定 0 9 1 1,4,7,10 之外每月的「提醒週四檢查」| 月報後 4-5 天有問題無法回頭看 |

### 4.2 排程調整建議

1. **總體經濟晨報**：從 `30 8 * * 1-6` 改 `0 9 * * 1-6`（避開 8:00 morning brief）。
2. **月報**：`0 8 12 * *` 改 `0 8 D * *`，D 為「月營收公布後第一個交易日」邏輯（用 `if [ $(date +%u) -le 5 ] && day >= 12 && day <= 18; then run`），或最簡單維持 12 號但加 `if previous trading day is holiday → skip`。
3. **weekly**：維持 `0 8 * * 1`，是最佳時段。
4. **quarterly reminder**：從「提醒去查」改為「自動 fetch 元大投信公告 RSS → diff config → 若有差異才提醒」；可以加 `*/5 * * * 1-5` 每 5 分鐘在 1-10 號期間跑 polling（輕量），比月一次性可靠。
5. **加 health check cron**：`0 12 * * *` 中午 12:00 一個「昨日報告是否送達？」自檢（`sqlite3 macro_history.db "SELECT date FROM macro_daily ORDER BY date DESC LIMIT 1"` → 跟 today-1 比對），漏了發 alarm。

### 4.3 排程配置改寫建議（pseudo-config）

```yaml
# 未來用 YAML/JSON 統一管理（取代散落的 cronjob create）
schedules:
  macro_daily:
    cron: "0 9 * * 1-6"
    script: "run.sh daily"
    timeout: 120
    on_failure: "send telegram: ⚠️ 總體經濟晨報失敗，exit_code=$?"
    healthcheck: "sqlite macro_daily.date >= today-1"
  industry_weekly:
    cron: "0 8 * * 1"
    script: "run.sh weekly"
    timeout: 180
  company_monthly:
    cron: "0 8 12 * *"
    script: "run.sh monthly"
    timeout: 300
    skip_if: "is_holiday_tw"  # 用 TWSE 開休市日曆
  constituents_quarterly:
    cron: "0 9 1 1,4,7,10 *"
    script: "scripts/check_constituents.py"
    action: "diff_and_alert"  # 不只提醒，要實際比對
  health_check:
    cron: "0 12 * * *"
    script: "scripts/health_check.py"
```

---

## 5. 觀測性（Observability）

目前幾乎為零。建議三層：

### L1 結構化日誌（短期）
- 用 `logging` module，格式 `[%(asctime)s] %(levelname)s %(name)s %(message)s`
- 每支 stock fetch 印 `INFO macro_report fetch code=2330 duration_ms=1234 source=yfinance`
- 每個 cron 觸發印 `INFO cron_run job=macro_daily exit_code=0 duration_s=12`
- 寫進 `logs/<job>.log`（不是 stderr，stderr 給 cron agent 看）

### L2 Metrics（中期）
- 暴露 `127.0.0.1:8765/metrics` Prometheus endpoint
- 關鍵 metric：
  - `macro_report_last_success_timestamp` (gauge)
  - `cron_run_duration_seconds{job="..."}` (histogram)
  - `cron_run_exit_code{job="..."}` (counter)
  - `t86_trading_days_fetched` (gauge)
  - `yfinance_request_failures_total` (counter)
  - `report_lines_total{job="..."}` (counter)

### L3 儀表板（長期）
- Hermes Dashboard（已上線）加一頁 `/macro-health`
- 即時看到 4 個排程 7 天成功率、平均耗時、上一份報告大小
- 異常發 Telegram alarm（不靠 cron agent 自身判斷）

---

## 6. 測試策略

### 6.1 現況
0 個 unit test、0 個 integration test、0 個 fixture。

### 6.2 建議分層

**Unit（6 個，半天可完成）**
- `test_safe_num.py`：覆蓋 % / pct / 空值 / N/A / 0 / 負數
- `test_format_net_shares.py`：1 張 / 1 千張 / 1 萬張 / 0 / 負值
- `test_assess_stock.py`：ROE 各級距、營收成長各級距、金融股 vs 非金融股分支
- `test_assess_liquidity.py`：VIX < 15 / 15-20 / 20-30 / > 30 + 利差正負
- `test_filter_by_date.py`：naive vs tz-aware pub_date、邊界日（7 天前 23:59）
- `test_analyze_supply_chain_impact.py`：中文股號 / 數字股號 / 大小寫

**Integration（4 個，1 天）**
- `test_yfinance_fetch.py`：mock yfinance，驗證 50 檔全部 try/except
- `test_t86_accumulation.py`：mock urllib，驗證 35 天都 stat=OK + 部分 stat!=OK 的容錯
- `test_db_save_and_query.py`：用 tmp sqlite 跑完整 round-trip
- `test_cron_wrapper.sh`：用 stub bash 跑 run.sh，驗證 exit code 0/1 兩種

**Smoke（1 個，可手動）**
- `bash tests/smoke.sh`：跑 3 個 main 確認 1 分鐘內 exit 0

**E2E（可選）**
- 從 cron trigger → 抓 mock yfinance → mock telegram → 收到訊息

---

## 7. 設定管理（Configuration Management）

### 7.1 現況
兩個 JSON（industry / taiwan50）+ 三個 bash wrapper + 4 個 cronjob prompt，全散落。

### 7.2 建議
- 一個 `config/` 目錄：
  - `config/scoring.yaml` — 評分 threshold（ROE 25/15/8 等）
  - `config/tickers.yaml` — yfinance ticker 對照表
  - `config/industries.json` — 現有 industry_config
  - `config/constituents.json` — 現有 taiwan50_config
  - `config/schedule.yaml` — 排程時間表
- 用 pydantic v2 寫 schema，啟動時驗證
- 加 `config/.gitignore` 把 `constituents.local.json` 排除（允許個人 override）

---

## 8. 擴展性（Extensibility）

### 8.1 加新 Layer 4（例如 ETF 持股週報）

現況痛點：要把「macro_daily / industry_weekly / company_monthly / 4 wrapper / 4 cron」一整套都複製一份才能加新 layer。

### 8.2 建議：抽象出 `BaseReport`

```python
# reports/base.py
class BaseReport:
    name: str
    schedule: str
    
    def fetch_data(self) -> dict: ...
    def assess(self, data) -> tuple[str, list, int]: ...
    def format(self, data, verdict, signals, score) -> str: ...
    def save(self, data, verdict, score, signals) -> None: ...
    
    def run(self) -> int:
        data = self.fetch_data()
        verdict, signals, score = self.assess(data)
        report = self.format(data, verdict, signals, score)
        print(report)
        try: self.save(data, verdict, score, signals)
        except Exception as e: log.warning(f"save failed: {e}")
        return 0
```

加新 Layer 只要寫 `class ETFWeeklyReport(BaseReport)` + 一行 cron 註冊。

---

## 9. 實作 Roadmap（含優先順序）

### Phase A — 止血（1-2 天，Q1-Q6）
- 目標：消除最致命的 3 個失敗點（F1, F4, F5, F6, F18）
- 產出：cron prompt 改寫、staleness 偵測、排程錯開、quarterly 自動查
- 風險：低（純文字 / 5 min 級別改動）
- 驗收：4 個 cron 跑一週 0 失敗、錯誤有告警

### Phase B — 健壯（1-2 週，M1-M6）
- 目標：cache / retry / test / observability 一次到位
- 產出：6 個 unit test + cache layer + 重構 T86 + 拆 format
- 風險：中（要動 yfinance 抓取邏輯）
- 驗收：test 全綠、月報耗時從 5 分鐘降到 2 分鐘、T86 容錯率 > 95%

### Phase C — 升級（1 季，L1-L7）
- 目標：可維護、可觀測、可擴展
- 產出：package 化 + dashboard + 策略信號 + 多源 fallback
- 風險：中（重構既有程式碼，但有 test 保護）
- 驗收：加一個新 Layer 4 不用改 1 行既有 code

### Phase D — 治理（持續，季度檢視）
- 季度 review：cron schedule 是否仍合適、scoring 標準是否要調、config 是否要 update
- 季度 archive：log 超過 90 天進 cold storage

---

## 10. 風險與取捨

| 建議 | 風險 | 緩解 |
|---|---|---|
| M1 yfinance cache | 過期資料風險 | TTL = 24hr，過期就重抓 |
| M2 T86 平行化 | IP 被 ban | 限制 5 worker，exponential backoff |
| M5 結構化 logging | log 檔膨脹 | rotate + gzip，超 30 天刪除 |
| L1 重構成 package | 既有 run.sh 失效 | 保留 3 個 wrapper 作為 fallback，deprecate 標記 |
| L3 策略信號 | 假警報太多鼎鼎會關掉 | 設 threshold 觸發率 < 20%/月 |
| L7 L1↔L2↔L3 交叉引用 | 跨層耦合 | 用 SQLite 中介層，不要直接 import |

---

## 11. 結論

四個排程已從「跑得起」進化到「能穩定跑」，但離「production-grade 投資決策系統」還有 1-2 季的工程債要還。**最關鍵的 3 件事**：

1. **加 exit-code gate 與 observability**（P0，1 天可完成）— 排程失敗不再靜默。
2. **加 cache + retry layer**（P1，1 週可完成）— yfinance / T86 不再是單點失敗。
3. **加 unit test + 結構化 logging**（P1，1 週可完成）— 之後重構有保護網。

做完 Phase A + B（2 週內）就能從「被動等事故」轉成「主動控管品質」，Phase C（1 季）則讓系統從「能跑」升級成「能擴展」。

如果需要，下一份報告可以針對某一項（例如 Q1 cron prompt 改寫範例、或 M1 cache 層架構）做詳細設計。

---

*報告完*
