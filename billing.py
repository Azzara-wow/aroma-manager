# billing.py — счёт покупателя по закупке: закупка + доставка = итого.
#
# Доставка в счёте — только когда её оплачиваем мы (Яндекс за наш счёт): цена
# черновика/подтверждённой заявки из deliveries, вверх до 10 ₽. СДЭК с наложенным
# платежом покупатель платит на ПВЗ, в счёт не идёт.
#
# Способ оплаты (решение Елены): «по ссылке» (по умолчанию) или «на карту» — это
# разметка для организатора и поставщика, НЕ факт оплаты. Факт оплаты — честная
# галочка payment_zakupka; она же уходит в витрину («Оплачено ✓»).

import buyers_sheet
import carriers
from models import get_setting, set_setting

METHOD_LINK = "link"
METHOD_CARD = "card"


def method_key(zakupka_id, buyer_name):
    return f"paymethod:{zakupka_id}:{buyer_name}"


def get_method(zakupka_id, buyer_name):
    return METHOD_CARD if get_setting(method_key(zakupka_id, buyer_name), "") == METHOD_CARD else METHOD_LINK


def set_method(zakupka_id, buyer_name, method):
    set_setting(method_key(zakupka_id, buyer_name), METHOD_CARD if method == METHOD_CARD else "")


def phone_by_name_map(db):
    return {b["name"]: buyers_sheet.normalize_phone(b["phone"] or "")
            for b in db.execute("SELECT name, phone FROM buyers").fetchall()}


def phone_of(buyer_name, phone_map):
    return phone_map.get(buyer_name, "") or buyers_sheet.phone_from_name(buyer_name)


def delivery_by_phone(db, zakupka_id):
    """{phone: (₽ в счёт, carrier)} — доставки, которые оплачиваем мы."""
    out = {}
    for d in db.execute(
        "SELECT phone, price, carrier FROM deliveries WHERE zakupka_id = ? "
        "AND status IN ('offered', 'confirmed', 'labeled') ORDER BY id",
        (zakupka_id,),
    ).fetchall():
        carrier = carriers.normalize(d["carrier"])
        if carriers.paid_by(carrier=carrier) == "recipient":
            continue
        rub = carriers.invoice_delivery_rub(d["price"])
        if rub:
            out[d["phone"]] = (rub, carrier)
    return out


def invoices(db, zakupka_id):
    """Счета по покупателям закупки (только с суммой > 0), по имени."""
    rows = db.execute(
        "SELECT zi.buyer_name, SUM(zi.total_sum) AS s, MAX(COALESCE(st.payment_zakupka, 0)) AS paid "
        "FROM zakaz_items zi LEFT JOIN statuses st ON st.zakaz_item_id = zi.id "
        "WHERE zi.zakupka_id = ? AND COALESCE(zi.ext_gone, 0) = 0 "
        "GROUP BY zi.buyer_name HAVING s > 0 ORDER BY zi.buyer_name",
        (zakupka_id,),
    ).fetchall()
    phones = phone_by_name_map(db)
    deliv = delivery_by_phone(db, zakupka_id)
    out = []
    for r in rows:
        phone = phone_of(r["buyer_name"], phones)
        goods = int(round(r["s"] or 0))
        d_rub, d_carrier = deliv.get(phone, (0, ""))
        out.append({
            "buyer": r["buyer_name"],
            "phone": phone,
            "goods": goods,
            "delivery": d_rub,
            "delivery_carrier": d_carrier,
            "total": goods + d_rub,
            "method": get_method(zakupka_id, r["buyer_name"]),
            "paid": bool(r["paid"]),
        })
    return out
