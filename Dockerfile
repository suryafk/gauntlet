FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Cloud Run sets PORT; SQLite lives on the container's ephemeral disk.
CMD exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080}
