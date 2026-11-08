"""Миграции лишь заранее подготовленной DEMO-базы, без чтения .env."""

from app.demo import demo_database
from app.db import migrate

if __name__ == "__main__":
    migrate(demo_database())
