FROM python:3.11-slim

ARG PRODUCT
ENV PRODUCT_NAME=${PRODUCT}

# Tesseract OCR for page orientation detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

# Shared core
COPY core/ ./core/
COPY products/__init__.py ./products/__init__.py

# Product-specific code only — image contains exactly one product
COPY products/${PRODUCT}/ ./products/${PRODUCT}/

ENV PYTHONPATH="/app:${PYTHONPATH}"

# ACA / Render sets $PORT, default locally is 8000
CMD ["bash", "-lc", "uvicorn core.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
