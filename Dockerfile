# 使用 Python 作為基礎映像檔 (因為主程式是 Streamlit)
FROM python:3.9-slim

# 安裝 Ruby 和編譯工具 (AnyStyle 需要)
RUN apt-get update && apt-get install -y \
    ruby-full \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安裝 AnyStyle Gem
RUN gem install anystyle-cli

# 設定工作目錄
WORKDIR /app

# 複製 Python 依賴檔並安裝
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案所有檔案
COPY . .

# 確保 modules 資料夾存在 (根據您的錯誤訊息)
# 如果 modules 是分開的，請確保它被 COPY 指令包含進去

# 開放 Streamlit 埠口
EXPOSE 8501

# 啟動 Streamlit
ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]