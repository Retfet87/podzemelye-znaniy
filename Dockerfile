FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY certificates/russian_trusted_root_ca.pem ./certificates/
COPY main.py manage.py ./
COPY scripts/ ./scripts/

USER 10001:10001
CMD ["python", "main.py"]
