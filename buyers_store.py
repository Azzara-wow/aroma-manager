"""Покупатели со стороны дашборда — через витрину (aroma_web), без гугл-листа.

Витрина хранит покупателей в своей базе (buyers.db) и отдаёт их по закрытому каналу
(заголовок X-Sync-Token, тот же ключ, что у «Забрать заказы из витрины»):
  ГЛОБАЛЬНОЕ — телефон (ключ), имя, ФИО, город, ПВЗ, перевозчик, e-mail, заметка, код входа;
  ЗАКУПОЧНОЕ — текущий счёт девочки: ссылка на оплату, сумма, доставка, «оплачено»,
               реквизиты перевода, ссылка отслеживания. Хозяин счёта — дашборд, витрина
               только показывает.
Имена функций те же, что были у листа (buyers_sheet.py), чтобы маршруты не менялись.
"""
import hashlib
import json
import os
import re

import requests

VITRINA_URL = os.environ.get("AROMA_WEB_URL", "http://127.0.0.1:8001").rstrip("/")
PAID_MARK = "оплачено"

# Ключ сервисного аккаунта — из него выводится ключ канала (env SYNC_TOKEN имеет приоритет).
KEY_PATHS = [
    os.environ.get("SERVICE_ACCOUNT_JSON", ""),
    "service_account.json",                       # локально рядом с дашбордом
    "../aroma_web/service_account.json",          # локальная разработка
    "/opt/aroma-web/service_account.json",        # сервер
    "/etc/secrets/service_account.json",
]

# Перевозчики с автоматической отправкой. Любое другое значение («Почта России», «Озон»…)
# вписывает организатор: ручная доставка по договорённости, адрес — свободным текстом
# в поле ПВЗ-адреса, через API такие посылки НЕ отправляем.
SELF_CARRIERS = ("yandex", "cdek")


def manual_carrier(raw):
    """Название ручного перевозчика или '' (Яндекс/СДЭК/пусто)."""
    v = (raw or "").strip()
    return "" if v.lower() in ("",) + SELF_CARRIERS else v


def _key_path():
    for p in KEY_PATHS:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Ключ сервисного аккаунта не найден. Задай SERVICE_ACCOUNT_JSON или положи "
        "service_account.json рядом. Искал: " + ", ".join(p for p in KEY_PATHS if p)
    )


def sync_token():
    env = os.environ.get("SYNC_TOKEN", "").strip()
    if env:
        return env
    with open(_key_path(), encoding="utf-8") as f:
        sa = json.load(f)
    return hashlib.sha256(("aroma-sync:" + sa["private_key"]).encode("utf-8")).hexdigest()


def _call(path, payload=None, timeout=30):
    """Запрос к витрине. Ошибка связи/ключа — исключение с понятным текстом."""
    try:
        h = {"X-Sync-Token": sync_token()}
        if payload is None:
            r = requests.get(VITRINA_URL + path, headers=h, timeout=timeout)
        else:
            r = requests.post(VITRINA_URL + path, headers=h, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Витрина не отвечает ({VITRINA_URL}): {e}")
    if r.status_code == 403:
        raise RuntimeError("Витрина не приняла ключ синхронизации.")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"Витрина ответила не JSON (HTTP {r.status_code}).")


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


def _to_recipient(b):
    """Запись витрины → словарь получателя (те же ключи, что были у листа, плюс счёт)."""
    g = lambda k: (b.get(k) or "").strip() if isinstance(b.get(k), str) else (b.get(k) or "")
    last, first, patr = g("last_name"), g("first_name"), g("patronymic")
    phone = b["phone"]
    pvz_id = g("pvz_id")
    manual = manual_carrier(g("carrier"))
    return {
        "phone": phone,
        "name": g("name"),               # витринное имя (может быть неформальным)
        "last_name": last,
        "first_name": first,
        "patronymic": patr,
        "fio": " ".join(p for p in (last, first, patr) if p),
        "city": g("city"),
        "pvz_address": g("pvz_address"),
        "pvz_id": pvz_id,
        # перевозчик — как выбрал покупатель (пусто, пока не выбрал);
        # для отправки пустое трактуем как Яндекс уже на этапе диспетчеризации
        "carrier": "manual" if manual else g("carrier").lower(),
        "carrier_manual": manual,        # «Почта России» и т.п. — отправка руками
        "carrier_raw": g("carrier"),
        "email": g("email"),
        "address": g("address"),         # старое поле с регистрации — справка
        "note": g("note"),
        "role": g("role"),
        "created": g("created"),
        "has_code": bool(b.get("has_code")),
        # счёт текущей закупки
        "pay_link": g("pay_link"),
        "pay_amount": g("pay_amount"),
        "pay_delivery": g("pay_delivery"),
        "paid": bool(b.get("paid")),
        "pay_to": g("pay_to"),
        "tracking_url": g("tracking_url"),
        # готов к доставке: валидный телефон + имя (или фамилия) + выбран ПВЗ
        # (ручная доставка к автоматической отправке не готова никогда)
        "delivery_ready": bool(valid_phone(phone) and (first or last) and pvz_id and not manual),
    }


def list_recipients():
    """Все покупатели с полями доставки (отсортированы по имени/фамилии)."""
    data = _call("/api/sync/buyers")
    if not data.get("ok"):
        raise RuntimeError("Витрина: " + str(data.get("error", "ошибка")))
    out = [_to_recipient(b) for b in data["buyers"] if valid_phone(b.get("phone", ""))]
    out.sort(key=lambda x: (x["last_name"] or x["name"]).lower())
    return out


def get_recipient(phone_raw):
    canon = normalize_phone(phone_raw)
    return next((r for r in list_recipients() if r["phone"] == canon), None)


# ======================================================================
#  Глобальное: профиль девочки
# ======================================================================

def save_buyer(phone_raw, fields, create=False):
    """Завести (create) или поправить девочку. fields — name, last_name, first_name,
    patronymic, city, pvz_address, pvz_id, carrier, email, note, address.
    → {"ok": True, "buyer": получатель} или {"ok": False, "reason": ...}"""
    res = _call("/api/sync/buyers/save", {"phone": normalize_phone(phone_raw),
                                          "create": bool(create), "fields": fields or {}})
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("error", "ошибка")}
    return {"ok": True, "buyer": _to_recipient(res["buyer"])}


def reset_code(phone_raw):
    res = _call("/api/sync/buyers/reset-code", {"phone": normalize_phone(phone_raw)})
    return {"ok": bool(res.get("ok")), "reason": res.get("error", "")}


def delete_buyer(phone_raw):
    res = _call("/api/sync/buyers/delete", {"phone": normalize_phone(phone_raw)})
    return {"ok": bool(res.get("ok"))}


def set_pvz(phone_raw, pvz_address, pvz_id):
    """Записать выбранный ПВЗ (человекочитаемый адрес + platform_id) для телефона."""
    return save_buyer(phone_raw, {"pvz_address": pvz_address or "", "pvz_id": pvz_id or ""})


def set_fio(phone_raw, last_name="", first_name="", patronymic=""):
    """Записать Фамилию/Имя/Отчество получателя для телефона."""
    return save_buyer(phone_raw, {"last_name": last_name or "", "first_name": first_name or "",
                                  "patronymic": patronymic or ""})


def set_city(phone_raw, city):
    return save_buyer(phone_raw, {"city": city or ""})


# ======================================================================
#  Закупочное: счёт девочки
# ======================================================================

def set_pay_fields_bulk(updates, clear_others=False):
    """Счета разом: {phone: {"link"?, "amount"?, "delivery"?, "paid"?, "payto"?}} — поле,
    которого нет, остаётся как было. clear_others=True — у всех, кого нет в updates, счёт
    стирается (новая закупка). Возвращает {ok, updated, not_found}."""
    ups = {normalize_phone(k): v for k, v in (updates or {}).items()}
    res = _call("/api/sync/bills", {"updates": ups, "clear_others": bool(clear_others)}, timeout=60)
    if not res.get("ok"):
        raise RuntimeError("Витрина: " + str(res.get("error", "ошибка")))
    return {"ok": True, "updated": res.get("updated", 0), "not_found": res.get("not_found", [])}


def set_pay_row(phone_raw, fields):
    """Счёт ОДНОЙ девочки: только переданные поля, остальные как были."""
    res = set_pay_fields_bulk({phone_raw: fields or {}})
    return {"ok": not res["not_found"], "reason": "not_found" if res["not_found"] else ""}


def set_paid(phone_raw, paid):
    """Отметка оплаты одной девочки — она сразу видит её в витрине."""
    return set_pay_row(phone_raw, {"paid": PAID_MARK if paid else ""})


def set_tracking(phone_raw, url):
    """Ссылка отслеживания — девочка видит её в витрине."""
    return set_pay_row(phone_raw, {"tracking": url or ""})


# ======================================================================
#  Мост «имя закупки → телефон получателя» (Вариант A: телефон = личность)
# ======================================================================

def _canon_name(s):
    """Имя к сравнимому виду: нижний регистр, схлопнутые пробелы."""
    return " ".join((s or "").lower().replace("ё", "е").split())


def phone_from_name(name):
    """Извлечь телефон из имени вида «79022034755 - Оксана». Канон 7XXXXXXXXXX или ''.

    Теперь витрина передаёт покупателя как «телефон - имя», поэтому телефон берём
    прямо из строки — это точная привязка, без нечёткого совпадения по имени."""
    s = str(name or "")
    # телефон обычно в начале, до разделителя (-, —, |, , : ;)
    head = re.split(r"[\-—|,:;]", s, 1)[0]
    canon = normalize_phone(head)
    if valid_phone(canon):
        return canon
    # запасной вариант: если во всей строке ровно один валидный номер
    canon = normalize_phone(s)
    return canon if valid_phone(canon) else ""


def suggest_phone(name, recipients=None):
    """Лучшее совпадение телефона по имени покупателя закупки. Канон или ''.
    Приоритет — телефон ПРЯМО В ИМЕНИ («79022034755 - Оксана»), это точная привязка;
    иначе падаем на нечёткое совпадение по имени со списком получателей."""
    ph = phone_from_name(name)
    if ph:
        return ph
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
