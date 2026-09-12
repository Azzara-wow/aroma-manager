"""Чтение/запись листа «Покупатели» (книга aroma_web) со стороны дашборда.

Дашборд берёт ПОЛУЧАТЕЛЕЙ для Яндекс Доставки отсюда — живое чтение того же
Google-листа, что заполняет витрина aroma_web. Пишем тем же сервисным аккаунтом.

КОНТРАКТ ЛИСТА (0-индексация; шапка в строке 1):
  0 A телефон (канон 7XXXXXXXXXX) — КЛЮЧ            7  H Фамилия
  1 B имя (отображение)                            8  I Имя
  2 C код-хеш                                      9  J Отчество
  3 D адрес                                        10 K Город
  4 E роль                                         11 L ПВЗ адрес
  5 F создан                                       12 M ПВЗ id (platform_id)
  6 G заметка
Колонки A–G принадлежат aroma_web (не трогаем их порядок). H–M — доставка.
"""
import os
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

USERS_URL = "https://docs.google.com/spreadsheets/d/15PjPHqSl6Iju41VIZOGkCMwy4kyBomr_X9F60hYwo0U/edit"
SHEET_NAME = "Пользователи"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Ключ сервисного аккаунта ищем по этим путям (env имеет приоритет).
KEY_PATHS = [
    os.environ.get("SERVICE_ACCOUNT_JSON", ""),
    "service_account.json",                       # локально рядом с дашбордом
    "../aroma_web/service_account.json",          # локальная разработка
    "/opt/aroma-web/service_account.json",        # beget
    "/etc/secrets/service_account.json",
]

# индексы столбцов
COL_PHONE, COL_NAME, COL_CODE, COL_ADDRESS, COL_ROLE, COL_CREATED, COL_NOTE = range(7)
COL_LAST, COL_FIRST, COL_PATR, COL_CITY, COL_PVZ_ADDR, COL_PVZ_ID = range(7, 13)
COL_TRACKING = 13  # N — ссылка отслеживания Яндекса (пишет дашборд после подтверждения)


def _key_path():
    for p in KEY_PATHS:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Ключ сервисного аккаунта не найден. Задай SERVICE_ACCOUNT_JSON или положи "
        "service_account.json рядом. Искал: " + ", ".join(p for p in KEY_PATHS if p)
    )


def _install_retries(client):
    """Автоповтор идемпотентных запросов (GET/PUT) на обрывах и 429/5xx.
    Канал РФ↔Google нестабилен — без ретраев чтение/запись иногда падает по таймауту.
    update() в gspread — это PUT (идемпотентно), поэтому его повтор безопасен."""
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(
            total=4, connect=4, read=4, backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "PUT", "HEAD", "OPTIONS", "DELETE"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session = client.http_client.session
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except Exception:
        pass
    try:
        client.set_timeout(30)
    except Exception:
        pass


@lru_cache(maxsize=1)
def _ws():
    creds = Credentials.from_service_account_file(_key_path(), scopes=SCOPES)
    client = gspread.authorize(creds)
    _install_retries(client)
    return client.open_by_url(USERS_URL).worksheet(SHEET_NAME)


def normalize_phone(raw) -> str:
    """К канону: 11 цифр с ведущей 7 (как в aroma_web)."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def valid_phone(canon: str) -> bool:
    return len(canon) == 11 and canon[0] == "7" and canon.isdigit()


def _cell(row, i):
    return (row[i].strip() if i < len(row) and row[i] else "")


def _row_to_recipient(row, idx):
    """Строка листа → словарь получателя для доставки."""
    last = _cell(row, COL_LAST)
    first = _cell(row, COL_FIRST)
    patr = _cell(row, COL_PATR)
    fio = " ".join(p for p in (last, first, patr) if p)
    phone = normalize_phone(_cell(row, COL_PHONE))
    pvz_id = _cell(row, COL_PVZ_ID)
    return {
        "row": idx,                      # 0-индекс в values (для точечной правки)
        "phone": phone,
        "name": _cell(row, COL_NAME),    # витринное имя (может быть неформальным)
        "last_name": last,
        "first_name": first,
        "patronymic": patr,
        "fio": fio,
        "city": _cell(row, COL_CITY),
        "pvz_address": _cell(row, COL_PVZ_ADDR),
        "pvz_id": pvz_id,
        # готов к доставке: валидный телефон + имя (или фамилия) + выбран ПВЗ
        "delivery_ready": bool(valid_phone(phone) and (first or last) and pvz_id),
    }


def list_recipients():
    """Все покупатели с полями доставки (отсортированы по имени/фамилии)."""
    values = _ws().get_all_values()
    out = []
    for r in range(1, len(values)):
        row = values[r]
        if not valid_phone(normalize_phone(_cell(row, COL_PHONE))):
            continue
        out.append(_row_to_recipient(row, r))
    out.sort(key=lambda x: (x["last_name"] or x["name"]).lower())
    return out


def _find_row(values, canon):
    for r in range(1, len(values)):
        if normalize_phone(_cell(values[r], COL_PHONE)) == canon:
            return r
    return None


def get_recipient(phone_raw):
    canon = normalize_phone(phone_raw)
    values = _ws().get_all_values()
    idx = _find_row(values, canon)
    return _row_to_recipient(values[idx], idx) if idx is not None else None


def _col_a1(col_idx_0):
    n, s = col_idx_0, ""
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def set_pvz(phone_raw, pvz_address, pvz_id):
    """Записать выбранный ПВЗ (человекочитаемый адрес + platform_id) для телефона."""
    canon = normalize_phone(phone_raw)
    ws = _ws()
    idx = _find_row(ws.get_all_values(), canon)
    if idx is None:
        return {"ok": False, "reason": "not_found"}
    rng = f"{_col_a1(COL_PVZ_ADDR)}{idx + 1}:{_col_a1(COL_PVZ_ID)}{idx + 1}"
    ws.update(range_name=rng, values=[[pvz_address or "", pvz_id or ""]])
    return {"ok": True}


def set_fio(phone_raw, last_name="", first_name="", patronymic=""):
    """Записать Фамилию/Имя/Отчество получателя для телефона."""
    canon = normalize_phone(phone_raw)
    ws = _ws()
    idx = _find_row(ws.get_all_values(), canon)
    if idx is None:
        return {"ok": False, "reason": "not_found"}
    rng = f"{_col_a1(COL_LAST)}{idx + 1}:{_col_a1(COL_PATR)}{idx + 1}"
    ws.update(range_name=rng, values=[[last_name or "", first_name or "", patronymic or ""]])
    return {"ok": True}


def set_city(phone_raw, city):
    canon = normalize_phone(phone_raw)
    ws = _ws()
    idx = _find_row(ws.get_all_values(), canon)
    if idx is None:
        return {"ok": False, "reason": "not_found"}
    ws.update_acell(f"{_col_a1(COL_CITY)}{idx + 1}", city or "")
    return {"ok": True}


def set_tracking(phone_raw, url):
    """Записать ссылку отслеживания (колонка N) — чтобы покупатель видел её в витрине."""
    canon = normalize_phone(phone_raw)
    ws = _ws()
    idx = _find_row(ws.get_all_values(), canon)
    if idx is None:
        return {"ok": False, "reason": "not_found"}
    ws.update_acell(f"{_col_a1(COL_TRACKING)}{idx + 1}", url or "")
    return {"ok": True}


# ======================================================================
#  Мост «имя закупки → телефон получателя» (Вариант A: телефон = личность)
# ======================================================================

def _canon_name(s):
    """Имя к сравнимому виду: нижний регистр, схлопнутые пробелы."""
    return " ".join((s or "").lower().replace("ё", "е").split())


def suggest_phone(name, recipients=None):
    """Лучшее совпадение телефона из листа по имени покупателя закупки.
    Возвращает канонический телефон или '' если уверенного совпадения нет."""
    recipients = recipients if recipients is not None else list_recipients()
    target = _canon_name(name)
    if not target:
        return ""
    ttokens = set(target.split())
    best_score, best_phone = 0, ""
    for r in recipients:
        candidates = [r.get("name", ""), r.get("fio", ""),
                      (r.get("first_name", "") + " " + r.get("last_name", ""))]
        score = 0
        for c in candidates:
            cc = _canon_name(c)
            if not cc:
                continue
            if cc == target:
                score = max(score, 100)
            elif target in cc or cc in target:
                # вложение засчитываем сильным только если в общей части ≥2 слов
                # (иначе одно общее имя «Ольга» ложно связывает разных людей)
                shorter = cc if len(cc) <= len(target) else target
                score = max(score, 80 if len(shorter.split()) >= 2 else 55)
            else:
                overlap = ttokens & set(cc.split())
                if overlap:
                    score = max(score, 40 + 10 * len(overlap))
        if score > best_score:
            best_score, best_phone = score, r["phone"]
    return best_phone if best_score >= 70 else ""


def picker_options(recipients=None):
    """Список для выпадашки привязки: [{'phone','label'}], label = 'ФИО/имя — телефон'."""
    recipients = recipients if recipients is not None else list_recipients()
    out = []
    for r in recipients:
        title = r.get("fio") or r.get("name") or "—"
        out.append({"phone": r["phone"], "label": f"{title} — {r['phone']}"})
    out.sort(key=lambda x: x["label"].lower())
    return out
