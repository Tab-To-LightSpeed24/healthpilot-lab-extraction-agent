FROM python:3.13-slim

WORKDIR /app

# Fewer malloc arenas keeps resident memory down when many large image buffers
# are allocated/freed across threads (measured as part of the 512MB budget).
ENV MALLOC_ARENA_MAX=2

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
