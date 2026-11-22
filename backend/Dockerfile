FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 启动时会执行 Alembic 迁移，因此迁移脚本与配置必须进镜像。
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
