# 每日焦點電子報(免費版)

RSS → Gemini 篩選(排除選舉/空泛)→ 抓全文 → Gemini 摘要+趨勢 → HTML 電子報 → Gmail 寄出 + 存檔

## 設定步驟(約 15 分鐘)

1. **建立 GitHub repo**,把這個資料夾的所有檔案(含隱藏的 `.github/`)推上去。
2. **Gemini 金鑰**:到 https://aistudio.google.com 建立 API key(免費層,不需信用卡)。
3. **Gmail 應用程式密碼**:Google 帳戶 → 安全性 → 開啟兩步驟驗證 → 搜尋「應用程式密碼」建立一組。
4. 到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**,新增:
   | 名稱 | 內容 |
   |---|---|
   | `GEMINI_API_KEY` | Gemini 金鑰 |
   | `GMAIL_USER` | 你的 Gmail 位址(寄件用) |
   | `GMAIL_APP_PASSWORD` | 第 3 步的 16 碼密碼 |
   | `MAIL_TO` | (選填)收件信箱,不填就寄給自己 |
5. 到 **Actions → Daily Newsletter → Run workflow** 手動跑一次測試。
6. 之後每天台灣時間 07:00 左右自動執行。

## 本機測試

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=你的金鑰
python newsletter.py --check-feeds   # 先確認各來源抓得到
python newsletter.py                  # 產生 docs/今天日期.html(沒設 Gmail 變數就不寄信)
```

## 調整

- 增減媒體:改 `newsletter.py` 的 `FEEDS`。
- 篩選口味:改 `stage1_pick()` 裡的「必須排除 / 優先選擇」。
- 每區則數:`PICKS_PER_REGION`;時間範圍:`HOURS`。
- 換模型:設定環境變數 `GEMINI_MODEL`。

## 歷史存檔(選用)

存檔會 commit 到 `docs/`。若要有網頁版:Settings → Pages → Branch 選 `main`、資料夾選 `/docs`。
注意:私有 repo 使用 Pages 需要付費方案;公開 repo 則存檔內容會是公開的。
不開 Pages 也可以,直接在 repo 裡點開 HTML 檔(或下載)即可。
