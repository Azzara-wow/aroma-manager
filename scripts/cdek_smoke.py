"""Смоук-тест cdek_delivery. Нужны доступы в окружении/.env:
    CDEK_ENV=test
    CDEK_ACCOUNT=<Account>
    CDEK_SECURE_PASSWORD=<Secure password>
Запуск: ./.venv/Scripts/python.exe scripts/cdek_smoke.py [Город]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from cdek_delivery import CdekClient  # noqa: E402


def main():
    city = sys.argv[1] if len(sys.argv) > 1 else "Красноярск"
    c = CdekClient()
    print("Хост:", c.base_url, "| окружение:", c.env)

    print("\n[1] авторизация…")
    c._auth()
    print("  токен получен ✓")

    print(f"\n[2] suggest/cities «{city}»")
    cities = c.suggest_cities(city)
    print("  найдено:", len(cities))
    for x in cities[:3]:
        print(f"   code={x.code}  {x.full_name}")
    if not cities:
        print("  город не найден — стоп"); return
    code = cities[0].code

    print(f"\n[3] deliverypoints (city_code={code}, выдача)")
    pts = c.list_pickup_points(city_code=code, is_handout=True, size=200)
    print("  пунктов выдачи:", len(pts))
    for p in pts[:5]:
        print(f"   {p.code}  {p.type:8}  {p.address_full}")

    print(f"\n[4] deliverypoints (city_code={code}, приём/отправление)")
    drop = c.list_pickup_points(city_code=code, is_reception=True, size=200)
    print("  точек приёма:", len(drop))

    print("\nOK")


if __name__ == "__main__":
    main()
