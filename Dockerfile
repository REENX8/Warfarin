FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cacheable layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .
COPY templates/ templates/

# Create data directory for SQLite
RUN mkdir -p /data

ENV DB_PATH=/data/medtrack.db

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
