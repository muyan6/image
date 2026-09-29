# 废片新生所 - 微信云托管 Dockerfile (backend 目录专版)
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=80 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data \
    UPLOAD_DIR=/app/uploads

WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY *.py *.html logo.jpg ./

VOLUME ["/app/data", "/app/uploads"]

EXPOSE 80

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
