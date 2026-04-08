FROM python:3.11-slim

# Tesseract OCR for page orientation detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

COPY . .

ENV PYTHONPATH="/app:${PYTHONPATH}"

# ACA / Render sets $PORT, default locally is 8000
CMD ["bash", "-lc", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]