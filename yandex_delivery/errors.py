"""Исключения модуля yandex_delivery."""


class YandexDeliveryError(Exception):
    """Базовая ошибка модуля (сеть, валидация, отсутствие данных)."""


class ApiError(YandexDeliveryError):
    """Не-2xx ответ от API Яндекса.

    Атрибуты:
        status_code — HTTP-код,
        code        — машинный код ошибки Яндекса (напр. 'no_delivery_options'),
        message     — человекочитаемое сообщение,
        payload     — распарсенное тело ответа (dict) или сырой текст.
    """

    def __init__(self, status_code, code=None, message=None, payload=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.payload = payload
        super().__init__(f"[{status_code}] {code or ''} {message or ''}".strip())
