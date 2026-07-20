# 畅捷通 OpenAPI → MCP 转换服务
# 单容器部署：FastAPI + Jinja2 + HTMX + SQLite

FROM python:3.13-slim

# 不写 .pyc、日志实时输出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝应用代码
COPY app ./app

# SQLite 数据目录（挂载卷持久化）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# 生产用多 worker 需注意 SQLite 写并发；轻量单机默认单 worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
