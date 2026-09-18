"""Исключения модуля cdek_delivery."""


class CdekError(Exception):
    """Базовая ошибка модуля (сеть, валидация, отсутствие доступов)."""


class CdekApiError(CdekError):
    """Не-2xx ответ от API CDEK.

    Атрибуты: status_code, errors (список кодов/сообщений от CDEK), payload (сырое тело).
    """

    def __init__(self, status_code, errors=None, payload=None):
        self.status_code = status_code
        self.errors = errors or []
        self.payload = payload
        msg = "; ".join(
            f"{e.get('code', '')} {e.get('message', '')}".strip()
            for e in (errors or [])
        ) or str(payload)[:200]
        super().__init__(f"[{status_code}] {msg}")
