# Research Interpretation Prompt — v0 placeholder

狀態：**DRAFT / NOT LOADED BY PRODUCTION**
用途：Phase 2B 之後 Research Agent 對分析結果做口語化解釋時的 prompt 起點
對應章節：`ARCHITECTURE_REVIEW_PHASE2.md` §8

---

## ⚠️ 重要

- 本檔案目前沒有任何 production runtime 載入它
- 4 個 cron job 的 prompt 仍存於 `~/.hermes/cron/jobs.json` 內，**未來 Phase 2C 才會改寫**（見 `ARCHITECTURE_REVIEW_PHASE2.md` §7.5）
- 變更本檔不會影響任何 cron 行為
- 本檔僅作為 Phase 2B 介面對齊的 placeholder

---

## 預期使用情境

當 BaseReport plugin 的 `analyze()` 結果需要額外的「口語化解釋」時（例如「為什麼
VIX 13.2 評為寬鬆」、「為什麼台積電評分 87」），dispatcher 可呼叫 LLM 並餵入本 prompt
產生 analyst commentary。

---

## 預期 Input Variables

| 變數 | 型別 | 說明 |
|---|---|---|
| `report_type` | string | enum 4 選 1 |
| `date` | str | `YYYY-MM-DD` Asia/Taipei |
| `verdict` | string | 評等字串 |
| `score` | int | 0-100 |
| `signals` | list | 個別指標信號 |
| `raw_indicators` | dict | 原始數字 |
| `audience` | string | `self`（鼎鼎本人）/ `external`（轉發情境） |

---

## Prompt 草案（v0）

```text
你是 M2 的投資情報解釋助手。請針對以下分析結果，產出 3-5 句的中文口語化解釋。

報告類型：{report_type}
日期：{date}（Asia/Taipei）
評等：{verdict}
分數：{score} / 100
個別信號：
{formatted_signals}

原始指標：
{formatted_indicators}

受眾：{audience}（self = 鼎鼎本人；external = 轉發給第三人時，措辭更中性）

要求：
1. 用繁體中文
2. 第一句直接講結論（與 verdict 一致）
3. 第二、三句解釋「為什麼是這個 verdict」（引用具體數字）
4. 第四、五句講「對 M2 的下一步動作有何建議」（例：觀望、追蹤、加碼）
5. 不超過 200 字
6. 不要加 emoji（footer 由 dispatcher 統一加）
7. 不要修改分析結果本身，只解釋

回傳 markdown 純文字即可，不要加 ``` 包裹。
```

---

## 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| 0 | 2026-07-08 | Phase 2A 初始 draft placeholder。**未實際載入任何 cron job。** |
