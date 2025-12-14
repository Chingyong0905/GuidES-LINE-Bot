# GuidES LINE Bot — Reproducibility Guide

本專題是一個 LINE Bot + Groq+ RAG 系所資訊助理。資料會先依「類型標籤」分類，建立 **4 個 FAISS 向量資料庫**（對應 4 個 mode），Bot 端依使用者選擇的 mode 載入對應 FAISS DB 進行檢索，再呼叫 LLM 生成回答。  
系統另使用 **Firebase Realtime Database（RTDB）** 暫存「同一 mode 內的短期對話記憶」，切換 mode 會清空記憶。

---

## 1) 專案資料夾結構

請將檔案放在同一個專案資料夾（root）中，結構如下：

```
submission-query-bot/
├─ app.py
├─ build_faiss_db.py
├─ uploaded_docs/                 # RAG input data 放這裡
├─ guides-linebot-firebase-adminsdk-xxx.json （Firebase service JSON：需自行到Firebase 申請下載）
└─ faiss_db/                      # 產生後會出現（四個子資料夾）
```

> 注意：本專題**不會提供** `guides-linebot-firebase-adminsdk-xxx.json`。重現者需要自行到 Firebase Console 申請並下載 service account JSON（見第 4 節）。

---

## 2) RAG input data 格式要求（最重要）

- 支援：`.txt / .pdf / .docx`
- 若為 `.txt`：**第一行必須為標籤**（用來分流到不同 mode）

可用標籤如下（四選一）：

- `類型：department_announcement`
- `類型：scholarship`
- `類型：faculty_lab`
- `類型：course_requirement`

範例 `.txt`：

```
類型：scholarship
（正文內容……）
```

---

## 3) 安裝環境與套件（使用 requirements.txt）

### Python
建議 Python 3.10+（3.11 通常也可）

### 安裝套件
在專案根目錄執行：

```bash
pip install -r requirements.txt
```
---
## 4) 取得必要金鑰（GROQ / LINE）（重現者需自行申請）

本專題不提供任何金鑰。重現者需要自行申請並取得以下環境變數：

- GROQ_API_KEY：至 Groq 申請並取得 API Key
- LINE_CHANNEL_ACCESS_TOKEN、LINE_CHANNEL_SECRET：至 LINE Developers 建立 Provider/Channel（Messaging API）後取得

---

## 5) Firebase Realtime Database 設定（短期記憶）

### 5.1 建立 RTDB
Firebase Console → **Realtime Database** → 建立資料庫。

### 5.2 Database Rules（建議）
建議使用 **Locked mode（鎖定模式）**：

```json
{
  "rules": {
    ".read": false,
    ".write": false
  }
}
```

> 本專題後端使用 Firebase Admin SDK（service account）寫入 RTDB，不受上述 rules 影響。

### 5.3 取得 service account JSON（重現者需要自行申請）
Firebase Console → Project settings → **Service accounts** → **Generate new private key**  
下載後將 JSON 放到專案根目錄（或依你 app.py 設定的路徑放置）。

### 5.4 取得 `databaseURL`
Firebase Console → Realtime Database 頁面，複製「資料庫根網址」，形式通常像：

- `https://<project>-default-rtdb.firebaseio.com`

> 請不要使用帶 `/:null` 的網址，那不是 databaseURL。

---

## 6) 產生 4 個 FAISS 向量資料庫（關鍵步驟）

1. 把你的資料放進 `uploaded_docs/`
2. 執行：

```bash
python build_faiss_db.py
```

成功後應出現：

```
faiss_db/
  faiss_db_department_announcement/
  faiss_db_scholarship/
  faiss_db_faculty_lab/
  faiss_db_course_requirement/
```

---

## 7) 設定環境變數（LINE + Groq）

### Windows PowerShell 範例
```powershell
$env:LINE_CHANNEL_ACCESS_TOKEN="你的token"
$env:LINE_CHANNEL_SECRET="你的secret"
$env:GROQ_API_KEY="你的groq_key"
$env:GROQ_MODEL="llama-3.3-70b-versatile"

# 開發模式：回答後是否自動附上選單（1=附選單, 0=不附）
$env:DEBUG_SHOW_MENU_AFTER_REPLY="1"
```

---

## 8) 啟動 Bot Server

在專案根目錄執行：

```bash
python app.py
```

本機健康檢查（若你的 `app.py` 有提供 `/`）：

- `http://127.0.0.1:5000/`

應看到 `OK`，並顯示各 mode 是否成功載入 FAISS。

---
## 9) 使用 ngrok 對外提供 webhook（必做）

由於 LINE Developers webhook 需要公開網址，重現者需使用 ngrok。

### 9.1 下載 ngrok

請至 ngrok 官網下載（Windows 版）並解壓縮取得 ngrok.exe：

- https://ngrok.com/

### 9.2 啟動 ngrok（轉發本機 5000）

在專案資料夾中執行：
```bash
ngrok http 5000
```

ngrok 會輸出一個公開網址，例如：

- https://xxxx-xxxx.ngrok-free.app

### 9.3 設定 LINE Developers Webhook URL

將 webhook URL 設定為：

- https://xxxx-xxxx.ngrok-free.app/callback

然後在 LINE Developers 後台：

1. 貼上 Webhook URL
2. 點 Verify
3. 顯示成功（success）即代表連線完成

---

## 10) LINE Bot 操作方式（重現測試流程）

1. 在 LINE 對話輸入：`選單`（或 `@機器人`）
2. 點選查詢類別（四個按鈕其一）
3. **直接輸入問題**（不需要 `@問題`）
4. 同一 mode 內連續對話：系統會使用 RTDB 暫存短期記憶，使回答更連貫
5. 切換到新 mode：舊 mode 記憶會被清空（避免跨類別污染）

---

## 11) 常見問題排除

### A) Verify 失敗 / webhook 打不到
- 確認 app.py 正在跑（5000 port）
- 確認 ngrok 仍在執行且網址未變
- 確認 webhook URL 結尾是 /callback

## B) FAISS DB 載入失敗
- 確認 faiss_db/ 底下四個資料夾存在
- 確認 app.py 內 FAISS_DIR_BY_MODE 路徑與資料夾名稱一致

## C) Firebase 寫入失敗
- 確認 databaseURL 是根網址（不要帶 /:null）
- 確認 service account JSON 路徑正確
- 確認程式只初始化 Firebase 一次（避免 default app already exists）

