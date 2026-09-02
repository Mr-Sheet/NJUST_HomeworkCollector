FROM ccr.ccs.tencentyun.com/library/python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# 用腾讯云 PyPI 镜像，国内构建更快
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.cloud.tencent.com/pypi/simple \
    -r requirements.txt

# 只复制运行必需的文件
COPY app.py ./app.py
COPY templates/ ./templates/
COPY static/ ./static/

# 云托管要求监听 PORT 环境变量（默认 80）
EXPOSE 80
CMD exec gunicorn --bind "0.0.0.0:${PORT:-80}" \
    --workers 1 --threads 8 --timeout 180 \
    --access-logfile - app:app
