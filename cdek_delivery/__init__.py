"""Модуль интеграции с API CDEK v2 (аналог yandex_delivery).

Самостоятельный блок: ничего не знает про data.db дашборда и гуглшиты.
Доступы (Account/Secure password) — только через env/.env (CDEK_ACCOUNT/CDEK_SECURE_PASSWORD).

Пример:
    from cdek_delivery import CdekClient
    c = CdekClient(env="test")
    code = c.city_code("Красноярск")
    points = c.list_pickup_points(city_code=code, is_handout=True)
"""
from .client import CdekClient
from .errors import CdekError, CdekApiError
from .models import CdekCity, CdekPoint
from . import config

__all__ = ["CdekClient", "CdekError", "CdekApiError", "CdekCity", "CdekPoint", "config"]
