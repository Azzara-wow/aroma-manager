"""Конфигурация модуля: хосты, токены, тестовые ориентиры.

Токен НИКОГДА не хранится в коде для боевого окружения — только через переменную
окружения YANDEX_DELIVERY_TOKEN (или файл .env, который в git не попадает).
Для тестового окружения используется ПУБЛИЧНЫЙ токен из документации Яндекса.
"""
import os

# --- Хосты API (раздел документации «Как получить доступ к API») ---
TEST_BASE_URL = "https://b2b.taxi.tst.yandex.net"
PROD_BASE_URL = "https://b2b-authproxy.taxi.yandex.net"

# Публичный тестовый Bearer-токен из документации. Это НЕ секрет: он общий,
# опубликован в доке и действует только на тестовом хосте (и только по Москве).
TEST_TOKEN = "y2_AgAAAAD04omrAAAPeAAAAAACRpC94Qk6Z5rUTgOcTgYFECJllXYKFx8"

# --- Тестовые ориентиры из документации (тест-среда работает только по Москве) ---
TEST_SOURCE_STATION_ID = "fbed3aa1-2cc6-4370-ab4d-59c5cc9bb924"  # тестовый склад, точка А (source)
TEST_PVZ_FROM_ID = "e1139f6d-e34f-47a9-a55f-31f032a861a6"        # Москва, Ленинградский пр-т 27
TEST_PVZ_TO_ID = "01946f4f013c7337874ec2fb848a58a4"              # Москва, Ленинградский пр-т 37к9
MOSCOW_GEO_ID = 213

DEFAULT_TIMEOUT = 30


def _load_dotenv(path=".env"):
    """Минимальный загрузчик .env без внешних зависимостей: строки KEY=VALUE.
    Уже заданные переменные окружения имеют приоритет (setdefault не перезапишет)."""
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


def resolve(env=None, token=None):
    """Возвращает (base_url, env, token) для выбранного окружения.

    env:   'test' | 'prod'. По умолчанию — из YANDEX_DELIVERY_ENV, иначе 'test'.
    token: явный токен; иначе из YANDEX_DELIVERY_TOKEN; для теста — публичный TEST_TOKEN.
    """
    _load_dotenv()
    env = (env or os.environ.get("YANDEX_DELIVERY_ENV", "test")).lower()
    if env not in ("test", "prod"):
        raise ValueError("env должен быть 'test' или 'prod', получено: %r" % env)

    base_url = TEST_BASE_URL if env == "test" else PROD_BASE_URL

    if token is None:
        token = os.environ.get("YANDEX_DELIVERY_TOKEN")
    if token is None:
        if env == "test":
            token = TEST_TOKEN
        else:
            raise RuntimeError(
                "Не задан боевой токен. Впиши YANDEX_DELIVERY_TOKEN в файл .env "
                "или в переменную окружения."
            )
    return base_url, env, token
