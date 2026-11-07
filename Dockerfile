FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Samara

WORKDIR /app

# Установка tzdata и postgresql-client (если понадобится локальная диагностика)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# По умолчанию запускаем миграции и стартуем бота
CMD ["sh", "-c", "python manage.py migrate && python main.py"]