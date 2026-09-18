"""HTTP-клиент API CDEK v2. Авторизация — OAuth client_credentials (токен ~1 час,
кэшируем в клиенте). Методы 1:1 с эндпоинтами /v2/*.

Отличия от Яндекса: токен получаем сами (client_id/secret), у ПВЗ — code (строка),
город — числовой city_code, создание заказа АСИНХРОННОЕ (POST → uuid → poll GET).
"""
import time

import requests

from . import config
from .errors import CdekError, CdekApiError
from .models import CdekCity, CdekPoint


class CdekClient:
    def __init__(self, env=None, account=None, secure=None,
                 timeout=config.DEFAULT_TIMEOUT, session=None):
        self.base_url, self.env, self._account, self._secure = config.resolve(env, account, secure)
        self.timeout = timeout
        self.session = session or requests.Session()
        self._token = None
        self._token_exp = 0.0

    # ------------------------------------------------------------------ авторизация
    def _auth(self):
        """Получить (и кэшировать) OAuth-токен. Обновляем за минуту до истечения."""
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        url = self.base_url.rstrip("/") + "/v2/oauth/token"
        try:
            resp = self.session.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._account,
                    "client_secret": self._secure,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise CdekError(f"Сетевая ошибка авторизации CDEK: {e}") from e
        if resp.status_code != 200:
            raise CdekApiError(resp.status_code, payload=_safe_json(resp))
        data = resp.json()
        self._token = data.get("access_token", "")
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        if not self._token:
            raise CdekError("CDEK не вернул access_token")
        return self._token

    # ------------------------------------------------------------------ низкий уровень
    def _request(self, method, path, params=None, json=None, binary=False):
        url = self.base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self._auth()}", "Accept": "application/json"}
        try:
            resp = self.session.request(
                method, url, params=params, json=json, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise CdekError(f"Сетевая ошибка при {method} {url}: {e}") from e
        if resp.status_code // 100 != 2:
            payload = _safe_json(resp)
            errors = (payload or {}).get("errors") if isinstance(payload, dict) else None
            raise CdekApiError(resp.status_code, errors=errors, payload=payload if payload is not None else resp.text)
        if binary:
            return resp.content
        if not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------- локации и ПВЗ
    def suggest_cities(self, name, country_code="RU"):
        """GET /v2/location/suggest/cities → [CdekCity]."""
        data = self._request("GET", "/v2/location/suggest/cities",
                             params={"name": name, "country_code": country_code})
        return [CdekCity.from_dict(c) for c in (data or [])]

    def city_code(self, name):
        """Числовой код первого подходящего города (или None)."""
        cities = self.suggest_cities(name)
        return cities[0].code if cities else None

    def list_pickup_points(self, city_code=None, type="PVZ", is_handout=None,
                           is_reception=None, country_code=None, size=None,
                           page=None, extra=None, as_models=True):
        """GET /v2/deliverypoints → список ПВЗ/постаматов.
        is_handout=True — точки ВЫДАЧИ (для получателя),
        is_reception=True — точки ПРИЁМА (для отправления, точка А)."""
        params = {}
        if city_code is not None:
            params["city_code"] = city_code
        if type is not None:
            params["type"] = type
        if is_handout is not None:
            params["is_handout"] = "true" if is_handout else "false"
        if is_reception is not None:
            params["is_reception"] = "true" if is_reception else "false"
        if country_code is not None:
            params["country_code"] = country_code
        if size is not None:
            params["size"] = size
        if page is not None:
            params["page"] = page
        if extra:
            params.update(extra)
        data = self._request("GET", "/v2/deliverypoints", params=params)
        points = data if isinstance(data, list) else (data.get("items") or [])
        return [CdekPoint.from_dict(p) for p in points] if as_models else points

    # ------------------------------------------------------------- расчёт и заказы
    def calculate_tariff(self, payload):
        """POST /v2/calculator/tariff — расчёт стоимости/срока по тарифу (аналог черновика с ценой)."""
        return self._request("POST", "/v2/calculator/tariff", json=payload)

    def calculate_tarifflist(self, payload):
        """POST /v2/calculator/tarifflist — список доступных тарифов с ценами."""
        return self._request("POST", "/v2/calculator/tarifflist", json=payload)

    def create_order(self, payload):
        """POST /v2/orders — регистрация заказа (АСИНХРОННО). Возвращает entity.uuid + requests."""
        return self._request("POST", "/v2/orders", json=payload)

    def get_order(self, uuid):
        """GET /v2/orders/{uuid} — статус, cdek_number, ссылка отслеживания."""
        return self._request("GET", f"/v2/orders/{uuid}")

    def delete_order(self, uuid):
        """DELETE /v2/orders/{uuid} — отмена заказа."""
        return self._request("DELETE", f"/v2/orders/{uuid}")

    # ------------------------------------------------------------- печать (ярлыки/ШК)
    def print_order(self, payload):
        """POST /v2/print/orders — задание на печать формы заказа (→ uuid)."""
        return self._request("POST", "/v2/print/orders", json=payload)

    def get_print_order_pdf(self, uuid):
        """GET /v2/print/orders/{uuid}.pdf — PDF формы заказа (байты)."""
        return self._request("GET", f"/v2/print/orders/{uuid}.pdf", binary=True)

    def print_barcodes(self, payload):
        """POST /v2/print/barcodes — задание на печать ШК-этикеток (→ uuid)."""
        return self._request("POST", "/v2/print/barcodes", json=payload)

    def get_barcode_pdf(self, uuid):
        """GET /v2/print/barcodes/{uuid}.pdf — PDF со штрих-кодами (байты)."""
        return self._request("GET", f"/v2/print/barcodes/{uuid}.pdf", binary=True)


def _safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return None
