# 📈 Chive Terminal / Taiwan Stock Follow-up

本專案是一個以 **Python + GitHub Actions + GitHub Pages** 建置的台股追蹤與靜態發布專案，會自動抓取市場資料、整理歷史紀錄，並將結果發布到 GitHub Pages，方便快速查看每日觀察重點。

目前專案重點包含：
- 自動更新 `data/history.json`
- 使用 GitHub Actions 定時執行資料建置與部署
- 強化券商資料抓取與 Regex parsing 流程
- 改善 ETF 偵測與股票代碼擷取邏輯
- 補強股票 / ETF 價格資料處理
- 使用 GitHub Pages 發布前端頁面

---

## ✨ Features

- 每日自動更新台股追蹤資料
- GitHub Actions 定時執行資料抓取、建置與部署
- GitHub Pages 靜態頁面展示結果
- 券商買進資料解析與整理
- ETF / 個股辨識與過濾
- 歷史資料輸出與追蹤

---

## 🧩 Recent Updates

根據最近的專案提交，近期更新主要包含：

### 1. 排程與部署
- 調整 deployment workflow 的 cron 設定
- 新增多組備援 cron job 強化更新穩定性
- 持續透過 GitHub Actions 自動更新 `data/history.json`

### 2. 資料抓取與解析
- 重構 `fetch_and_build.py` 的編碼與解析流程
- 將券商資料抓取邏輯改為以 Regex parsing 為主
- 清理資料中的特殊空白字元問題

### 3. ETF / 股票資料處理
- 改善 ETF 偵測與過濾邏輯
- 補強股票代碼擷取規則
- 加入股票與 ETF 價格資料處理

---

## 📂 Project Structure

```text
chive-terminal/
├── .github/
│   └── workflows/
│       └── deploy.yml          # GitHub Actions workflow：排程抓資料、建置、部署 Pages
├── data/
│   ├── history.json            # 歷史資料輸出
│   └── broker_history.json     # 券商相關歷史資料
├── Achieved/
│   └── README.md               # 舊版或備存文件
├── fetch_and_build.py          # 主要資料抓取、解析、輸出腳本
├── requirements.txt            # Python 套件需求
└── README.md                   # 專案說明文件
```

---

## ⚙️ Workflow 說明

主要 workflow 位於：
- `.github/workflows/deploy.yml`

此 workflow 會執行以下流程：
1. checkout repository
2. 設定 Python 3.10
3. 安裝 `requirements.txt` 中的依賴套件
4. 執行 `fetch_and_build.py`
5. 將更新後的 `data/history.json` commit 回 repository
6. 上傳靜態頁面內容並部署到 GitHub Pages

### 排程時間
目前 workflow 設定了多組平日排程作為更新與備援機制：
- 台灣時間 **07:30**
- 台灣時間 **08:30**
- 台灣時間 **09:30**
- 台灣時間 **11:30**
- 台灣時間 **15:00**
- 台灣時間 **20:00**

另外也支援手動觸發：
- `workflow_dispatch`

---

## 🗂 Data Source

從程式內容可知，本專案主要透過 Python 腳本抓取市場相關資料，並做後續清洗與輸出。README 目前可合理描述的資料來源包含：

- 台股相關公開市場資料
- 券商買進 / 分點觀察資料
- 股票與 ETF 代碼及價格相關資料

如果你之後希望 README 更精確，也可以再補上實際使用的資料來源網址與欄位說明。

---

## 🚀 Local Development

### 1. Clone repository
```bash
git clone https://github.com/whsu45/chive-terminal.git
cd chive-terminal
```

### 2. 建立虛擬環境（可選）
```bash
python -m venv .venv
source .venv/bin/activate
```

如果你在 Windows：
```bash
.venv\Scripts\activate
```

### 3. 安裝依賴
```bash
pip install -r requirements.txt
```

### 4. 執行資料建置
```bash
python fetch_and_build.py
```

執行後，預期會更新或產生：
- `data/history.json`
- `data/broker_history.json`
- 前端頁面所需的靜態資料內容

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
- **Requests**：HTTP 資料抓取
- **BeautifulSoup4**：HTML 解析
- **lxml**：解析輔助
- **GitHub Actions**：自動化排程與部署
- **GitHub Pages**：前端靜態網站託管

---

## 📌 Project Purpose

這個專案主要用來追蹤台股相關資料與觀察指標，透過自動化流程降低手動整理成本，並讓每日市場資訊能以簡潔頁面快速查看。

後續如果要再加強 README，建議可以再補：
- 頁面截圖
- 輸出資料範例
- 各欄位與指標定義
- 實際資料來源連結
- 系統流程圖
