"""Живая калибровка двухшага на ТЕСТОВОМ хосте: offers/create → offers/confirm →
request/info → generate-labels. Печатает всё подробно, останавливается на первой ошибке.

Запуск (из aroma-manager):
    ./.venv/Scripts/python.exe scripts/yd_twostep.py
"""
import os
import sys
import json
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from yandex_delivery import YandexDeliveryClient, config, parcel as p
from yandex_delivery.errors import ApiError


def dump(title, obj):
    print(f"\n{title}")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:2500])
    else:
        print(obj)


def main():
    c = YandexDeliveryClient(env="test")
    calc = p.calc([p.ParcelLine("Тест-аромат", 10, 2, 50000)], barcode="LUZI-TEST-1")
    print("Вес:", calc.weight_g, "г | коробка:", calc.box.code)

    payload = {
        "info": {"operator_request_id": "luzi-" + uuid.uuid4().hex[:12]},
        "source": {"platform_station": {"platform_id": config.TEST_PVZ_FROM_ID}},
        "destination": {
            "type": "platform_station",
            "platform_station": {"platform_id": config.TEST_PVZ_TO_ID},
        },
        "items": [i.to_dict() for i in calc.items],
        "places": [calc.place.to_dict()],
        "billing_info": {"payment_method": "already_paid", "delivery_cost": 0},
        "recipient_info": {
            "first_name": "Тест",
            "last_name": "Получатель",
            "phone": "+79991234567",
        },
        "last_mile_policy": "self_pickup",
    }
    dump(">>> offers/create payload:", payload)

    # --- ШАГ 1: offers/create ---
    try:
        offers_resp = c.create_offers(payload)
    except ApiError as e:
        print(f"\n[offers/create] ApiError {e.status_code}: code={e.code} message={e.message}")
        dump("payload ошибки:", e.payload)
        return
    dump("<<< offers/create OK:", offers_resp)

    offers = offers_resp.get("offers") or offers_resp.get("variants") or []
    if not offers:
        print("\nНет вариантов доставки в ответе — печатаю сырой ответ выше, дальше не идём.")
        return
    offer = offers[0]
    offer_id = offer.get("offer_id") or offer.get("id") or offer.get("token")
    print("\nВыбран offer_id:", offer_id)

    # --- ШАГ 2: offers/confirm ---
    try:
        confirm_resp = c.confirm_offer(offer_id)
    except ApiError as e:
        print(f"\n[offers/confirm] ApiError {e.status_code}: code={e.code} message={e.message}")
        dump("payload ошибки:", e.payload)
        return
    dump("<<< offers/confirm OK:", confirm_resp)

    request_id = confirm_resp.get("request_id") or confirm_resp.get("id")
    print("\nrequest_id:", request_id)
    if not request_id:
        return

    # --- request/info (сырой, чтобы увидеть реальную структуру) ---
    try:
        raw_info = c.get_request_info(request_id, as_model=False)
        dump("<<< request/info RAW:", raw_info)
    except ApiError as e:
        print(f"\n[request/info] ApiError {e.status_code}: {e.message}")

    # --- generate-labels ---
    try:
        pdf = c.generate_labels([request_id])
        out = os.path.join(os.path.dirname(__file__), "..", "labels_test.pdf")
        with open(out, "wb") as f:
            f.write(pdf)
        print(f"\n<<< generate-labels OK: {len(pdf)} байт → {os.path.abspath(out)}")
    except ApiError as e:
        print(f"\n[generate-labels] ApiError {e.status_code}: code={e.code} message={e.message}")
        dump("payload ошибки:", e.payload)


if __name__ == "__main__":
    main()
