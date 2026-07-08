# Phase 3 — Milestone Roadmap

狀態：DESIGN ONLY
目標：時程、驗收標準、go/no-go gate。

---

## 0. 總覽（Timeline）

```
Q3 2026                  Q4 2026                  Q1 2027
─────────────────────────────────────────────────────────
Phase 3A (3 週)         Phase 3B (4 週)         Phase 3C (4 週)         Phase 3D (2 週)
核心建構 + shadow run   雙軌期 + 整合 + API      GPT 整合 + Server       Dashboard + 文件
─────────────────────────────────────────────────────────
W27 W28 W29              W30 W31 W32 W33          W34 W35 W36 W37         W38 W39
```

- **Phase 3A**：3 週（21 天）— 核心建構 + 7-day shadow run
- **Phase 3B**：4 週（28 天）— 雙軌期 + 整合到 Phase 2B framework + JSON-RPC
- **Phase 3C**：4 週（28 天）— GPT Orchestrator 整合 + Server 上線
- **Phase 3D**：2 週（14 天）— Dashboard + 文件

總計約 13 週（91 天）。可壓縮但不建議（每個 gate 都必要）。

---

## 1. Phase 3A — 核心建構 + Shadow Run（3 週）

### 1.1 目標
完成 `phase3/` 核心模組（datamodel、signal engine、3 個 scorer、SQLiteGraphStore、config、CLI）、完整 unit test 覆蓋、7 天 shadow run 驗證零 production 干擾。

### 1.2 Week-by-Week 拆解

#### Week 1（W27）— DataModel + Signal Engine
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | datamodel 全部 frozen dataclass + unit test | `phase3/datamodel/*.py` + 100% 覆蓋 |
| 3-4 | Signal Engine 核心（adapt / weight / decay / confidence / aggregate） | `phase3/signals/engine.py` + 5 個輔助檔 + tests |
| 5 | 4 個 SourceAdapter + unit test | `phase3/signals/adapters/*.py` + tests |
| 5 | decay function（linear / exponential / step / none）+ unit test | `phase3/signals/decay.py` + tests |

**Week 1 Gate**：
- [ ] 80%+ test coverage on `datamodel/` + `signals/`
- [ ] `python3 -m phase3 signal process --source yfinance --entity 2330` 跑通
- [ ] `mypy --strict` 全綠

#### Week 2（W28）— Scoring + Storage
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | 18 個 dimension 函式 + 5 個 transformation + unit test | `phase3/scoring/dimensions/*` + `transformations/*` + tests |
| 3 | MacroScorer + IndustryScorer + CompanyScorer + unit test | `phase3/scoring/{macro,industry,company}.py` + tests |
| 4 | Cross-layer adjustment + explain() + unit test | `phase3/scoring/cross_layer.py` + `explain.py` + tests |
| 5 | Storage (db.py / schema.py / migrations.py / 3 個 repo) + unit test | `phase3/storage/*` + tests |

**Week 2 Gate**：
- [ ] 80%+ test coverage on `scoring/` + `storage/`
- [ ] `python3 -m phase3 score macro --date 2026-07-08` 跑通
- [ ] `intelligence.db` schema 與 docs/phase3/07_data_model.md 一致

#### Week 3（W29）— Graph + CLI + Shadow Run Start
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | GraphStore (SQLite) + EvidenceTracer + BFS/DFS/shortest_path + unit test | `phase3/graph/*` + tests |
| 3 | ConfigLoader + Validator + Hasher + unit test | `phase3/config/*` + tests |
| 4 | CLI 全套（signal / score / graph / config / explain / backtest） + unit test | `phase3/cli/*` + tests |
| 5 | **Shadow Run Start**：所有 6 個 cron job 跑完後，**靜默**觸發 Phase 3 跑分數，結果寫 SQLite 但**不**送到 Telegram |

**Week 3 Gate**：
- [ ] 80%+ overall coverage
- [ ] `phase3 score all --date <today>` 跑通
- [ ] `phase3 graph trace --score <id>` 跑通
- [ ] **不修改任何現有 production 檔案**（byte-for-byte check）

### 1.3 Phase 3A 結束 Gate（Go/No-Go for 3B）

✅ 必過（不然就停在 3A 修）：
- [ ] **零 production 干擾**：現有 cron 跑出來的 Telegram 訊息與 3A 開始前 byte-identical
- [ ] **7-day shadow run 完成**且**零 crash**
- [ ] **整體 test coverage > 80%**（細節：datamodel > 95%, signals > 85%, scoring > 85%, graph > 80%）
- [ ] **`mypy --strict` 全綠**
- [ ] **所有 Phase 3A 完成的程式碼有對應 unit test**（新增無 test 的 code = fail）
- [ ] **`intelligence.db` 跑 7 天後 node count / edge count 統計合理**（例如 macro 24×7 = 168 個 score_snapshot, 8 industries × 7 = 56, companies × 7）
- [ ] **`intelligence.db` 跑 7 天後 query latency P95 < 200ms**（graph 簡單查詢）
- [ ] **shadow run 期間新舊分數比對**：對同一個 entity，新 macro_scorer 與現有 `macro_daily.liquidity_score` 相關性 > 0.7

❌ 必不通過（任何一條 = stop）：
- [ ] 任何現有 production 檔案被改
- [ ] 任何 cron job 失敗
- [ ] 任何 score 出現 NaN / inf

---

## 2. Phase 3B — 雙軌期 + 整合 + API（4 週）

### 2.1 目標
Phase 3 score 變成「可選增強」接到現有 report pipeline；JSON-RPC server 上線供 GPT 查詢；feature flag 控制。

### 2.2 Week-by-Week 拆解

#### Week 4（W30）— BaseReport 整合（不改既有 .py）
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | 寫 thin adapter：把現有 `macro_daily.py` 的 fetch 結果餵給 `MacroScorer.score()` | `reports/plugins/macro_daily_phase3_adapter.py` |
| 3 | 同上：industry + company | `reports/plugins/*_phase3_adapter.py` |
| 4 | 修改 `macro_daily.py` 在 main() 結尾 **append**（不替換）Phase 3 score 摘要 | `macro_daily.py` 增量更新（最小 diff） |
| 5 | 7-day byte-identical 驗證：現有段落不變，新段落是「Phase 3 評分：+28（信心 0.78）」 | 對照組 + 實驗組 |

**Week 4 Gate**：
- [ ] 現有 4 支 .py 的「既有段落」byte-identical
- [ ] 新增的 Phase 3 段落可開關（feature flag `phase3_report_integration.enabled`）

#### Week 5（W31）— Feature Flag 與回滾
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | 在 `config/phase3/enabled.yaml` 加 `report_integration` 區段 | config |
| 3 | 寫 `FeatureGate` 抽象（per-feature kill switch） | `phase3/feature_gate.py` |
| 4 | 把 4 支 .py 的 Phase 3 段落接到 FeatureGate | 4 支 .py |
| 5 | 壓力測試：開啟 / 關閉 feature 後的 Telegram 訊息對照 | 測試報告 |

**Week 5 Gate**：
- [ ] 關 feature → Telegram 訊息與 Phase 3A 結束前 byte-identical
- [ ] 開 feature → 既有段落 byte-identical + 新段落出現

#### Week 6（W32）— JSON-RPC Server
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | FastAPI app + routes (8 個端點) | `phase3/server/app.py` + `routes.py` |
| 3 | Bearer token auth + rate limiting | `phase3/server/auth.py` + `ratelimit.py` |
| 4 | OpenAPI yaml 自動生成 | `docs/phase3/openapi.yaml` |
| 5 | 本地 + Cloudflare tunnel 部署；對外 https | `phase3.biaobecue.com` |

**Week 6 Gate**：
- [ ] 8 個端點全部可呼叫
- [ ] Bearer token 驗證 + rate limit 驗證
- [ ] Cloudflare tunnel 穩定
- [ ] 既有 hermes_runtime-bridge / Cloudflare 設定**不被改動**

#### Week 7（W33）— Backtest 工具 + 文檔
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | `phase3 backtest run` 完整實作 | `phase3/cli/backtest.py` + tests |
| 3 | 30-day backtest 跑一遍 macro / industry / company | `/tmp/backtest_diff.json` |
| 4 | 補齊所有 README、docstring、API doc | `phase3/README.md` + 各模組 docstring |
| 5 | Security review：token / path traversal / SQL injection | 安全報告 |

**Week 7 Gate**：
- [ ] `backtest run` 可在 5 分鐘內跑完 30 天 × 3 個 scorer
- [ ] diff 報告顯示所有分數變動 < 30 分（信心區間內）
- [ ] README + docstring 100% 覆蓋

### 2.3 Phase 3B 結束 Gate（Go/No-Go for 3C）

✅ 必過：
- [ ] **現有 4 支 report 完整可運作**（feature flag off 狀態下）
- [ ] **Phase 3 增強可開可關**（feature flag on 狀態下多出新段落，不影響既有段落）
- [ ] **JSON-RPC server 上線**且**通過 security review**
- [ ] **30-day backtest 顯示新 config 與舊 config 的分數差異在合理區間**（< 30 分 / dim）
- [ ] **既有 hermes_runtime-bridge 不受影響**

---

## 3. Phase 3C — GPT 整合 + Server 強化（4 週）

### 3.1 目標
Phase 3 接到 ChatGPT Custom GPT Action；Server 強化（idempotency、cache、observability）；LLM 強化（opt-in 解釋）。

### 3.2 Week-by-Week 拆解

#### Week 8（W34）— ChatGPT Custom GPT Action
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | 設計 GPT Action schema（從 OpenAPI 轉） | GPT Action manifest |
| 3 | 對 GPT 編排者寫合約文件 | `docs/phase3/gpt_orchestrator_contract.md` |
| 4 | 從 GPT 端呼叫 8 個端點 e2e 驗證 | e2e 測試 |
| 5 | 寫 GPT prompt 模板（給鼎鼎在 ChatGPT 用） | `templates/gpt_prompts/*.md` |

**Week 8 Gate**：
- [ ] GPT 可成功呼叫 `POST /v1/phase3/score/company` 並解析回應
- [ ] GPT 可呼叫 `GET /v1/phase3/evidence/{score_id}` 並整理 evidence 給鼎鼎看

#### Week 9（W35）— Idempotency + Cache
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | Idempotency-Key header + SQLite cache | `phase3/server/idempotency.py` |
| 3 | Score cache：同 (entity, scorer_type, as_of) 1 小時內回傳同樣結果 | `phase3/server/cache.py` |
| 4 | Cache invalidation：config_hash 變動時自動 invalidate | 測試 |
| 5 | 壓力測試：1000 RPM 連續 1 小時 | 測試報告 |

**Week 9 Gate**：
- [ ] Idempotency 100% 正確（同 key 同 response）
- [ ] Cache hit ratio > 60%（daily query pattern）

#### Week 10（W36）— Observability
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | 結構化 log（trace_id / score_id / scorer / latency） | `phase3/observability/log.py` |
| 3 | Metrics endpoint `/v1/phase3/metrics` | Prometheus-style output |
| 4 | 寫 dashboard 規格（Phase 3D 才實作） | `docs/phase3/dashboard_spec.md` |
| 5 | 跑 7 天收 log / metrics | 基準數據 |

**Week 10 Gate**：
- [ ] 每次 score 都有 trace_id
- [ ] metrics 包含 P50/P95/P99 latency、error rate、cache hit rate

#### Week 11（W37）— LLM 強化（opt-in）+ 文件
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | LLM 解釋層（opt-in 啟用） | `phase3/explain/llm_optional.py` |
| 3 | LLM prompt 設計：只摘要 evidence，不引入新 fact | prompt templates |
| 4 | A/B 測試：rule-based vs LLM 解釋 | 測試報告 |
| 5 | 完整 Phase 3 使用手冊 | `docs/phase3/USER_GUIDE.md` |

**Week 11 Gate**：
- [ ] LLM 解釋是 opt-in，預設關閉
- [ ] 7-day 監控：開 LLM 與不開的差異記錄

### 3.3 Phase 3C 結束 Gate

✅ 必過：
- [ ] GPT Custom GPT 可呼叫 Phase 3 完成 e2e demo
- [ ] Idempotency 與 cache 在 production 100% 正確
- [ ] LLM 解釋預設關閉、opt-in 可用
- [ ] User Guide 完整

---

## 4. Phase 3D — Dashboard + 文件收尾（2 週）

### 4.1 目標
Dashboard 上線（從 `metrics` endpoint 拉資料）；文件收尾；release note。

### 4.2 Week-by-Week 拆解

#### Week 12（W38）— Dashboard
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | Dashboard 設計（HTML + JS） | `dashboard/index.html` |
| 3 | 拉 metrics endpoint | 前端 |
| 4 | 連到 Cloudflare tunnel | `https://phase3-dashboard.biaobecue.com` |
| 5 | User test | 修正 |

#### Week 13（W39）— 文件 + Release
| Day | Task | Deliverable |
|---|---|---|
| 1-2 | 完整 README + 架構圖 + API 參考 | `docs/phase3/README.md` + 圖 |
| 3 | 對外 release note | `docs/phase3/RELEASE_NOTES_v1.0.md` |
| 4 | Phase 4 規劃（ML / multi-worker） | `docs/phase4/00_overview.md`（預留） |
| 5 | 慶祝 🎉（沒寫進 Gantt） |  |

### 4.3 Phase 3D 結束 Gate

✅ 必過：
- [ ] Dashboard 上線
- [ ] 對外文件齊全
- [ ] Phase 4 規劃文件存在

---

## 5. Go / No-Go 決策矩陣

每個 phase 結束時逐項 check：

| 項目 | 重要性 | 沒過怎麼辦 |
|---|---|---|
| 零 production 干擾 | 🔴 Critical | 整個 Phase 退回前一階段 |
| Test coverage > 80% | 🟡 Important | 補完再進 |
| Performance P95 < 500ms | 🟡 Important | Profiling + 優化 |
| 7-day shadow run 通過 | 🟡 Important | 重跑 shadow run |
| 文件完整 | 🟢 Nice | 可往後延，但 release 前必齊 |
| 30-day backtest 通過 | 🟡 Important | 重新校準 config |

---

## 6. 資源需求

| 角色 | Phase 3A | Phase 3B | Phase 3C | Phase 3D |
|---|---|---|---|---|
| M2（架構 + coding） | 主要 | 主要 | 主要 | 主要 |
| 鼎鼎（review） | 1 次/週 | 1 次/週 | 1 次/週 | 1 次/週 |
| Tony（review） | optional | optional | optional | - |
| GPT Orchestrator | - | - | 整合測試 | - |

**會議頻率**：每週 1 次 30 分鐘 review，鼎鼎逐項 check gate。

---

## 7. 風險與緩解

| 風險 | 緩解 |
|---|---|
| Phase 3A 沒過導致後續延遲 | shadow run 7 天，可提前在 Week 2 末開始 |
| 鼎鼎中途改 config 導致 backtest 失敗 | 改 config 流程要走 PR review + auto-backtest |
| GPT 整合需要 ChatGPT Plus 訂閱 | 確認有訂閱後再做 3C；無訂閱則 3C 延後 |
| LLM 解釋 A/B 測試結論不明 | 預設關閉 LLM 即可，問題不影響主流程 |
| 13 週太長 | 每個 phase 結束都有可運作的 artifact，可中斷 |

---

## 8. 與 Phase 2 / 4 的時程關係

```
Q3 2026                              Q4 2026
─────────────────────────────────────────────────────────
現在     Phase 2B 已上線、Phase 2C 待辦
─────────────────────────────────────────────────────────
W27-W29  ← Phase 3A
W30-W33  ← Phase 3B
W34-W37  ← Phase 3C
W38-W39  ← Phase 3D
         ───────
         Phase 3 v1.0 release
```

- **Phase 2C dispatcher** 可在 Phase 3A 期間平行做（不衝突）
- **Phase 4 ML** 在 Phase 3 完成後才開始

---

## 9. Release Artifacts（v1.0 必備）

- [ ] `phase3/` 完整 source code
- [ ] `config/phase3/` 完整 config
- [ ] `intelligence.db` schema v1.0
- [ ] `tests/phase3/` 100% 覆蓋
- [ ] `docs/phase3/` 11 個文件 + `openapi.yaml`
- [ ] `dashboard/` 可運作
- [ ] `https://phase3.biaobecue.com` 公開端點
- [ ] `https://phase3-dashboard.biaobecue.com` dashboard
- [ ] `RELEASE_NOTES_v1.0.md`
- [ ] Demo 影片（鼎鼎要求的話）

---

_本 roadmap 為設計提案，實際時程可由鼎鼎調整。_
