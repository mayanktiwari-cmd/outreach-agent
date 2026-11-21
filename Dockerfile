FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY outreach/ outreach/
COPY profile.example.yaml profile.yaml
COPY professors.example.csv professors.csv

RUN mkdir -p outbox

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV SEND_MODE=dry_run

CMD ["uvicorn", "outreach.api:app", "--host", "0.0.0.0", "--port", "8000"]
