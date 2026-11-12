FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8000

# gunicorn 启动；分析会话保存在进程内存中，因此固定 1 个 worker、多线程。
# 溯源为纯 CPU 轻量计算，8 线程足够演示。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "60", "server:app"]
