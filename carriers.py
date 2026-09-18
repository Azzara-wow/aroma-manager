"""Диспетчер перевозчиков для дашборда: единый интерфейс поверх yandex_delivery и
cdek_delivery. Хендлеры /dostavka ветвятся по carrier ('yandex' | 'cdek').

Механики разные, поэтому сведены к общим операциям:
  • confirm_delivery — ЗАБРОНИРОВАТЬ (Яндекс: offers/confirm; СДЭК: create_order + опрос номера)
  • cancel_delivery  — отменить (Яндекс: request/cancel; СДЭК: delete_order)
  • labels_pdf       — PDF ярлыков (Яндекс: generate-labels; СДЭК: print/barcodes)
Черновик (ШАГ ①): у Яндекса нужен offers/create (даёт offer_id+цену для confirm),
у СДЭК заказ создаётся сразу при подтверждении, поэтому «Создать» для СДЭК —
локальная пометка (без вызова API), а вся работа в confirm.
"""
import time

CDEK_TARIFF_PVZ = 136  # посылка склад-склад (ПВЗ→ПВЗ)


def normalize(carrier):
    return "cdek" if str(carrier or "").lower() == "cdek" else "yandex"


# ====================================================================
#  ПОДТВЕРЖДЕНИЕ (реальная бронь)
# ====================================================================

def confirm_delivery(carrier, row, rec, calc, opid, origin_id):
    """Забронировать доставку. Возвращает dict:
        {ok, request_id, cdek_number, tracking, price, error}."""
    if normalize(carrier) == "cdek":
        return _cdek_book(rec, calc, opid, origin_id)
    return _yandex_confirm(row)


def _yandex_confirm(row):
    from yandex_delivery import YandexDeliveryClient
    from yandex_delivery.errors import YandexDeliveryError
    try:
        c = YandexDeliveryClient()
        resp = c.confirm_offer(row["offer_id"])
        rid = resp.get("request_id", "")
        track = ""
        try:
            info = c.get_request_info(rid, as_model=False)
            track = info.get("sharing_url", "") or ""
        except Exception:
            pass
        return {"ok": True, "request_id": rid, "cdek_number": "", "tracking": track}
    except YandexDeliveryError as e:
        return {"ok": False, "error": getattr(e, "message", str(e))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _cdek_book(rec, calc, opid, origin_code):
    from cdek_delivery import CdekClient
    from cdek_delivery.errors import CdekError
    try:
        c = CdekClient()
        payload = _cdek_order_payload(rec, calc, opid, origin_code)
        resp = c.create_order(payload)
        u = (resp.get("entity") or {}).get("uuid", "")
        if not u:
            # ошибки приходят в requests[].errors
            errs = []
            for r in resp.get("requests", []):
                errs += [e.get("message", "") for e in (r.get("errors") or [])]
            return {"ok": False, "error": "; ".join(e for e in errs if e) or "СДЭК не вернул uuid"}
        # опрашиваем номер (async); если не успел — оставим пустым, добьём кнопкой «↻ ссылка»
        num = ""
        for _ in range(3):
            time.sleep(4)
            info = c.get_order(u)
            num = (info.get("entity") or {}).get("cdek_number") or ""
            if num:
                break
        return {"ok": True, "request_id": u, "cdek_number": num,
                "tracking": cdek_tracking(num)}
    except CdekError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _cdek_order_payload(rec, calc, opid, origin_code):
    """Собрать тело POST /v2/orders для ПВЗ→ПВЗ (тариф склад-склад)."""
    box = calc.box
    n = max(1, len(calc.items))
    per_w = max(1, calc.weight_g // n)
    items = []
    for i, it in enumerate(calc.items):
        items.append({
            "name": it.name[:255],
            "ware_key": (it.article or f"i{i + 1}")[:50],
            "payment": {"value": 0},                       # предоплата — на ПВЗ ноль
            "cost": max(1, round((it.unit_price or 0) / 100)),  # объявленная ценность, ₽
            "weight": per_w,
            "amount": it.count,
        })
    fio = (rec.get("fio") or rec.get("first_name") or rec.get("last_name") or "Получатель")
    return {
        "type": 1,
        "tariff_code": CDEK_TARIFF_PVZ,
        "shipment_point": origin_code,
        "delivery_point": rec["pvz_id"],
        "recipient": {"name": fio, "phones": [{"number": "+" + rec["phone"]}]},
        "packages": [{
            "number": opid,
            "weight": calc.weight_g,
            "length": box.dx, "width": box.dy, "height": box.dz,
            "items": items,
        }],
    }


# ====================================================================
#  ОТМЕНА
# ====================================================================

def cancel_delivery(carrier, request_id):
    """Отменить бронь у перевозчика (request_id: Яндекс — request_id, СДЭК — uuid заказа)."""
    if normalize(carrier) == "cdek":
        from cdek_delivery import CdekClient
        CdekClient().delete_order(request_id)
    else:
        from yandex_delivery import YandexDeliveryClient
        YandexDeliveryClient().cancel_request(request_id)


# ====================================================================
#  ТРЕК
# ====================================================================

def cdek_tracking(cdek_number):
    return f"https://www.cdek.ru/ru/tracking?order_id={cdek_number}" if cdek_number else ""


def refresh_tracking(carrier, row):
    """Обновить ссылку отслеживания из API. → '' если ещё не готова."""
    if normalize(carrier) == "cdek":
        from cdek_delivery import CdekClient
        info = CdekClient().get_order(row["request_id"])
        num = (info.get("entity") or {}).get("cdek_number") or row.get("cdek_number") or ""
        return cdek_tracking(num), num
    else:
        from yandex_delivery import YandexDeliveryClient
        info = YandexDeliveryClient().get_request_info(row["request_id"], as_model=False)
        return (info.get("sharing_url", "") or ""), ""


# ====================================================================
#  ЯРЛЫКИ (PDF)
# ====================================================================

def labels_pdf(carrier, request_ids):
    """PDF ярлыков/ШК для списка заявок ОДНОГО перевозчика (request_ids — их id)."""
    if normalize(carrier) == "cdek":
        from cdek_delivery import CdekClient
        c = CdekClient()
        # задание на печать ШК → uuid → PDF
        resp = c.print_barcodes({"orders": [{"order_uuid": u} for u in request_ids]})
        u = (resp.get("entity") or {}).get("uuid", "")
        if not u:
            raise RuntimeError("СДЭК не принял задание на печать")
        for _ in range(6):
            time.sleep(3)
            try:
                return c.get_barcode_pdf(u)
            except Exception:
                continue
        raise RuntimeError("СДЭК ещё готовит ШК, попробуй позже")
    else:
        from yandex_delivery import YandexDeliveryClient
        from yandex_delivery.errors import ApiError
        c = YandexDeliveryClient()
        try:
            return c.generate_labels(request_ids)
        except ApiError as e:
            if e.status_code == 409:  # часть заявок ещё не готова — печатаем готовые
                ready = []
                for rid in request_ids:
                    try:
                        info = c.get_request_info(rid, as_model=False)
                        if (info.get("state") or {}).get("status"):
                            ready.append(rid)
                    except Exception:
                        pass
                if not ready:
                    raise RuntimeError("ярлыки ещё готовятся у Яндекса — попробуй чуть позже")
                return c.generate_labels(ready)
            raise
