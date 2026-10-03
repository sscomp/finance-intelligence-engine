# ADR-C10: A3 Non-Production Boundary（A3 僅為開發機）

Status: PROPOSED — 2026-10-03（Phase 6.2 Architecture Review 待確認）

## Context

- Omarchy-A3 是今日唯一能跑完整營運流程的主機（wrappers + Hermes + 本機 DB）。Phase 6.1 已把軟體基線做成任意路徑可移植（clean-env 安裝、任意 checkout 煙測 PASS），Phase 6.2 的雲端煙霧（`git archive` + `FIE_*`）進一步證明**無 AEE、無特定主機佈局**即可以跑（Phase 6.1 報告 §16）。
- 生產相依於 A3 的風險：筆電/工作站斷電、關機、搬遷即停擺；無 HA、無受管備份。
- AEE（`/home/sscomp/aee-runtime-bridge` 等 3 路徑）為 DO-NOT-TOUCH 邊界，FIE 6.2 依工單 §4 **不得**依賴 AEE。

## Decision

1. **A3 角色永久定為 development workstation**：原始碼開發、本地 SQLite 測試、一次性驗證與 PoC。**不是**生產執行環境、不是 scheduler host、不是資料主檔所在地。
2. **"No Machine to Maintain" gate（工單 §24）作為所有雲端架構提案的硬性驗收**：A3 關機 7 天，以下必須全部成立——排程 job 照跑、資料庫可用、ChatGPT-facing API 可用、歷史情報可用。任何依賴 A3 的提案直接不合格（見報告 §22 驗證）。
3. **AEE 與 FIE 雲端化的關係 = 零**：FIE 6.2/6.3+ 不得 import、呼叫、設定或排程任何 AEE 元件；Hermes 排程器（恰與 AEE 邊界相鄰的外部系統）僅在遷移期作為平行觀察，不進新架構依賴（ADR-C06 Alternatives A 已拒絕）。
4. **A3 上保留的實用功能**：production `macro_history.db` 的遷移期 snapshot 來源（報告 §23）、開發用 venv、Codex Cloud gate 的本地對照組。

## Alternatives

- **A. A3 作為 hybrid：本機 scheduler + 雲端 API**：仍受關機影響、違反 gate → 拒絕為生產。
- **B. 將 A3 視為 hot spare / 兜底**：需要額外同步機制且同樣單點 → 拒絕。
- **C. 完全拋棄 A3**：不必要——開發體驗與遷移工具不需要雲端；此 ADR 只劃界不排除使用。

## Consequences

- 6.4（Cloud Runtime）的驗收必須實際測「A3 不線上時的全流程」——建議以「關掉 A3 上的任何 wrapper 執行」作為 6.4 gate 的一部分。
- Hermes cron jobs 在雲端接管後必須**明確停用**（避免雙派送）——遷移計畫（報告 §23）的 cutover 步驟，屆時需要 AEE/Hermes 邊界的明確授權討論。

## Open Questions

- Hermes 是 AEE 生態的一部分還是獨立工具——其停用/遷移授權範圍需在 6.4 工單明確（本階段不動）。