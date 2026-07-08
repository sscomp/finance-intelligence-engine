# Phase 3 — Risks & Assumptions

狀態：DESIGN ONLY
目標：把所有「可能讓 Phase 3 失敗」的因素列出來，附緩解策略。鼎鼎審查時可逐條 confirm / override。

---

## 1. Assumptions（事前提假設，未來驗證）

### A1. yfinance 欄位穩定
**假設**：yfinance 0.2.x 的 `info` dict 在未來 12 個月內不會有大規模欄位 rename。
**驗證方式**：Phase 3A 上線後跑 30 天，監控 `SubIndicatorResult.raw_value is None` 的比率。
**如果失敗**：fallback 到 `MacroMicro` / `Investing.com` web scrape，或建立欄位 alias 表。

### A2. 8 個產業的成分股清單穩定
**假設**：`industry_stocks.yaml` 預設的 8 個產業（AI/半導體/伺服器/PCB/網通/重電/金融/高股息）成分股 12 個月內不大幅變動。
**驗證方式**：與 `taiwan50_config.json` 對齊；季度成分股更新 cron 已上線（`af64556bc8e9`）。
**如果失敗**：Phase 3C 加「未分類公司」bucket，把新加入的 0050 成分股先放 bucket，跑 30 天後再決定是否新增產業。

### A3. SQLite 足以應付 graph
**假設**：graph_nodes < 10k、graph_edges < 50k 時 SQLite BFS 效能可接受。
**驗證方式**：Phase 3A 上線 30 天後量測 P95 BFS latency。
**如果失敗**：升級 NetworkX in-memory；介面不變（GraphStore ABC）。

### A4. 鼎鼎滿足於 -100~100 的單一數字
**假設**：把「總體 / 產業 / 公司」全部壓成 -100~100 對鼎鼎是直覺的。
**驗證方式**：Phase 3A 結束後做 user review（看 Telegram 訊息、解釋是否清楚）。
**如果失敗**：保留分數，但加 alternative 視覺化（radar chart、6 維度 bar）。

### A5. cross-layer 調整的權重合理
**假設**：tech 對 liquidity 0.05、金融對 rates 0.08 的敏感度係數是合理的初值。
**驗證方式**：跑 30 天，觀察「總體劇烈變動時，公司分數被調整的幅度是否合理」。
**如果失敗**：開放 config / 增加 A/B 模式（手動 vs 自動敏感度）。

### A6. 新聞情緒用關鍵字+規則就夠
**假設**：Phase 3A 不需要 LLM 做情緒標註。
**驗證方式**：比較 7 天 rule-based vs LLM-based 的情緒分數，相關性 > 0.7 視為可接受。
**如果失敗**：Phase 3B 加 LLM 標註 opt-in。

### A7. 每個 ScoreResult 的 evidence 數量 < 50
**假設**：typical score 的 evidence chain 不會爆量。
**驗證方式**：監控每個 score 的 `len(overall_evidence)` P95。
**如果失敗**：在 scorer 內加 `max_evidence` 截斷規則（保留 top-K by weight）。

---

## 2. Risks（事後可能出問題）

### R1. Scorer 數學 bug 導致錯誤分數流到 Telegram
**嚴重度**：🔴 高（投資決策影響）
**可能性**：中
**緩解**：
- 7-day shadow run：Phase 3A 期間，新 scorer 跑分數並 log，**不**送到 Telegram；Telegram 仍由舊邏輯發。
- 每個 scorer 必寫 unit test：固定 input → 固定 score（table-driven）
- evidence audit log：每個 score 存 `result_json` 進 SQLite，可隨時 replay
- kill switch：`config/phase3/enabled.yaml` 控制某 scorer 是否啟用，可即時關閉

### R2. Evidence 爆炸（每個分數帶 100+ evidence）
**嚴重度**：🟡 中（效能 + 訊息可讀性）
**可能性**：高
**緩解**：
- scorer 內設 `max_evidence_per_dimension: 10` 與 `max_evidence_per_score: 20` 截斷規則
- 截斷時保留「highest weight」的 evidence，記錄 `truncated_count` 欄位

### R3. Config 被亂改導致分數劇烈跳動
**嚴重度**：🟡 中
**可能性**：高（鼎鼎想試不同 weight）
**緩解**：
- config_hash 必寫入每個 score_snapshot（已 trace）
- 改 config 後必跑 30 天 backtest：拿歷史 raw data × 新 config 算分數，與既有分數做 diff
- 變動 > 30 分的維度必 warning

### R4. 與 Phase 2B framework 內部 API 不同步演化
**嚴重度**：🟡 中
**可能性**：低（Phase 3 不 import 內部 class）
**緩解**：
- Phase 3 透過 ABC 介面（SourceAdapter、GraphStore）與 framework 解耦
- ABC 介面 freeze 6 個月，破壞性變更必升 major version

### R5. LLM 解釋階段 hallucination 引入錯誤 fact
**嚴重度**：🟡 中
**可能性**：中
**緩解**：
- Phase 3A 預設**不啟用 LLM 解釋**；解釋用 `explain()` 純 rule-based 拼字串
- LLM 解釋是 opt-in：只在 user 明確要求時調用
- LLM prompt 設計為「只摘要 evidence，不許引入新 fact」

### R6. SQLite 三張新表 + 現有三張表 → 維護成本
**嚴重度**：🟢 低
**可能性**：低
**緩解**：
- 新表獨立檔案 `intelligence.db`，可單獨備份
- 既有 `macro_history.db` 完全不動
- schema migration 走 `SchemaMigrator` 類別

### R7. Macro 跨層調整過度疊加導致 company score 失真
**嚴重度**：🟡 中
**可能性**：中
**緩解**：
- 單一 macro dim 影響上限 5%（已在 `sector_macro_sensitivity.yaml` 預設）
- 整體 macro 影響上限 20%（clip）
- 調整前/後分數並列（`raw_score` 與 `score` 兩個欄位）方便 debug

### R8. Geopolitics 維度品質不穩
**嚴重度**：🟡 中
**可能性**：高
**緩解**：
- 預設使用關鍵字+規則標註（不需要 LLM）
- LLM 標註是 opt-in 強化
- 給定低預設 weight 0.10（影響有限）
- 加入「資料不足 → 維度分數 = 0」的安全預設

### R9. 6 個 scorer 全部跑完時間太長
**嚴重度**：🟡 中
**可能性**：中
**緩解**：
- 純 scorer 函式 < 1 秒；瓶頸是 I/O（yfinance/RSS/T86）
- Phase 3A 預設只在 cron job 觸發時跑（不快於每日一次）
- 單獨 macro scorer 可在 daily report 後跑，industry weekly 後跑，company monthly 後跑
- Phase 3C 之後考慮 lazy evaluation：只在 user 查詢特定公司時才跑 company score

### R10. 與現有 Telegram 報告流程衝突
**嚴使用者**：🔴 高（一旦壞掉，鼎鼎看不到報告）
**可能性**：中
**緩解**：
- **絕不**修改現有 `run.sh` / `run_weekly.sh` / `run_monthly.sh`
- Phase 3A 期間，Phase 3 是**純觀察層**，完全不接 Telegram
- Phase 3B 引入 feature flag 切換；預設關閉 Phase 3 路徑
- Phase 3C 完整切換前 7-day shadow run 比對

---

## 3. Out of Scope（明確不做，避免擴張）

- ❌ 即時交易 / 下單 / portfolio management
- ❌ ML 預測模型（XGBoost / LSTM 等）
- ❌ 即時報價 / 串流 API（WebSocket / SSE）
- ❌ 多市場（A 股 / 日股 / 港股等；目前只覆蓋台股 + 美股 + 國際 macro）
- ❌ 法人持倉明細（13F / 集保）
- ❌ LLM 主動發問（Phase 3 LLM 只做「把 score 翻成中文」）
- ❌ 修改任何現有 production file / cron / prompt
- ❌ 任何 Phase 2B framework breaking change
- ❌ Realtime dashboard（Phase 4）
- ❌ multi-worker / multi-region

---

## 4. Decisions Made by Default（已預設選擇，可被 override）

| 決策 | 預設 | 為什麼這樣選 | override 路徑 |
|---|---|---|---|
| 分數範圍 | -100~100 | 直覺對稱、易記 | 改 `ScoringPolicy.min/max` |
| 維度數量 | macro 6 / industry 6 / company 7 | 平衡可解釋性與覆蓋面 | 改 scorer 公式 |
| Cross-layer 影響上限 | 整體 20% | 避免疊加失真 | 改 `cross_layer_limits.yaml` |
| Graph 預設 backend | SQLite | 零外部依賴 | 升級 NetworkX |
| TTL | macro 24h / industry 24h / company 168h | 對應 Layer 1/2/3 排程頻率 | 改 manifest |
| Evidence 上限 | 10/dim, 20/score | 訊息可讀性 | 改 `evidence_limits.yaml` |
| Sector sensitivity 初值 | 半導體=0.05、金融=0.08 等 | 從經驗出發 | 跑 30 天後調 |
| Geopolitics 預設 | 關鍵字+規則，不啟用 LLM | 成本與可控性 | Phase 3B opt-in |
| New industry 新增 | 手動編輯 `industry_stocks.yaml` | 安全；避免自動分類錯誤 | Phase 4 自動 |

---

## 5. Kill Switch 設計

Phase 3 必備 3 個 kill switch，鼎鼎可隨時關閉：

1. **`config/phase3/enabled.yaml`** —
   ```yaml
   enabled: true
   scorers:
     macro: true
     industry: true
     company: true
     graph: true
   ```
   改 `enabled: false` → Phase 3 整個停擺，現有 report 照跑。

2. **`config/phase3/scorers/<name>.yaml`** —
   個別 scorer 可關。

3. **`config/phase3/cross_layer_adjustment.yaml`** —
   ```yaml
   macro_to_company: false     # 關掉 macro 對 company 的影響
   industry_to_company: false   # 關掉 industry 對 company 的影響
   ```

---

## 6. 對 Phase 2 / Phase 4 的影響

### Phase 2B framework（不變）
- `run.sh` / `run_weekly.sh` / `run_monthly.sh` / cron jobs / cron prompts **完全不動**
- BaseReport / Pipeline / Dispatcher / LocalStore 介面不動
- Phase 3 透過 `BaseReport.analyze()` 的 optional hook 接入（Phase 3B 才做）

### Phase 2C dispatcher（不衝突）
- Phase 2C 的 dispatcher 接 cron job 排程；Phase 3 接 scorer 排程
- 兩者可獨立運行；Phase 3C 合併時再加 adapter

### Phase 4（未來）
- ML 預測模型可讀 `score_snapshot` 訓練
- 多 worker / multi-region 可用 `intelligence.db` 做 state
- 即時 dashboard 可 query `score_snapshot` 與 `signal_log`

---

## 7. 法律與合規

- yfinance 為非官方 Yahoo Finance API；使用需自我負責（不對外提供 / 不商業化）
- DIGITIMES / 財訊 / 鉅亨網 RSS 為公開 RSS；引用時附 source link
- 個人 node（Person）資料：預設 opt-in，config 控制
- 不存任何個人投資組合資料（Phase 3 不涉及 portfolio）
- 不做任何投資建議（明確 disclaimer）

---

## 8. 風險登記簿（Risk Register）

| ID | 風險 | 嚴重度 | 可能性 | 緩解 | 負責模組 | Gate |
|---|---|---|---|---|---|---|
| R1 | Scorer bug 流到 Telegram | 🔴 | 中 | shadow run + kill switch | scorer | Phase 3B 上線前 |
| R2 | Evidence 爆炸 | 🟡 | 高 | truncation 規則 | scorer | Phase 3A |
| R3 | Config 亂改 | 🟡 | 高 | config_hash + backtest | config | Phase 3A |
| R4 | framework 不同步 | 🟡 | 低 | ABC 介面 freeze | graph | Phase 3A |
| R5 | LLM hallucination | 🟡 | 中 | opt-in | explain | Phase 3C |
| R6 | 維護成本 | 🟢 | 低 | 獨立 db | storage | Phase 3A |
| R7 | 跨層疊加失真 | 🟡 | 中 | clip + 並列欄位 | company | Phase 3A |
| R8 | Geopolitics 不穩 | 🟡 | 高 | 預設關鍵字 | macro | Phase 3A |
| R9 | 效能太慢 | 🟡 | 中 | lazy eval | dispatcher | Phase 3C |
| R10 | 衝突現有流程 | 🔴 | 中 | 不改現有檔案 | deploy | 永久 |

---

_鼎鼎審查時可逐條 confirm / override / add。所有未在文件出現的「隱藏假設」都會被 R 級風險登記。_
