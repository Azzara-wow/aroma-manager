"""Конфигурация модуля CDEK: хосты и доступы (client_id/secret = Account/Secure password).

Доступы НИКОГДА не хранятся в коде — только через окружение/.env:
  CDEK_ENV=test|prod
  CDEK_ACCOUNT=<client_id / Account>
  CDEK_SECURE_PASSWORD=<client_secret / Secure password>
"""
import os

# Хосты API v2 (из openapi: servers).
TEST_BASE_URL = "https://api.edu.cdek.ru"   # тестовая среда
PROD_BASE_URL = "https://api.cdek.ru"       # рабочая среда

DEFAULT_TIMEOUT = 30


def _load_dotenv(path=".env"):
    """Минимальный загрузчик .env без зависимостей (KEY=VALUE построчно)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def resolve(env=None, account=None, secure=None):
    """Вернуть (base_url, env, account, secure) для окружения CDEK."""
    _load_dotenv()
    env = (env or os.environ.get("CDEK_ENV", "test")).lower()
    if env not in ("test", "prod"):
        raise ValueError("CDEK_ENV должен быть 'test' или 'prod', получено: %r" % env)
    base_url = TEST_BASE_URL if env == "test" else PROD_BASE_URL

    account = account or os.environ.get("CDEK_ACCOUNT")
    secure = secure or os.environ.get("CDEK_SECURE_PASSWORD")
    if not (account and secure):
        raise RuntimeError(
            "Нет доступов CDEK. Задай CDEK_ACCOUNT и CDEK_SECURE_PASSWORD "
            "(Account / Secure password из кабинета CDEK) в .env или окружении."
        )
    return base_url, env, account, secure
