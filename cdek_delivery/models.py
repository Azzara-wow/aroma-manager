"""Датаклассы модуля CDEK. Сырой ответ всегда сохраняется в .raw."""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class CdekCity:
    """Город из /v2/location/suggest/cities."""
    code: Optional[int] = None
    full_name: str = ""
    city: str = ""
    region: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d):
        return cls(
            code=d.get("code"),
            full_name=d.get("full_name") or d.get("city") or "",
            city=d.get("city", ""),
            region=d.get("region", ""),
            raw=d,
        )


@dataclass
class CdekPoint:
    """Пункт выдачи / постамат из /v2/deliverypoints. code — идентификатор для заказа."""
    code: str = ""
    name: str = ""
    type: str = ""                 # PVZ | POSTAMAT
    address_full: str = ""
    city: str = ""
    city_code: Optional[int] = None
    have_cash: Optional[bool] = None
    have_cashless: Optional[bool] = None
    allowed_cod: Optional[bool] = None
    is_reception: Optional[bool] = None
    is_handout: Optional[bool] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d):
        loc = d.get("location") or {}
        return cls(
            code=d.get("code", ""),
            name=d.get("name", ""),
            type=d.get("type", ""),
            address_full=loc.get("address_full") or loc.get("address") or "",
            city=loc.get("city", ""),
            city_code=loc.get("city_code"),
            have_cash=d.get("have_cash"),
            have_cashless=d.get("have_cashless"),
            allowed_cod=d.get("allowed_cod"),
            is_reception=d.get("is_reception"),
            is_handout=d.get("is_handout"),
            latitude=loc.get("latitude"),
            longitude=loc.get("longitude"),
            raw=d,
        )
