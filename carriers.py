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
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

CDEK_TARIFF_PVZ = 136  # посылка склад-склад (ПВЗ→ПВЗ)


@dataclass
class DeliveryConfig:
    """Контекст одного отправителя (арендатора). Всё, что раньше бралось из общего
    .env, теперь можно передать явно — так движок перестаёт быть привязан к одному
    продавцу и готов принимать доступы клиента (личный кабинет → эта структура).

    Любое поле None → значение берётся из окружения/.env, как раньше (обратная
    совместимость: без cfg всё работает по-старому)."""
    # доступы перевозчиков
    yandex_token: Optional[str] = None
    yandex_env: Optional[str] = None        # 'test' | 'prod'
    cdek_account: Optional[str] = None
    cdek_secure: Optional[str] = None
    cdek_env: Optional[str] = None          # 'test' | 'prod'
    # ПВЗ отправления по перевозчикам
    origin_yandex: Optional[str] = None
    origin_cdek: Optional[str] = None
    # кто платит доставку: 'seller' | 'recipient'
    paid_by: Optional[str] = None


def _yandex_client(cfg: Optional[DeliveryConfig] = None):
    """Клиент Яндекса с доступами клиента (или из .env, если cfg не задан)."""
    from yandex_delivery import YandexDeliveryClient
    return YandexDeliveryClient(env=getattr(cfg, "yandex_env", None),
                                token=getattr(cfg, "yandex_token", None))


def _cdek_client(cfg: Optional[DeliveryConfig] = None):
    """Клиент СДЭК с доступами клиента (или из .env, если cfg не задан)."""
    from cdek_delivery import CdekClient
    return CdekClient(env=getattr(cfg, "cdek_env", None),
                      account=getattr(cfg, "cdek_account", None),
                      secure=getattr(cfg, "cdek_secure", None))


def normalize(carrier):
    return "cdek" if str(carrier or "").lower() == "cdek" else "yandex"


def paid_by(cfg: Optional[DeliveryConfig] = None):
    """'recipient' — доставку оплачивает покупатель (наложенный платёж); иначе 'seller'.
    Приоритет — значение из cfg (личный кабинет клиента); иначе общий флаг из .env."""
    if cfg is not None and cfg.paid_by:
        return cfg.paid_by.lower()
    try:
        from yandex_delivery import config as ycfg
        ycfg._load_dotenv()
    except Exception:
        pass
    return os.environ.get("DELIVERY_PAID_BY", "seller").lower()


def price_to_kopecks(price_str):
    """'184.83 RUB' → 18483 (копейки). Пусто/None → 0."""
    m = re.search(r"\d+[.,]?\d*", str(price_str or ""))
    return int(round(float(m.group(0).replace(",", ".")) * 100)) if m else 0


# ====================================================================
#  ПОДТВЕРЖДЕНИЕ (реальная бронь)
# ====================================================================

def confirm_delivery(carrier, row, rec, calc, opid, origin_id, cfg=None):
    """Забронировать доставку. Возвращает dict:
        {ok, request_id, cdek_number, tracking, price, error}.
    cfg (DeliveryConfig) — доступы отправителя; None → из .env (как раньше)."""
    if normalize(carrier) == "cdek":
        return _cdek_book(rec, calc, opid, origin_id, cfg)
    return _yandex_confirm(row, cfg)


def _yandex_confirm(row, cfg=None):
    from yandex_delivery.errors import YandexDeliveryError
    try:
        c = _yandex_client(cfg)
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


def _cdek_delivery_price(c, origin_code, rec, calc):
    """Стоимость доставки СДЭК (₽, целое) через calculator/tariff. 0 — если не вышло.
    Города берём: получателя — из rec['city'], отправления — по коду ПВЗ отправления."""
    try:
        to_code = c.city_code(rec.get("city", "")) if rec.get("city") else None
        from_pts = c.list_pickup_points(type=None, extra={"code": origin_code})
        from_code = from_pts[0].city_code if from_pts else None
        if not (to_code and from_code):
            return 0
        resp = c.calculate_tariff({
            "tariff_code": CDEK_TARIFF_PVZ,
            "from_location": {"code": from_code},
            "to_location": {"code": to_code},
            "packages": [{"weight": calc.weight_g}],
        })
        val = resp.get("total_sum") or resp.get("delivery_sum") or 0
        return int(round(float(val))) if val else 0
    except Exception:
        return 0


def _cdek_book(rec, calc, opid, origin_code, cfg=None):
    from cdek_delivery.errors import CdekError
    try:
        c = _cdek_client(cfg)
        recipient_cost = None
        if paid_by(cfg) == "recipient":
            price = _cdek_delivery_price(c, origin_code, rec, calc)
            if not price:
                return {"ok": False,
                        "error": "СДЭК: не удалось рассчитать стоимость доставки для наложенного платежа"}
            recipient_cost = price
        payload = _cdek_order_payload(rec, calc, opid, origin_code, recipient_cost)
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
                "tracking": cdek_tracking(num),
                "price": (f"{recipient_cost} RUB" if recipient_cost else "")}
    except CdekError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _cdek_order_payload(rec, calc, opid, origin_code, recipient_cost=None):
    """Собрать тело POST /v2/orders для ПВЗ→ПВЗ (тариф склад-склад).
    recipient_cost (₽) — если задан, доставку оплачивает получатель (наложенный платёж)."""
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
    body = {
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
    if recipient_cost:  # доставку оплачивает получатель на ПВЗ (наложенный платёж)
        body["delivery_recipient_cost"] = {"value": recipient_cost}
    return body


# ====================================================================
#  ОТМЕНА
# ====================================================================

def cancel_delivery(carrier, request_id, cfg=None):
    """Отменить бронь у перевозчика (request_id: Яндекс — request_id, СДЭК — uuid заказа)."""
    if normalize(carrier) == "cdek":
        _cdek_client(cfg).delete_order(request_id)
    else:
        _yandex_client(cfg).cancel_request(request_id)


# ====================================================================
#  ТРЕК
# ====================================================================

def cdek_tracking(cdek_number):
    return f"https://www.cdek.ru/ru/tracking?order_id={cdek_number}" if cdek_number else ""


def refresh_tracking(carrier, row, cfg=None):
    """Обновить ссылку отслеживания из API. → '' если ещё не готова."""
    if normalize(carrier) == "cdek":
        info = _cdek_client(cfg).get_order(row["request_id"])
        num = (info.get("entity") or {}).get("cdek_number") or row.get("cdek_number") or ""
        return cdek_tracking(num), num
    else:
        info = _yandex_client(cfg).get_request_info(row["request_id"], as_model=False)
        return (info.get("sharing_url", "") or ""), ""


# ====================================================================
#  ЯРЛЫКИ (PDF)
# ====================================================================

def labels_pdf(carrier, request_ids, cfg=None):
    """PDF ярлыков/ШК для списка заявок ОДНОГО перевозчика (request_ids — их id)."""
    if normalize(carrier) == "cdek":
        c = _cdek_client(cfg)
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
        from yandex_delivery.errors import ApiError
        c = _yandex_client(cfg)
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
