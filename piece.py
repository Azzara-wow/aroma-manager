# piece.py — штучные товары закупки (База для парфюма, ММБ): не разливаются, едут бутылкой.
#
# В витрине это категория «База» (core.PIECE_CATEGORIES) — заказ в штуках, поэтому в
# zakaz_items.volume_ml у таких позиций лежит КОЛИЧЕСТВО ШТУК, а не миллилитры.
# Признак zakaz_items.is_piece ставит синхронизация с витриной (по категории); для
# старых и ручных позиций — по названию (looks_piece).
#
# Вес 1 шт: вписанный организатором (settings «pieceweight:<название>»), иначе
# оценка по названию: литры × 1000 г + бутылка (1 л → 1100 г, 0,5 л → 560 г).

import re

from models import get_setting, set_setting

_PIECE_PREFIXES = ("база", "ммб")


def looks_piece(name):
    """Похоже ли название на штучную базу («База для парфюма…», «ММБ 0,5»)."""
    return (name or "").strip().lower().startswith(_PIECE_PREFIXES)


def default_weight_g(name):
    """Оценка веса 1 шт по названию: последнее число ≤ 5 — литры."""
    nums = re.findall(r"\d+(?:[.,]\d+)?", name or "")
    liters = None
    for n in reversed(nums):
        v = float(n.replace(",", "."))
        if 0 < v <= 5:
            liters = v
            break
    if liters is None:
        return 1100
    bottle = 100 if liters >= 1 else 60
    return int(round(liters * 1000 + bottle))


def _key(name):
    return "pieceweight:" + (name or "").strip().lower()


def weight_g(name):
    """Вес 1 шт: вписанный руками, иначе оценка по названию."""
    v = get_setting(_key(name), "").strip()
    try:
        return int(v) if v else default_weight_g(name)
    except ValueError:
        return default_weight_g(name)


def manual_weight(name):
    v = get_setting(_key(name), "").strip()
    return int(v) if v.isdigit() else None


def set_weight(name, grams):
    set_setting(_key(name), str(int(grams)) if str(grams).strip() else "")
