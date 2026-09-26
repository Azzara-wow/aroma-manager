# vitrina_sync.py — дашборд ⇄ витрина (aroma_web): состав закупки без гуглшита.
#
# Витрина отдаёт свёртку Потока (GET /api/sync/zakupka): телефон, имя, аромат, мл,
# цена за мл, сумма. Ключ записи = телефон + аромат (в нижнем регистре): Поток и сам
# сворачивает заказы по этой паре. У позиций дашборда ключ достаём из buyer_name
# («79001234567 - Имя») + aroma_name, поэтому старые закупки тоже обновляются.
#
# Правила обновления (решение Елены):
#   - новые пары — добавляем (статус 0);
#   - изменился объём/цена/сумма — правим позицию, статусы НЕ трогаем;
#   - пара пропала в витрине: не разлита — удаляем, разлита — оставляем с пометкой ext_gone;
#   - позиции без телефона в имени (добавлены руками в «Состав») не трогаем.

import hashlib
import json
import os

import requests

import buyers_sheet
from piece import looks_piece

VITRINA_URL = os.environ.get("AROMA_WEB_URL", "http://127.0.0.1:8001").rstrip("/")


def sync_token():
    env = os.environ.get("SYNC_TOKEN", "").strip()
    if env:
        return env
    with open(buyers_sheet._key_path(), encoding="utf-8") as f:
        sa = json.load(f)
    return hashlib.sha256(("aroma-sync:" + sa["private_key"]).encode("utf-8")).hexdigest()


def fetch():
    """Состав из витрины: {"rows": [...], "problems": [...]}. Ошибка — исключение с понятным текстом."""
    try:
        r = requests.get(VITRINA_URL + "/api/sync/zakupka",
                         headers={"X-Sync-Token": sync_token()}, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"Витрина не отвечает ({VITRINA_URL}): {e}")
    if r.status_code == 403:
        raise RuntimeError("Витрина не приняла ключ синхронизации.")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Витрина ответила не JSON (HTTP {r.status_code}).")
    if not data.get("ok"):
        raise RuntimeError("Витрина: " + str(data.get("error", "ошибка")))
    return data


def key_of(phone, aroma):
    return f"{phone}|{(aroma or '').strip().lower()}"


def item_key(buyer_name, aroma_name):
    phone = buyers_sheet.phone_from_name(buyer_name or "")
    return key_of(phone, aroma_name) if phone else None


def buyer_label(row):
    return f"{row['phone']} - {row['name']}"


def compute_diff(db, zakupka_id, rows):
    """Сравнить позиции закупки с витриной. Ничего не пишет."""
    items = db.execute(
        "SELECT z.id, z.buyer_name, z.aroma_name, z.volume_ml, z.price_per_10ml, z.total_sum, "
        "COALESCE(z.ext_gone, 0) AS ext_gone, COALESCE(s.rozliv, 0) AS rozliv, "
        "COALESCE(z.is_piece, 0) AS is_piece "
        "FROM zakaz_items z LEFT JOIN statuses s ON s.zakaz_item_id = z.id "
        "WHERE z.zakupka_id = ?", (zakupka_id,)).fetchall()

    by_key, manual = {}, 0
    name_by_phone = {}
    for it in items:
        k = item_key(it["buyer_name"], it["aroma_name"])
        if not k:
            manual += 1
            continue
        by_key.setdefault(k, []).append(it)
        name_by_phone.setdefault(k.split("|")[0], it["buyer_name"])

    added, changed, removed, kept, piece_fix = [], [], [], [], []
    unchanged = 0
    seen = set()
    for r in rows:
        k = key_of(r["phone"], r["aroma"])
        seen.add(k)
        vol, price, amount = int(r["volume"]), float(r["per_ml"]), float(r["amount"])
        piece = 1 if (r.get("piece") or looks_piece(r["aroma"])) else 0
        its = by_key.get(k)
        if not its:
            added.append({"buyer": name_by_phone.get(r["phone"]) or buyer_label(r),
                          "aroma": r["aroma"], "volume": vol, "price": price, "amount": amount,
                          "piece": piece})
            continue
        # признак «штучный» — молча выравниваем по витрине (не считаем изменением)
        piece_fix += [(x["id"], piece) for x in its if x["is_piece"] != piece]
        # пара может быть разбита на несколько строк (правили руками) — сравниваем итог
        it = its[0]
        old_vol = sum(x["volume_ml"] or 0 for x in its)
        old_amount = sum(x["total_sum"] or 0 for x in its)
        if (old_vol != vol or abs(old_amount - amount) > 0.5
                or any(x["ext_gone"] for x in its)
                or (len(its) == 1 and abs((it["price_per_10ml"] or 0) - price) > 0.005)):
            changed.append({"id": it["id"], "buyer": it["buyer_name"], "aroma": it["aroma_name"],
                            "old_volume": old_vol, "volume": vol,
                            "old_amount": old_amount, "amount": amount, "price": price,
                            "rozliv": it["rozliv"],
                            # лишние строки пары: удаляем только не разлитые
                            "dups": [x["id"] for x in its[1:] if not x["rozliv"]]})
        else:
            unchanged += 1

    for k, its in by_key.items():
        if k in seen:
            continue
        for it in its:
            rec = {"id": it["id"], "buyer": it["buyer_name"], "aroma": it["aroma_name"],
                   "volume": it["volume_ml"], "amount": it["total_sum"]}
            if it["rozliv"]:
                if not it["ext_gone"]:
                    kept.append(rec)
            else:
                removed.append(rec)

    return {"added": added, "changed": changed, "removed": removed, "kept": kept,
            "unchanged": unchanged, "manual": manual, "piece_fix": piece_fix}


def apply_diff(db, zakupka_id, diff):
    """Применить разницу (commit — на вызывающем)."""
    for a in diff["added"]:
        cur = db.execute(
            "INSERT INTO zakaz_items (zakupka_id, buyer_name, aroma_name, volume_ml, price_per_10ml, total_sum, is_piece) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (zakupka_id, a["buyer"], a["aroma"], a["volume"], a["price"], a["amount"], a.get("piece", 0)))
        db.execute("INSERT INTO statuses (zakaz_item_id, rozliv, upakovka, payment_zakupka, shipped) "
                   "VALUES (?, 0, 0, 0, 0)", (cur.lastrowid,))
        if not db.execute("SELECT id FROM buyers WHERE name = ?", (a["buyer"],)).fetchone():
            db.execute("INSERT INTO buyers (name) VALUES (?)", (a["buyer"],))
    for c in diff["changed"]:
        db.execute("UPDATE zakaz_items SET volume_ml = ?, price_per_10ml = ?, total_sum = ?, ext_gone = 0 "
                   "WHERE id = ?", (c["volume"], c["price"], c["amount"], c["id"]))
        for dup in c["dups"]:   # задвоенная пара — оставляем одну позицию
            db.execute("DELETE FROM statuses WHERE zakaz_item_id = ?", (dup,))
            db.execute("DELETE FROM zakaz_items WHERE id = ?", (dup,))
    for r in diff["removed"]:
        db.execute("DELETE FROM statuses WHERE zakaz_item_id = ?", (r["id"],))
        db.execute("DELETE FROM zakaz_items WHERE id = ?", (r["id"],))
    for r in diff["kept"]:
        db.execute("UPDATE zakaz_items SET ext_gone = 1 WHERE id = ?", (r["id"],))
    for iid, piece in diff.get("piece_fix", []):
        db.execute("UPDATE zakaz_items SET is_piece = ? WHERE id = ?", (piece, iid))
