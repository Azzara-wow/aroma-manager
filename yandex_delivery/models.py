"""Датаклассы модуля: то, что читаем из API (from_dict) и что отправляем (to_dict).

Модели намеренно «толерантны»: сырой ответ Яндекса всегда сохраняется в .raw,
чтобы ничего не терять, пока схема калибруется на живых тестовых вызовах.
"""
from dataclasses import dataclass, field
from typing import Optional, List


# --- ЧТЕНИЕ из API ---

@dataclass
class PickupPoint:
    """Пункт выдачи / точка самопривоза из pickup-points/list."""
    id: str
    name: str = ""
    type: str = ""                 # pickup_point | terminal | warehouse
    operator_id: str = ""
    full_address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    available_for_dropoff: Optional[bool] = None
    payment_methods: List[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d):
        pos = d.get("position") or {}
        addr = d.get("address") or {}
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            type=d.get("type", ""),
            operator_id=d.get("operator_id", ""),
            full_address=addr.get("full_address", ""),
            latitude=pos.get("latitude"),
            longitude=pos.get("longitude"),
            available_for_dropoff=d.get("available_for_dropoff"),
            payment_methods=d.get("payment_methods") or [],
            raw=d,
        )


@dataclass
class RequestInfo:
    """Инфо о заявке из request/info — нужна для пары «штрих-код ↔ получатель»."""
    request_id: str = ""
    status: str = ""
    recipient_first_name: str = ""
    recipient_last_name: str = ""
    recipient_patronymic: str = ""
    recipient_phone: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def recipient_fio(self):
        parts = [self.recipient_last_name, self.recipient_first_name, self.recipient_patronymic]
        return " ".join(p for p in parts if p).strip()

    @classmethod
    def from_dict(cls, d):
        # ответ request/info вкладывает заявку в ключ "request"
        req = d.get("request") or {}
        r = req.get("recipient_info") or d.get("recipient_info") or {}
        state = d.get("state") or {}
        status = (
            d.get("status")
            or state.get("status")
            or state.get("description")
            or ""
        )
        return cls(
            request_id=d.get("request_id", ""),
            status=status,
            recipient_first_name=r.get("first_name", ""),
            recipient_last_name=r.get("last_name", ""),
            recipient_patronymic=r.get("patronymic", ""),
            recipient_phone=r.get("phone", ""),
            raw=d,
        )


# --- ОТПРАВКА в API ---

@dataclass
class Recipient:
    """Получатель (recipient_info)."""
    first_name: str
    phone: str
    last_name: str = ""
    patronymic: str = ""
    email: str = ""

    def to_dict(self):
        out = {"first_name": self.first_name, "phone": self.phone}
        if self.last_name:
            out["last_name"] = self.last_name
        if self.patronymic:
            out["patronymic"] = self.patronymic
        if self.email:
            out["email"] = self.email
        return out


@dataclass
class Dimensions:
    """Весогабариты грузоместа. weight_gross — в ГРАММАХ, dx/dy/dz — в САНТИМЕТРАХ."""
    weight_gross: int
    dx: int
    dy: int
    dz: int

    def to_dict(self):
        return {"weight_gross": self.weight_gross, "dx": self.dx, "dy": self.dy, "dz": self.dz}


@dataclass
class Place:
    """Грузоместо (коробка). barcode связывает place и items внутри него."""
    barcode: str
    dimensions: Dimensions

    def to_dict(self):
        return {"barcode": self.barcode, "physical_dims": self.dimensions.to_dict()}


@dataclass
class Item:
    """Товарная позиция внутри грузоместа."""
    name: str
    count: int
    place_barcode: str
    article: str = ""
    unit_price: int = 0            # копейки
    assessed_unit_price: int = 0   # оценочная стоимость, копейки

    def to_dict(self):
        # article обязателен в API; если не задан — берём имя как артикул.
        return {
            "name": self.name,
            "article": self.article or self.name,
            "count": self.count,
            "place_barcode": self.place_barcode,
            "billing_details": {
                "unit_price": self.unit_price,
                "assessed_unit_price": self.assessed_unit_price or self.unit_price,
            },
        }
