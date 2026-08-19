# 📈 Chive Terminal / Taiwan Stock Follow-up

本專案是一個以 **Python + GitHub Actions + GitHub Pages** 建置的台股追蹤儀表板，會自動整理與發布每日觀察資料，方便快速檢視籌碼與市場重點。

目前 repository 近期已加入與強化的內容，包含：
- 自動更新 `history.json`
- 調整部署排程（cron jobs）
- 強化券商資料抓取與解析流程
- 改善 ETF 偵測邏輯
- 補強股票 / ETF 價格擷取
- 更新前端展示頁面與 GitHub Pages 發布流程

---

## ✨ Features

- 每日自動更新台股追蹤資料
- GitHub Actions 定時執行資料建置與部署
- GitHub Pages 靜態頁面展示結果
- 券商買進資料解析與整理
- ETF / 個股辨識與過濾
- 歷史資料輸出與追蹤

---

## 🧩 Recent Updates

根據最近的專案提交，近一步更新主要包含：

### 1. 排程與部署
- 調整 deployment workflow 的 cron 設定
- 新增額外的 cron job 作為排程補強
- 持續透過 GitHub Actions 自動更新 `history.json`

### 2. 資料抓取與解析
- 重構 `fetch_and_build.py` 的編碼與解析流程
- 將券商資料抓取邏輯改為以 Regex parsing 為主
- 清理資料中的特殊空白字元問題

### 3. ETF / 股票資料處理
- 改善 ETF 偵測與過濾邏輯
- 補強股票代碼擷取規則
- 加入股票與 ETF 價格資料處理

---

## 🚀 Deployment

本專案使用 **GitHub Actions** 產生資料，並透過 **GitHub Pages** 發布。

### GitHub Pages 設定
1. 前往 repository 的 **Settings** → **Pages**
2. 將 **Build and deployment > Source** 設為 **GitHub Actions**
3. 到 **Actions** 頁面手動執行 workflow，或等待排程自動更新

### GitHub Pages
- https://whsu45.github.io/chive-terminal/

---

## 🛠 Tech Stack

- **Python**：資料抓取、清洗、轉換與建置
- **HTML**：靜態頁面展示
- **GitHub Actions**：自動化排程與部署
- **GitHub Pages**：前端靜態網站託管

---

## 📂 Project Purpose

這個專案主要用來追蹤台股相關資料與觀察指標，透過自動化流程降低手動整理成本，並讓每日市場資訊能以簡潔頁面快速查看。

若後續功能持續擴充，README 建議再補上：
- 專案目錄結構
- 主要 workflow 說明
- 資料來源
- 畫面截圖
- 本地執行方式
