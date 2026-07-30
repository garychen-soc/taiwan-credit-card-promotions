# 刷卡活動登錄雷達

整理台灣銀行官方信用卡活動，優先呈現需要登錄的時間點，並提供活動期間與登錄期間的一鍵行事曆功能。

目前資料來源：

- 星展銀行信用卡活動頁與官方網購活動明細
- 國泰世華 CUBE 活動專區與官方公開結構化資料
- 中國信託 LINE Pay 卡官方優惠一覽與六大點數生活圈
- 永豐銀行信用卡「刷卡享優惠」官方活動清單
- 上海商銀信用卡官方熱門活動清單與活動明細

## 網站功能

- 今日、明日活動登錄快速檢視
- 重點、高回饋、需登錄、即將開始、即將結束、銀行、分類等多維篩選
- 每筆活動列出銀行、期間、摘要、登錄時間與官方來源
- 活動與登錄時段可加入 Google Calendar，或下載含 10 分鐘前提醒的 `.ics`
- 高回饋定義：回饋率至少 10%，或單筆／每期最高回饋至少 NT$500
- 指定入口失效時，只在銀行官方網域尋找替代網址，並輸出警示

## 架構

```text
銀行官方頁面
  └─ 固定規則擷取器（Python 標準函式庫）
       ├─ 官方網域與重新導向檢查
       ├─ 活動、回饋、登錄時段標準化
       ├─ docs/data/promotions.json
       └─ reports/latest.json（本機更新報告，不發布）

docs/
  └─ 純靜態 HTML、CSS、JavaScript
       └─ GitHub Actions → GitHub Pages
```

正常更新不使用生成式模型：固定規則、結構化欄位與資料差異即可完成擷取、分類與發布。只有來源失效、官方版型改變或規則無法確認時，才需要人工或 AI 檢查，兼顧正確性與 token 成本。

## 本機更新與驗證

```bash
PYTHONPATH=src python3 scripts/update_data.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --check docs/assets/app.js
python3 scripts/automation_summary.py
```

`scripts/automation_summary.py` 僅讀取更新後的精簡 JSON，供排程直接產生 Slack 摘要，避免重複讀取所有原始頁面內容。

## 安全邊界

本專案只讀取公開官方資訊，不登入銀行帳戶、不替使用者執行活動登錄，也不收集卡號或個人資料。實際名額、資格與回饋仍以銀行官方公告為準。
