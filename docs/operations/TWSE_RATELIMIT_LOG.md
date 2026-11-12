# TWSE / TPEx 資料抓取限流與反爬攔截治理紀錄

> **目的：** 收錄 T2 策略報告與 FIE 的限流觀察、操作規格及待辦，供 fetcher 修改前引用。
> **維護：** T2 agent（Hermes）。後續限流事件與驗證結果一律 **append 到 §4**；原始事件有待修正時另增補充，不覆寫歷史。
> **適用邊界：** 本檔是治理規格，不代表 FIE 已實作共用攔截偵測、路徑冷卻或 MI_INDEX adapter。實作差距見 §3。
> **每輪必檢：** §2.4 是可隨 repo 使用的 SOP 摘要；本機來源為 `strategy_reports/PIPELINE_SOP.md` §8 項 15（repo 外，非 clone 前提）。

## 1. 攔截行為 X 光（2026-11-11 本機觀察）

### 1.1 觀察與推論的邊界

| 觀察 | 判讀與限制 |
|---|---|
| pick-list 分析對 `rwd/zh/exchangeReport/STOCK_DAY` 逐檔多次呼叫，另有晨間排程呼叫同一路徑；原始事件記錄約 31 請求 × 2 輪 | 高頻逐檔呼叫與攔截同時出現；精確觸發門檻未量測 |
| 同一觀察窗內 STOCK_DAY 被擋，T86／FMTQIK／MI_INDEX 曾可回 JSON | 符合路徑與客戶端相關限制的推論，當時不足以判定全域 IP ban；不代表其他路徑永久免疫 |
| 間隔 20–30 秒單發仍被擋 | 已受限時短暫 sleep 不足以恢復；累積計數是推論，非官方公布機制 |
| 原始事件稱「約 30+ 分鐘自然解除」，但紀錄也有 13:30 前後鎖、14:0x 仍鎖 | 僅能作為冷卻下限參考；無精確解除時間或 SLA，追加說明見 §4 |
| 同日收盤報告記載 MI_INDEX 安全頁，且本機留有安全頁 body | MI_INDEX 也可能被攔截；合併請求降低請求數，不保證可用 |

### 1.2 攔截外觀與判別

- 本機紀錄曾見 HTTP **200 或 307** 搭配 HTML 安全頁；不可只以狀態碼判成功或判限流。
- 安全頁特徵串：`安全性考量，您所執行的頁面無法呈現`、`FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED`。中文標點可能不同，應比對核心字串。
- 本機觀察有 HiNetCDN／`X-Cache: RULE` 及 redirect 循環，推測涉及 CDN/WAF；未取得官方規則佐證。
- urllib 與 curl 在當次條件下都受攔截，更換工具或 User-Agent 未解決當次問題；不據此推論所有攔截皆與 UA 無關。
- **307 單獨不等於限流**。無安全頁特徵的非 JSON／redirect 應記為來源或格式異常，不視為行情、不盲目重試；安全頁則依 §2.2 停止。

### 1.3 路徑比較（相對觀察，非官方安全速率）

| 路徑 | 用途 | 當次觀察／治理方向 |
|---|---|---|
| `exchangeReport/STOCK_DAY` | 個股月 K，每檔一請求 | 本次主要受限路徑；優先重用已落地歷史資料 |
| `fund/BFI4U4`／BFI4ALL 舊路徑 | ETF 相關資料 | 仍待確認，不能宣稱低風險 |
| `fund/T86` | 全市場法人日報 | 單日全市場合併請求，曾可用；歷史多日仍累積請求 |
| `afterTrading/FMTQIK` | 大盤日資料 | 曾可用，仍需偵測安全頁與驗證欄位 |
| `afterTrading/MI_INDEX?type=ALL` | 全上市單日 O/H/L/C | 批量單日行情優先路徑；已驗證覆蓋 22/22，亦有受攔截紀錄 |
| TPEx 官網彙總／OpenAPI | 上櫃資料 | 官網彙總有使用紀錄；OpenAPI 尚待正式驗證，不列為已可用備援 |

## 2. 已驗證解法與操作規格

### 2.1 合併請求優先

**需要全上市單日行情時，優先 `MI_INDEX?date=YYYYMMDD&type=ALL`，避免對同日逐檔呼叫 STOCK_DAY。** MI_INDEX 單日快照不取代個股整月／長期 K 線；月 K 應重用既有快照或歷史檔，必要逐檔時遵守下方節流規則。

- 2026-11-11 離線複核本機 `mi_index_20261110.json`：回應 `date=20261110`，22/22 上市精選股有 O/H/L/C，缺檔 0。這是固定樣本驗證，不是全市場完整性保證。
- 該快照國巨代號為 `2327`、名稱為 `國巨*`（**半形星號**）。先前記為「＊」的說明於此校正；清理同時支援 `*` 與 `＊`，匹配一律使用代號。
- 快照 SHA-256：`8dceb61fa70a4c40f9b8a0ce88e096c6d37ae6a97c621f5ce3fd028d1ccf0d89`。原始快照留本機，不隨 repo 發布；本次未重打外部 API。
- `tables[]` 依**欄位名稱**選表及取值，不硬編欄序。Python 內建 `str.strip()` 只去首尾空白，不能移除 HTML、逗號或星號。

下列為離線解析模板，**尚未接入產線 fetcher**；呼叫端需先完成 §2.2 的安全頁與狀態檢查，並另驗證日期、覆蓋率及 OHLC 合理性。

```python
import re
from html import unescape

# 單日全上市請求 URL（本模板不發送網路請求）
url_template = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALL&response=json"


def clean_text(value):
    return unescape(re.sub(r"<[^>]+>", "", str(value))).strip()


def parse_mi_index(payload):
    required = ("證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價")
    tables = [t for t in payload.get("tables", [])
              if set(required).issubset(t.get("fields", []))]
    if payload.get("stat") != "OK" or len(tables) != 1:
        raise ValueError("MI_INDEX status/schema invalid")
    table = tables[0]
    index = {name: table["fields"].index(name) for name in required}
    quotes = {}
    for row in table["data"]:
        code = clean_text(row[index["證券代號"]])
        if code in quotes:
            raise ValueError("duplicate security code")
        raw_name = clean_text(row[index["證券名稱"]])
        quote = {"name_raw": raw_name,
                 "name": raw_name.replace("*", "").replace("＊", "").strip()}
        for key in required[2:]:
            value = clean_text(row[index[key]]).replace(",", "")
            quote[key] = None if value in {"", "--", "---"} else float(value)
        quotes[code] = quote
    return quotes
```

- **上櫃檔：** 本機有 yfinance `.TWO` 使用紀錄，但非官方定稿替代保證；保留來源、日期、調整口徑及缺值標記。TPEx OpenAPI 尚待驗證，不因寫入規格便視為已可用。
- **必要逐檔月 K：** 沿用本機 SOP 的「每 10 檔批次間至少 5 秒、單 script 約 20 檔上限」，這是暫行操作上限，**不是安全速率保證，也不授權連發 10 檔**。批次內仍需節流；觸發攔截立刻停止，不以跨 script 分割規避計數。跨 script／跨排程重用同日 JSON，避免重抓。

### 2.2 遇鎖標準處置（fetcher 待實作）

1. 命中安全頁特徵串 → **停止該路徑本輪剩餘請求**，不得立刻重試同一 URL 或僅換參數連打。
2. 記錄路徑、來源日期、觀察時間與時區、狀態碼／body 類型、冷卻至何時、受影響資料及替代來源；日誌不得帶憑證。
3. 至少冷卻 **30 分鐘**後才做受控單次探測；仍被擋則延長退避、追加事件，不把「30 分鐘」當作已解鎖。
4. 有可用合併路徑或其他來源時先驗證其回應與資料契約；前日定稿只能標成前日／過期資料，缺值應明示 `DATA_UNAVAILABLE`，不能冒充當日行情。
5. 向營運告警並 append §4。本機報告缺檔 watchdog 僅是交付層保護，不能代替 fetcher 限流告警。

### 2.3 非解方與營運裁定

- **VPN：不啟用。** 2026-11-11 操作者裁定維持現有連線，優先降低請求數、合併及冷卻；當時觀察不足以支持全域 IP ban。VPN、換 IP 或多機分流不能取代治理規則。
- 資料中心 IP 是否受到更嚴規則**未驗證**；不將推測寫成 TWSE 官方政策。若所有路徑持續跨日失敗，先重新診斷與記錄，再由操作者評估連線方案。
- 更換 UA、工具或連續重試未解決當次事件；禁止以重試風暴處理安全頁。
- **Tailscale exit node 備援（已建置並實測，預設不啟用）：** 本機事實基準為 **Abacus 雲端主機（境外資料中心 IP）**，非操作者家用網路；舊敘述「家用寬頻連線」以此更正。備援出口＝操作者 tailnet 內一台台灣住宅寬頻節點（HiNet 住宅位階）：節點端 `tailscale set --advertise-exit-node`＋開 IP forwarding＋管理台 approve；本機端 `tailscale set --exit-node=<節點tailnet IP> --exit-node-allow-lan-access` 即可切換（userspace wireguard，免 sudo）。2026-11-11 夜間同刻對照：legacy `exchangeReport/STOCK_DAY` 住宅出口 **200＋完整 JSON**、雲端出口 307 安全頁——鎖按來源 IP 計，切換出口立即解鎖。**可用性邊界：** 住宅節點需開機才有備援；本次流量經遠端 DERP 中繼（未建 direct），MI_INDEX 5.69MB 實測約 17.5s（同期雲端直連 0.24s）；`/rwd/zh/` 前綴路徑自住宅出口回 302→page-not-found（非安全頁），成因暫不結論。**裁定：預設不啟用（與 VPN 同一審查標準）**，遇鎖或跨日／全域事件才手動啟用；啟用後受控單發＋安全頁檢查，用畢 `tailscale set --exit-node=` 還原並驗證出口。節點位址、IP 與中繼細節屬私有拓撲，僅存本機 SOP 與營運記憶，不入本 repo。**2026-11-12 邊界更新：** 實測夜後本機曾遺失 IPv4 default route（與 exit node 切換／還原時序相關、成因未逐項還原；LLM／GitHub 連線同時中斷，後依開機 vm_gw 參數補回並驗證恢復）。據此裁定升級：**不在共用產線主機（與報告／LLM 同機）直接切換全機出口**；需對照外部出口時先另建程序級代理或隔離環境，未驗證前不套用；只清空 exit node 不等於路由已恢復，復原須比對開機 gateway 與 default route。

### 2.4 每輪必檢（本機 SOP §8 項 15 的 repo 摘要）

> **TWSE 限流：** 記取路徑／客戶端相關的累積呼叫風險；同窗其他路徑曾可用不代表永久免疫，30+ 分鐘只是冷卻參考。批量單日上市 O/H/L/C 優先 MI_INDEX type=ALL、按欄名與代號取值（名稱星號另清理）；必要逐檔依「≥5s／10 檔批次、約 20 檔／script」暫行上限及批次內節流、快取重用。遇「安全性考量，您所執行的頁面無法呈現」或英文安全頁即停該路徑，至少 30 分鐘後受控單次探測；307 不單獨等於限流，禁止立即重試。VPN 維持不啟用；exit node 備援已建置且通聯可行，惟 2026-11-12 切換曾致本機 default route 遺失，裁定不在共用產線主機直接切全機出口、程序級代理未驗證前不套用（§2.3）；TPEx OpenAPI 待驗證。詳見 §2.1–§2.3。

## 3. 待辦（FIE 精進規格）

| 項目 | 程式現況／驗收目標 | 狀態 |
|---|---|---|
| 共用 TWSEFetcher：合併優先、路徑冷卻、退避及安全頁偵測 | **已實作（2026-11-12）：`twse_fetcher.py`**——安全頁核心字串偵測（§1.2）、路徑級跨進程冷卻（flock JSON state，預設 1800s、到期受控單次探測、失敗探測不延長冷卻）、per-path 節流（0.3s 下限）；網路／時鐘／sleep 全可注入，**測試以本機假端點驗證（tests/test_twse_fetcher.py 11/11，零真網路）**。`institutional.py` 與 `phase3/backfill.py` 的 T86 逐日迴圈已接入：遇 `FetchCooldownActive` 即停本輪剩餘日期（§2.2）；套件基線比對改動前後失敗清單相同（139=139，零新增失敗；既有失敗為 PG 依賴 baseline） | **已落地** |
| 整合 T2 ingestion | `instances/t2/src/ingestion.py` 行情已用 `STOCK_DAY_ALL` 全市場路徑，非逐檔 STOCK_DAY；T86 逐日迴圈（rwd 前綴＋自有 reliability 契約）改走 twse_fetcher 為**後續工單**—需先驗證其 fetch() 快取與冷卻契約相容性，不躁進。保留既有日期／欄位／覆蓋率契約 | 待實作（後續工單） |
| repo 外策略報告腳本 | 本機 `strategy_reports/scripts/fetch_close_20261111.py` 仍含逐檔 STOCK_DAY 與一般例外重試；依 §2 分別規劃單日行情合併、歷史快取及遇鎖中止（可引用 `twse_fetcher.fetch_json` 契約） | 待處理，非本次已部署能力 |
| TPEx OpenAPI 驗證 | 先確認官方端點、回應 schema、日期、單位、覆蓋率、timeout／redirect 行為，再新增 adapter 與離線測試 | 待驗證 |
| fetcher 級告警 | 安全頁與一般來源異常分開記錄；發生時即記 pipeline 事件並通知營運，不依賴報告缺檔才告警 | 待實作；現有 watchdog 僅部分覆蓋 |
| 月回顧 | 彙總事件與冷卻實測，再調整暫行參數；未設自動排程 | 人工維護 |

## 4. 事件簿（append-only）

原始事件保留原文；後續補充以新列註明，推論或解除時間的修正以較新紀錄為準。

| 日期 | 事件 | 處置 | 解除 |
|---|---|---|---|
| **2026-11-11** | pick-list 30 檔分析，STOCK_DAY 逐檔 ~31 請求 ×2 輪 → STOCK_DAY 路徑鎖（307→安全頁 HTML）；同窗 T86/FMTQIK/MI_INDEX 正常 | 改用已落地的 MI_INDEX type=ALL 快照（22/22）＋yfinance（30/30 含 TPEx .TWO）；VPN 討論→裁定不動用 | STOCK_DAY 鎖約 30+ 分鐘自然解除；MI_INDEX 路徑全程可用 |
| **2026-11-11（合併前複核補充）** | 原始事件的「MI_INDEX 全程可用／STOCK_DAY 約 30+ 分鐘解除」只涵蓋先前紀錄；同日收盤報告記載 MI_INDEX 安全頁，`mi_test.json` 本機 body 亦命中中英文安全特徵，兩者無法證明精確解鎖時間 | 保留原事件；將 §1–§2 改為觀察與操作下限，307 不單獨判限流、禁止立即重試；新增 repo 每輪必檢摘要 | STOCK_DAY／MI_INDEX 精確解除時間待補證，不保證 30 分鐘恢復 |
| **2026-11-11（離線覆蓋驗證）** | 以 20261110 MI_INDEX 留存快照重驗 22/22 上市標的 O/H/L/C；2327 代號無星號、名稱為半形 `國巨*` | 按欄名解析；範例保留 name_raw 並清理兩種星號；快照 hash 見 §2.1 | 離線驗證，未新增網路請求；TPEx 不在此覆蓋結果內 |
| **2026-11-11（夜間受控單發探測）** | 22:1x／22:22 台灣時間兩次單發 `STOCK_DAY?date=20261110&stockNo=2330` 皆 307→安全頁；同窗 `MI_INDEX?date=20261110&type=ALL` 200 JSON（total 0.63s）。路徑級鎖模式夜間復確認；MI_INDEX 收盤後仍可用。日間鎖是連續存在或被夜間探測重新觸發，無法判別 | 單發後即止，當日資料已有 MI_INDEX 快照不重抓；不連發測試，解除時間待次一營業日上午驗證 | 本輪未解除（夜間仍鎖），「30+ 分鐘」維持下限解讀；另有 tailscale exit node 備援方案提報操作者待裁定（見 §2.3 追加） |
| **2026-11-11（夜間 exit node 實測）** | 建置 tailnet exit node（操作者住宅寬頻出口）後受控單發：`exchangeReport/STOCK_DAY`（legacy 無 `/rwd/zh/` 前綴）自住宅出口 **200＋完整 JSON**（`stat: OK`、台積電 11/10 各日成交）；同刻自雲端出口同路徑 307 安全頁——解鎖歸因**來源 IP 變更**而非時間自然解除。`/rwd/zh/` 前綴同段自住宅出口 302→page-not-found（非安全頁）。MI_INDEX 自住宅出口 200（經 DERP 中繼 5.69MB 約 17.5s；同期雲端直連同參數 200／0.24s） | 切換→單發→立即還原，全程受控單發；測後出口還原雲端並驗證，無路由殘留 | 鎖在本輪雲端出口夜間持續（跨 4 小時+）；住宅出口當下可用——備援驗證有效，裁定預設不啟用（§2.3） |
| **2026-11-12（出口事故追加）** | 實測夜後（00:30 台灣時間）本機 IPv4 default route 缺失、LLM API 與 GitHub IPv4 同時不通；時序與前一輪 exit node 切換／還原相關，未取得逐項路由變更紀錄（即時還原驗證當下曾通，路由缺失在其後浮現）。DNS 正常、IPv6 不受影響 | 依開機 vm_gw 參數補回 default route（eth0），未重啟服務、未動身分與憑證；驗證 LLM API 與 GitHub HTTPS 均 200 | 恢復；裁定升級為「不在共用產線主機直接切全機出口，程序級代理／隔離環境未驗證前不套用」；事故細節留本機文件，不隨 repo 發布 |
| **2026-11-12（T86 觸點與品質對照）** | 晨間單發觸點：`fund/T86` legacy 路徑自雲端出口 307（0.56s，307 單獨不判限流、即停未重試（§1.2））。近 4 日品質對照：FIE 產線全綠（daily/weekly/monthly artifacts 零缺值、.err 全空）；策略報告線 307 影響集中 11-11/12 凌晨鎖窗——11-11 收盤版漲跌家數 DATA_UNAVAILABLE（FMTQIK 新版無家數欄＋MI_INDEX 同窗被鎖），picklist 報告 4 筆補核聲明；11-12 開盤 2 筆為欄位不存在的合約聲明非鎖所致 | 有鎖窗的數值皆依 §2.2 如實宣告或替代源補核，無冒充；觀察後不追加探測，避免累積路徑計數 | OVH 出口鎖窗跨日持續（11-11 日間至 11-12 晨間至少一次 307 復確認）；解除時間仍未知，待 04:50/16:20 管線觀察 |
| **2026-11-12（TWSEFetcher 落地）** | §3 首項完成：`twse_fetcher.py`（安全頁偵測＋路徑冷卻＋受控探測＋跨進程 state）＋假端點測試 11/11 全綠（零真網路）；`institutional.py`／`phase3/backfill.py` T86 迴圈接入遇冷卻即停（§2.2）；全套件基線比對改動前後失敗清單相同（139=139，既有失敗為 PG 依賴 baseline、非本次引入） | fetcher 行為以 tests/test_twse_fetcher.py 鎖定；state 檔 gitignored、運行時才產生；T2 ingestion 迴圈整合列入後續工單 | 治理規格 §2.2 自此為程式契約而非文件約定；實戰遇鎖行為待下次管線事件驗證 |
| （後續事件紀錄於此） | | | |

## 5. 關聯來源與版本控管範圍

- [README](../../README.md)、[法人抓取入口](../../institutional.py)、[T2 ingestion](../../instances/t2/src/ingestion.py)、[T2 資料來源契約](../t2/DATA_SOURCES.md)。
- 本機 `strategy_reports/PIPELINE_SOP.md` §8 項 15 與 `pipeline.log`：營運原文與留痕，均在 repo 外；本檔 §2.4 收錄必要操作規則，不發布整份 SOP 或每日報告。
- 本機 `strategy_reports/official_close_flow_20261111.md`（17:08 留存版）與 `/tmp/mi_test.json`：MI_INDEX 亦曾受攔截的佐證；安全頁僅驗證外觀，不據此宣稱精確攔截或解除時間。
- Hermes `scheduled-report-pipelines` 與 Supermemory 是本機營運參照，repo 使用不依賴私人記憶條目 ID。
- 原始快照、pipeline.log、報告、憑證、私人設定及本機 systemd 部署檔未納入本次合併。

## 6. 本機與 GitHub 比對（2026-11-11）

- 比對起點：本機 `master` 與 `origin/master` 同為 `95a62ac`，ahead／behind 均為 0。
- 待收錄項目：本檔原為未追蹤檔；README 與 institutional.py 原有未提交治理新增。合併範圍限這三檔，既有執行邏輯不變。
- 校正：SOP 本機引用原漏掉 `docs/operations/`；安全頁立即重試與退避規則衝突；內建 strip 與自訂清理函式混淆；0.3s 已足／MI_INDEX 永久最低風險缺乏證據。以上於本檔、註解及本機 SOP 同步釐清。
