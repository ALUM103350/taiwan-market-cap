# 部署到 Vercel + Supabase

## 架構說明
```
GitHub Actions（每天 5:30PM）→ 抓取市值資料 → Supabase PostgreSQL
Vercel（靜態 + Serverless）   → 讀取 Supabase → 瀏覽器
```

---

## 步驟一：建立 Supabase 資料庫

1. 前往 https://supabase.com → **Start your project**（免費，不需信用卡）
2. 建立新 Project（選 Region: **Northeast Asia (Tokyo)** 最近）
3. 記下 **Database Password**
4. 進入 Project → **Settings → Database → Connection string → URI**
5. 複製連線字串，格式如下（把 `[YOUR-PASSWORD]` 換成你的密碼）：
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.xxxx.supabase.co:5432/postgres
   ```

---

## 步驟二：把本地資料搬到 Supabase

```powershell
cd e:\python練習\taiwan-market-cap

# 設定環境變數（把連線字串貼進去）
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@db.xxxx.supabase.co:5432/postgres"

# 執行移轉（約 1-2 分鐘）
python migrate_to_supabase.py
```

---

## 步驟三：上傳到 GitHub

1. 前往 https://github.com/new 建立新 Repository（可設 Private）
2. 在本機執行：

```powershell
cd e:\python練習\taiwan-market-cap

git init
git add .
git commit -m "init: Taiwan market cap tracker"
git branch -M main
git remote add origin https://github.com/你的帳號/倉庫名稱.git
git push -u origin main
```

3. 在 GitHub Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DATABASE_URL`
   - Value: 你的 Supabase 連線字串

---

## 步驟四：部署到 Vercel

1. 前往 https://vercel.com → **Add New Project**
2. 選 **Import Git Repository** → 選你剛建立的 GitHub Repo
3. **Framework Preset** 選 `Other`
4. 展開 **Environment Variables**，新增：
   - Key: `DATABASE_URL`
   - Value: 你的 Supabase 連線字串
5. 點 **Deploy**

部署完成後，Vercel 會給你一個網址（如 `your-project.vercel.app`）

---

## 每日自動更新

GitHub Actions 已設定在每週一到週五 **下午 5:30（台灣時間）** 自動抓取當日市值資料存入 Supabase。

如需手動觸發：GitHub Repo → **Actions → Daily Market Cap Update → Run workflow**

---

## 本地開發（繼續用 SQLite）

```powershell
cd e:\python練習\taiwan-market-cap
python app.py          # SQLite 模式，不需 DATABASE_URL
```

---

## 檔案結構

```
taiwan-market-cap/
├── api/index.py               # Vercel serverless 入口
├── app.py                     # Flask 路由
├── database.py                # SQLite（本地開發）
├── database_pg.py             # PostgreSQL（Vercel + Supabase）
├── fetcher.py                 # yfinance 資料抓取
├── daily_fetch.py             # GitHub Actions 執行腳本
├── migrate_to_supabase.py     # 一次性移轉工具
├── templates/index.html       # 前端頁面
├── vercel.json                # Vercel 設定
└── .github/workflows/
    └── daily_update.yml       # 每日自動更新
```
