"""Смоук-тест модуля yandex_delivery против ТЕСТОВОГО хоста Яндекса.

Проверяет живую связь и токен: определяет geo_id Москвы и тянет список ПВЗ.
Запуск (из папки aroma-manager):
    ./.venv/Scripts/python.exe scripts/yd_smoke.py
"""
import os
import sys

# чтобы работал импорт пакета при запуске файла напрямую
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# консоль Windows по умолчанию не UTF-8 → кириллица билась бы в мохито
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from yandex_delivery import YandexDeliveryClient
from yandex_delivery.errors import ApiError, YandexDeliveryError


def main():
    c = YandexDeliveryClient(env="test")
    print("Хост:       ", c.base_url)
    print("Окружение:  ", c.env)
    print("Токен:      ", c._token[:10] + "…")

    print("\n[1] location/detect 'Москва'")
    variants = c.detect_location("Москва")
    print("    варианты:", variants)
    if not variants:
        print("    ! пустой ответ — дальше не идём")
        return
    gid = variants[0]["geo_id"]
    print("    geo_id =", gid)

    print(f"\n[2] pickup-points/list (geo_id={gid})")
    points = c.list_pickup_points(geo_id=gid)
    print("    всего пунктов:", len(points))
    for p in points[:8]:
        print(f"     • {p.id}  [{p.type}]  {p.name} — {p.full_address}")

    print("\nOK — связь с тестовым API живая.")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        print(f"\nAPI вернул ошибку: {e}")
        print("payload:", e.payload)
        sys.exit(1)
    except YandexDeliveryError as e:
        print(f"\nОшибка модуля: {e}")
        sys.exit(1)
