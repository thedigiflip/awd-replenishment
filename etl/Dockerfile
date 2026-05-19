# ─── Stage 1: Builder ────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 安裝編譯依賴（DuckDB 在 arm64 有 wheel，通常不需要編譯）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ─── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# 複製已安裝的套件
COPY --from=builder /install /usr/local

# 安裝 runtime 依賴（curl 用於 healthcheck）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 複製應用程式碼
COPY . .

# 建立 DuckDB 資料目錄
RUN mkdir -p /data

EXPOSE 8000

# 使用 uvicorn 啟動，--reload 在 dev 模式很有用
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--log-level", "info"]
