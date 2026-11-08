# 1. Gunakan base image Python yang ringan
FROM python:3.11-slim

# 2. Set environment variables penting untuk Python di dalam Docker
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 3. Tentukan working directory di dalam container
WORKDIR /app

# 4. Install system dependencies (diperlukan oleh Pillow dan psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    fontconfig \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# 5. Copy & install Python dependencies (dikompres tanpa cache)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 6. Copy seluruh kode aplikasi backend ke dalam container
COPY . .

# 7. Buka port 8000 untuk akses HTTP
EXPOSE 8000

# 8. Perintah untuk menjalankan Django pakai uvicorn (ASGI server) di port 8000
# Catatan: Ganti 'core' dengan nama folder project Django milikmu (folder tempat asgi.py berada)
CMD sh -c "uvicorn core.asgi:application --host 0.0.0.0 --port ${PORT:-8000}"