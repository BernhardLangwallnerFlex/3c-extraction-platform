FROM python:3.11-slim

WORKDIR /app

COPY requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

COPY . .

ENV PYTHONPATH="/app:${PYTHONPATH}"

# ACA / Render sets $PORT, default locally is 8000
CMD ["bash", "-lc", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]