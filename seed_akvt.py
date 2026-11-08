"""Совместимый вход в изолированный DEMO-seed без рабочих настроек."""

from seed_demo import main

if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
