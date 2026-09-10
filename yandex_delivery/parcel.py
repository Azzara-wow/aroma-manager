"""Калькулятор посылки: из позиций заказа считает вес, подбирает коробку и
собирает place[]/items[] для заявки в Яндекс Доставку.

Все настраиваемые числа — в блоке РЕДАКТИРУЕМЫЕ ТАБЛИЦЫ ниже. Правь их здесь.
"""
from dataclasses import dataclass
from typing import List, Optional

from .models import Dimensions, Place, Item

# ================= РЕДАКТИРУЕМЫЕ ТАБЛИЦЫ =================

# Вес ПУСТОГО флакона по объёму, граммы.
FLACON_EMPTY_WEIGHTS_G = {5: 18, 10: 24, 30: 35, 50: 35, 100: 54}

# Плотность парфюма, г/мл. Содержимое считаем как объём × плотность (сейчас 1 к 1).
PERFUME_DENSITY = 1.0

# Вес упаковки (пупырка/коробка/наполнитель) на посылку, граммы. Меняй свободно.
PACKAGING_WEIGHT_G = 20


@dataclass(frozen=True)
class Box:
    """Стандартная коробка. dx=длина, dy=ширина, dz=высота (см)."""
    code: str
    name: str
    dx: int
    dy: int
    dz: int
    max_weight_g: int


# Стандартные коробки Яндекса (пока ориентируемся на них).
BOXES = [
    Box("XS", "Размер XS", 17, 12, 9, 500),
    Box("S", "Размер S", 25, 15, 10, 2000),
    Box("M", "Размер M", 35, 25, 15, 5000),
    Box("L", "Размер L", 45, 30, 20, 12000),
    Box("XL", "Размер XL", 60, 40, 45, 20000),
]

# ================= КОНЕЦ ТАБЛИЦ =================

# коробки по возрастанию грузоподъёмности — для авто-подбора наименьшей подходящей
_BOXES_BY_CAPACITY = sorted(BOXES, key=lambda b: b.max_weight_g)
_BOXES_BY_CODE = {b.code.upper(): b for b in BOXES}


@dataclass
class ParcelLine:
    """Одна позиция посылки: аромат, объём флакона, количество, цена за штуку."""
    name: str
    volume_ml: int
    count: int
    unit_price: int = 0  # копейки, оценочная стоимость за 1 шт (для billing_details)


@dataclass
class ParcelCalc:
    """Результат расчёта посылки."""
    weight_g: int
    box: Box
    place: Place
    items: List[Item]


def flacon_gross_weight_g(volume_ml: int) -> int:
    """Брутто-вес одного флакона: пустой флакон + содержимое (объём × плотность)."""
    empty = FLACON_EMPTY_WEIGHTS_G.get(volume_ml)
    if empty is None:
        # неизвестный объём — берём вес пустого флакона ближайшего известного
        nearest = min(FLACON_EMPTY_WEIGHTS_G, key=lambda v: abs(v - volume_ml))
        empty = FLACON_EMPTY_WEIGHTS_G[nearest]
    return round(empty + volume_ml * PERFUME_DENSITY)


def parcel_weight_g(lines: List[ParcelLine], include_packaging: bool = True) -> int:
    """Суммарный вес посылки в граммах."""
    total = sum(flacon_gross_weight_g(l.volume_ml) * l.count for l in lines)
    if include_packaging:
        total += PACKAGING_WEIGHT_G
    return total


def get_box(code: str) -> Box:
    """Коробка по коду (XS/S/M/L/XL)."""
    try:
        return _BOXES_BY_CODE[code.upper()]
    except KeyError:
        raise KeyError(f"Нет коробки с кодом {code!r}. Доступны: {list(_BOXES_BY_CODE)}")


def choose_box(weight_g: int) -> Box:
    """Наименьшая коробка, выдерживающая вес. Если тяжелее максимума — самая большая."""
    for b in _BOXES_BY_CAPACITY:
        if weight_g <= b.max_weight_g:
            return b
    return _BOXES_BY_CAPACITY[-1]


def calc(lines: List[ParcelLine], barcode: str, box: Optional[Box] = None) -> ParcelCalc:
    """Полный расчёт: вес → коробка (или заданная) → place + items для API.

    barcode связывает грузоместо и товары внутри него.
    box=None → коробка подбирается автоматически по весу.
    """
    weight = parcel_weight_g(lines)
    box = box or choose_box(weight)
    place = Place(barcode=barcode, dimensions=Dimensions(weight, box.dx, box.dy, box.dz))
    items = [
        Item(name=l.name, count=l.count, place_barcode=barcode, unit_price=l.unit_price)
        for l in lines
    ]
    return ParcelCalc(weight_g=weight, box=box, place=place, items=items)
