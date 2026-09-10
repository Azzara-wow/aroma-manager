"""Модуль интеграции с API Яндекс Доставки (профиль продавца, доставка «на другой день»).

Самостоятельный блок: ничего не знает про data.db дашборда и про гуглшиты.
Принимает простые данные, ходит в API, отдаёт dataclass-модели.

Пример:
    from yandex_delivery import YandexDeliveryClient
    c = YandexDeliveryClient(env="test")           # тестовый хост + публичный тест-токен
    points = c.list_pickup_points(geo_id=213)      # ПВЗ по Москве
"""
from .client import YandexDeliveryClient
from .errors import YandexDeliveryError, ApiError
from .models import PickupPoint, Recipient, Item, Place, Dimensions, RequestInfo
from . import config

__all__ = [
    "YandexDeliveryClient",
    "YandexDeliveryError",
    "ApiError",
    "PickupPoint",
    "Recipient",
    "Item",
    "Place",
    "Dimensions",
    "RequestInfo",
    "config",
]
