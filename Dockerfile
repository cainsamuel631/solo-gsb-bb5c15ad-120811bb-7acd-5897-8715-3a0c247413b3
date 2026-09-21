FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/review.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 数据库持久化目录（容器启动时自动初始化）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动：uvicorn 的 startup 事件执行 init_db() 自动建表
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
