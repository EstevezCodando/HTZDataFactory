FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gdal-bin \
    libgdal-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
COPY frontend/ ./frontend/
COPY startup.py .

# Garante que os pacotes do src/ sejam encontrados
ENV PYTHONPATH=/app

# startup.py aplica fix PROJ antes de importar rasterio (evita conflito com PostgreSQL/PostGIS)
CMD ["python", "startup.py"]
