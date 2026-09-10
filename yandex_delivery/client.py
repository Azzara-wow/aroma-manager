"""HTTP-клиент API Яндекс Доставки. Методы 1:1 с эндпоинтами /api/b2b/platform/*.

Сложные тела (offers/create, request/create) пока принимаются как готовый dict —
их точную схему добьём на живых тестовых вызовах в Фазах 4–5. Простые ручки
(location/detect, pickup-points/list) уже типизированы.
"""
import requests

from . import config
from .errors import ApiError, YandexDeliveryError
from .models import PickupPoint, RequestInfo


class YandexDeliveryClient:
    def __init__(self, env=None, token=None, timeout=config.DEFAULT_TIMEOUT, session=None):
        self.base_url, self.env, self._token = config.resolve(env, token)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------ низкий уровень
    def _url(self, path):
        return self.base_url.rstrip("/") + "/api/b2b/platform/" + path.lstrip("/")

    def _request(self, method, path, json=None, params=None, binary=False):
        url = self._url(path)
        try:
            resp = self.session.request(
                method, url, json=json, params=params, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise YandexDeliveryError(f"Сетевая ошибка при {method} {url}: {e}") from e

        if resp.status_code // 100 != 2:
            code = message = None
            payload = resp.text
            try:
                payload = resp.json()
                code = payload.get("code")
                message = payload.get("message")
            except ValueError:
                pass
            raise ApiError(resp.status_code, code, message, payload)

        if binary:
            return resp.content
        if not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------- 2. Точки самопривоза и ПВЗ
    def detect_location(self, location):
        """POST /location/detect → список вариантов [{geo_id, address}, ...]."""
        data = self._request("POST", "location/detect", json={"location": location})
        return data.get("variants", [])

    def geo_id(self, location):
        """Удобный хелпер: первый geo_id для строки адреса."""
        variants = self.detect_location(location)
        if not variants:
            raise YandexDeliveryError(f"Не найден geo_id для {location!r}")
        return variants[0]["geo_id"]

    def list_pickup_points(self, geo_id=None, type=None, payment_methods=None,
                           available_for_dropoff=None, latitude=None, longitude=None,
                           operator_ids=None, extra=None, as_models=True):
        """POST /pickup-points/list → список ПВЗ/точек самопривоза."""
        body = {}
        if geo_id is not None:
            body["geo_id"] = geo_id
        if type is not None:
            body["type"] = type
        if payment_methods is not None:
            body["payment_methods"] = payment_methods
        if available_for_dropoff is not None:
            body["available_for_dropoff"] = available_for_dropoff
        if latitude is not None:
            body["latitude"] = latitude
        if longitude is not None:
            body["longitude"] = longitude
        if operator_ids is not None:
            body["operator_ids"] = operator_ids
        if extra:
            body.update(extra)

        data = self._request("POST", "pickup-points/list", json=body)
        points = data.get("points", [])
        return [PickupPoint.from_dict(p) for p in points] if as_models else points

    # ------------------------------------------------------------- 3. Основные запросы
    def create_offers(self, payload):
        """POST /offers/create — ШАГ ①: варианты доставки с ценой (черновик)."""
        return self._request("POST", "offers/create", json=payload)

    def confirm_offer(self, offer_id, **extra):
        """POST /offers/confirm — ШАГ ②: бронирование варианта → появляется request_id."""
        body = {"offer_id": offer_id}
        body.update(extra)
        return self._request("POST", "offers/confirm", json=body)

    def create_request(self, payload):
        """POST /request/create — одношаговое создание (опция «без подтверждения»)."""
        return self._request("POST", "request/create", json=payload)

    def get_request_info(self, request_id, as_model=True):
        """GET /request/info — статус + recipient_info (для пары ШК ↔ получатель)."""
        data = self._request("GET", "request/info", params={"request_id": request_id})
        return RequestInfo.from_dict(data) if as_model else data

    def get_requests_info(self, payload):
        """POST /requests/info — инфо о заявках за период / пачкой (для массовых операций)."""
        return self._request("POST", "requests/info", json=payload)

    def cancel_request(self, request_id, **extra):
        """POST /request/cancel — отмена заявки."""
        body = {"request_id": request_id}
        body.update(extra)
        return self._request("POST", "request/cancel", json=body)

    # ---------------------------------------------------------------- 4. Ярлыки и акты
    def generate_labels(self, request_ids, **extra):
        """POST /request/generate-labels — ярлыки со штрих-кодами. Возвращает байты (PDF)."""
        if isinstance(request_ids, str):
            request_ids = [request_ids]
        body = {"request_ids": request_ids}
        body.update(extra)
        return self._request("POST", "request/generate-labels", json=body, binary=True)
