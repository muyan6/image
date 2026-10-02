# 废片新生所 - 微信云托管 Dockerfile
FROM python:3.10-slim

# 设置时区与环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=80 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data \
    UPLOAD_DIR=/app/uploads

WORKDIR /app

# 安装 OpenCV 和图像处理必需的系统库
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制后端代码
COPY backend/*.py backend/*.html backend/logo.jpg ./
COPY backend/static/web ./static/web
COPY backend/tools/build_web_assets.py ./tools/build_web_assets.py
RUN python tools/build_web_assets.py

VOLUME ["/app/data", "/app/uploads"]

# 暴露端口 (微信云托管默认 80)
EXPOSE 80

# 启动 FastAPI 服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
