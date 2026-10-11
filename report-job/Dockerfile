# 壓測報表 Job（Cloud Run Jobs）
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY aggregate.py annotate.py render.py sources.py main.py ./
RUN useradd -u 10001 -m report
USER report
ENTRYPOINT ["python", "main.py"]
