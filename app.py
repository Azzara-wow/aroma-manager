from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import pandas as pd
from urllib.parse import urlparse, parse_qs
import sqlite3
from datetime import datetime
import os

app = FastAPI()
from catalog_api import setup_catalog; setup_catalog(app)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

from models import get_db, init_db, get_setting, set_setting

# === Функция для получения CSV из Google Sheets ===
def make_csv_url(sheet_url: str) -> str:
    """Преобразует ссылку на Google Sheets в ссылку для скачивания CSV"""
    parsed = urlparse(sheet_url)
    path_parts = parsed.path.split("/")

    try:
        d_index = path_parts.index("d")
        spreadsheet_id = path_parts[d_index + 1]
    except (ValueError, IndexError):
        raise ValueError("Неверная ссылка на Google Sheets")

    query = parse_qs(parsed.query)
    gid = query.get("gid", ["0"])[0]

    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def row_to_dict(row):
    """Безопасно конвертирует sqlite3.Row в dict"""
    return dict(row)


# === Главная страница — дашборд ===
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    _snapshot("index_open")
    db = get_db()

    # Активные закупки
    active_zakupkas_raw = db.execute(
        "SELECT * FROM zakupkas WHERE status = 'active' ORDER BY created_at DESC"
    ).fetchall()

    # Архивные закупки
    closed_zakupkas_raw = db.execute(
        "SELECT * FROM zakupkas WHERE status = 'closed' ORDER BY created_at DESC"
    ).fetchall()

    # Считаем количество позиций для каждой закупки
    active_zakupkas = []
    for z in active_zakupkas_raw:
        z_dict = row_to_dict(z)
        count = db.execute(
            "SELECT COUNT(*) as cnt FROM zakaz_items WHERE zakupka_id = ?",
            (z_dict["id"],)
        ).fetchone()
        z_dict["item_count"] = count["cnt"]
        active_zakupkas.append(z_dict)

    closed_zakupkas = []
    for z in closed_zakupkas_raw:
        z_dict = row_to_dict(z)
        count = db.execute(
            "SELECT COUNT(*) as cnt FROM zakaz_items WHERE zakupka_id = ?",
            (z_dict["id"],)
        ).fetchone()
        z_dict["item_count"] = count["cnt"]
        closed_zakupkas.append(z_dict)

    db.close()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "active_zakupkas": active_zakupkas,
            "closed_zakupkas": closed_zakupkas
        }
    )


# === Страница создания новой закупки ===
@app.get("/zakupka/new", response_class=HTMLResponse)
async def new_zakupka_form(request: Request):
    return templates.TemplateResponse("zakupka_new.html", {"request": request})


@app.post("/zakupka/new")
async def create_zakupka(
        request: Request,
        name: str = Form(...),
        sheet_url: str = Form(...)
):
    db = None
    try:
        csv_url = make_csv_url(sheet_url)
        df = pd.read_csv(csv_url)

        required_cols = ["Имя покупателя", "Название аромата", "Объём (мл)", "Цена за 10мл", "Сумма"]

        col_mapping = {}
        for req_col in required_cols:
            for col in df.columns:
                if req_col.lower() in col.lower():
                    col_mapping[req_col] = col
                    break

        if len(col_mapping) < 5:
            alt_names = {
                "Имя покупателя": ["имя", "покупатель", "buyer", "name"],
                "Название аромата": ["аромат", "название", "aroma", "aroma_name"],
                "Объём (мл)": ["объём", "объем", "volume", "мл", "ml"],
                "Цена за 10мл": ["цена", "price", "за 10"],
                "Сумма": ["сумма", "sum", "итого", "total"]
            }

            for req_col, alternatives in alt_names.items():
                if req_col not in col_mapping:
                    for col in df.columns:
                        if any(alt.lower() in col.lower() for alt in alternatives):
                            col_mapping[req_col] = col
                            break

        if len(col_mapping) < 5:
            raise ValueError(
                f"Не найдены нужные колонки. Найдено: {list(col_mapping.keys())}. Колонки в файле: {list(df.columns)}")

        db = get_db()

        cursor = db.execute(
            "INSERT INTO zakupkas (name, google_sheet_url, status, created_at) VALUES (?, ?, 'active', ?)",
            (name, sheet_url, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        zakupka_id = cursor.lastrowid

        for _, row in df.iterrows():
            buyer_name = str(row[col_mapping["Имя покупателя"]]).strip()
            aroma_name = str(row[col_mapping["Название аромата"]]).strip()

            volume_str = str(row[col_mapping["Объём (мл)"]]).strip()
            volume_ml = int(''.join(filter(str.isdigit, volume_str)) or 0)

            price_str = str(row[col_mapping["Цена за 10мл"]]).strip()
            price_per_10ml = float(price_str.replace(",", ".").replace(" ", ""))

            sum_str = str(row[col_mapping["Сумма"]]).strip()
            total_sum = float(sum_str.replace(",", ".").replace(" ", ""))

            cursor = db.execute(
                "INSERT INTO zakaz_items (zakupka_id, buyer_name, aroma_name, volume_ml, price_per_10ml, total_sum) VALUES (?, ?, ?, ?, ?, ?)",
                (zakupka_id, buyer_name, aroma_name, volume_ml, price_per_10ml, total_sum)
            )
            zakaz_item_id = cursor.lastrowid

            db.execute(
                "INSERT INTO statuses (zakaz_item_id, rozliv, upakovka, payment_zakupka, shipped) VALUES (?, 0, 0, 0, 0)",
                (zakaz_item_id,)
            )

            existing = db.execute("SELECT id FROM buyers WHERE name = ?", (buyer_name,)).fetchone()
            if not existing:
                db.execute("INSERT INTO buyers (name) VALUES (?)", (buyer_name,))

        db.commit()
        db.close()

        return RedirectResponse(url=f"/zakupka/{zakupka_id}", status_code=303)

    except Exception as e:
        # Обязательно закрываем соединение при ошибке, иначе БД остаётся заблокированной
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        return templates.TemplateResponse(
            "zakupka_new.html",
            {
                "request": request,
                "error": f"Ошибка при импорте: {str(e)}"
            }
        )


# === Страница закупки с тремя вкладками ===
@app.get("/zakupka/{zakupka_id}", response_class=HTMLResponse)
async def view_zakupka(request: Request, zakupka_id: int):
    db = get_db()

    zakupka = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zakupka:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")

    zakupka_dict = row_to_dict(zakupka)

    # === ДАННЫЕ ДЛЯ ВКЛАДКИ "РОЗЛИВ" ===
    rozliv_items_raw = db.execute("""
        SELECT 
            zi.id, 
            zi.buyer_name, 
            zi.aroma_name, 
            zi.volume_ml,
            s.rozliv
        FROM zakaz_items zi
        JOIN statuses s ON s.zakaz_item_id = zi.id
        WHERE zi.zakupka_id = ?
        ORDER BY zi.aroma_name, zi.buyer_name
    """, (zakupka_id,)).fetchall()

    rozliv_items = [row_to_dict(item) for item in rozliv_items_raw]

    rozliv_total = len(rozliv_items)
    rozliv_done = sum(1 for item in rozliv_items if item["rozliv"] == 1)
    rozliv_percent = int((rozliv_done / rozliv_total * 100)) if rozliv_total > 0 else 0

    # === ДАННЫЕ ДЛЯ ВКЛАДКИ "ОТПРАВКА" ===
    otpravka_zakupka_raw = db.execute("""
        SELECT 
            zi.id,
            zi.buyer_name,
            zi.aroma_name,
            zi.volume_ml,
            'закупка' as source,
            s.upakovka
        FROM zakaz_items zi
        JOIN statuses s ON s.zakaz_item_id = zi.id
        WHERE zi.zakupka_id = ?
    """, (zakupka_id,)).fetchall()

    otpravka_nalichie_raw = db.execute("""
        SELECT 
            no.id,
            no.buyer_name,
            no.aroma_name,
            no.volume_ml,
            'наличие' as source,
            COALESCE(s.upakovka, 0) as upakovka
        FROM nalichie_orders no
        LEFT JOIN statuses s ON s.nalichie_order_id = no.id
        WHERE no.zakupka_id = ? OR no.zakupka_id IS NULL
    """, (zakupka_id,)).fetchall()

    all_otpravka = [row_to_dict(item) for item in otpravka_zakupka_raw] + [row_to_dict(item) for item in
                                                                           otpravka_nalichie_raw]
    all_otpravka.sort(key=lambda x: (x["buyer_name"], 0 if x["source"] == "закупка" else 1))

    otpravka_total = len(all_otpravka)
    otpravka_done = sum(1 for item in all_otpravka if item["upakovka"] == 1)
    otpravka_percent = int((otpravka_done / otpravka_total * 100)) if otpravka_total > 0 else 0

    # === ДАННЫЕ ДЛЯ ВКЛАДКИ "ОПЛАТА" ===
    buyers_summary = {}

    zakupka_sums = db.execute("""
        SELECT 
            zi.buyer_name,
            SUM(zi.total_sum) as total_zakupka,
            MAX(s.payment_zakupka) as payment_zakupka,
            MAX(s.shipped) as shipped
        FROM zakaz_items zi
        JOIN statuses s ON s.zakaz_item_id = zi.id
        WHERE zi.zakupka_id = ?
        GROUP BY zi.buyer_name
    """, (zakupka_id,)).fetchall()

    for row in zakupka_sums:
        row_dict = row_to_dict(row)
        buyer = row_dict["buyer_name"]
        buyers_summary[buyer] = {
            "sum_zakupka": row_dict["total_zakupka"],
            "sum_nalichie": 0,
            "payment_zakupka": row_dict["payment_zakupka"],
            "payment_nalichie": 0,
            "shipped": row_dict["shipped"]
        }

    nalichie_sums = db.execute("""
        SELECT 
            no.buyer_name,
            SUM(no.price) as total_nalichie,
            MAX(COALESCE(s.payment_nalichie, 0)) as payment_nalichie
        FROM nalichie_orders no
        LEFT JOIN statuses s ON s.nalichie_order_id = no.id
        WHERE no.zakupka_id = ? OR no.zakupka_id IS NULL
        GROUP BY no.buyer_name
    """, (zakupka_id,)).fetchall()

    for row in nalichie_sums:
        row_dict = row_to_dict(row)
        buyer = row_dict["buyer_name"]
        if buyer in buyers_summary:
            buyers_summary[buyer]["sum_nalichie"] = row_dict["total_nalichie"]
            buyers_summary[buyer]["payment_nalichie"] = row_dict["payment_nalichie"]
        else:
            buyers_summary[buyer] = {
                "sum_zakupka": 0,
                "sum_nalichie": row_dict["total_nalichie"],
                "payment_zakupka": 0,
                "payment_nalichie": row_dict["payment_nalichie"],
                "shipped": 0
            }

    for buyer_name in buyers_summary:
        buyer_info = db.execute(
            "SELECT phone, address FROM buyers WHERE name = ?",
            (buyer_name,)
        ).fetchone()
        if buyer_info:
            buyers_summary[buyer_name]["phone"] = buyer_info["phone"] or ""
            buyers_summary[buyer_name]["address"] = buyer_info["address"] or ""
        else:
            buyers_summary[buyer_name]["phone"] = ""
            buyers_summary[buyer_name]["address"] = ""

    buyers_summary_sorted = sorted(buyers_summary.items(), key=lambda x: x[0])

    total_buyers = len(buyers_summary_sorted)
    paid_zakupka = sum(1 for _, data in buyers_summary_sorted if data["payment_zakupka"] == 1)
    shipped_count = sum(1 for _, data in buyers_summary_sorted if data["shipped"] == 1)

    payment_zakupka_percent = int((paid_zakupka / total_buyers * 100)) if total_buyers > 0 else 0
    shipped_percent = int((shipped_count / total_buyers * 100)) if total_buyers > 0 else 0

    db.close()

    return templates.TemplateResponse(
        "zakupka.html",
        {
            "request": request,
            "zakupka": zakupka_dict,
            "rozliv_items": rozliv_items,
            "rozliv_total": rozliv_total,
            "rozliv_done": rozliv_done,
            "rozliv_percent": rozliv_percent,
            "all_otpravka": all_otpravka,
            "otpravka_total": otpravka_total,
            "otpravka_done": otpravka_done,
            "otpravka_percent": otpravka_percent,
            "buyers_summary": buyers_summary_sorted,
            "total_buyers": total_buyers,
            "paid_zakupka": paid_zakupka,
            "shipped_count": shipped_count,
            "payment_zakupka_percent": payment_zakupka_percent,
            "shipped_percent": shipped_percent
        }
    )


# === API для обновления статусов ===
@app.post("/api/status/rozliv/{item_id}")
async def toggle_rozliv(item_id: int):
    db = get_db()
    current = db.execute("SELECT rozliv FROM statuses WHERE zakaz_item_id = ?", (item_id,)).fetchone()
    if current:
        new_val = 1 if current["rozliv"] == 0 else 0
        db.execute("UPDATE statuses SET rozliv = ? WHERE zakaz_item_id = ?", (new_val, item_id))
    else:
        # Строки статуса для этой позиции нет — создаём её,
        # иначе галочка молча не сохранялась бы (и был бы NameError на new_val)
        new_val = 1
        db.execute(
            "INSERT INTO statuses (zakaz_item_id, rozliv) VALUES (?, ?)",
            (item_id, new_val)
        )
    db.commit()
    db.close()
    return {"ok": True, "new_value": new_val}


@app.post("/api/status/upakovka/{item_id}")
async def toggle_upakovka(item_id: int, source: str = "zakupka"):
    db = get_db()
    if source == "zakupka":
        current = db.execute("SELECT upakovka FROM statuses WHERE zakaz_item_id = ?", (item_id,)).fetchone()
        if current:
            new_val = 1 if current["upakovka"] == 0 else 0
            db.execute("UPDATE statuses SET upakovka = ? WHERE zakaz_item_id = ?", (new_val, item_id))
        else:
            # Раньше здесь не было этой ветки: если строки статуса для позиции
            # закупки не существовало, галочка молча не сохранялась (и падал
            # NameError на new_val). Теперь создаём строку статуса.
            new_val = 1
            db.execute(
                "INSERT INTO statuses (zakaz_item_id, upakovka) VALUES (?, ?)",
                (item_id, new_val)
            )
    else:
        current = db.execute("SELECT upakovka FROM statuses WHERE nalichie_order_id = ?", (item_id,)).fetchone()
        if current:
            new_val = 1 if current["upakovka"] == 0 else 0
            db.execute("UPDATE statuses SET upakovka = ? WHERE nalichie_order_id = ?", (new_val, item_id))
        else:
            new_val = 1
            db.execute(
                "INSERT INTO statuses (nalichie_order_id, upakovka) VALUES (?, ?)",
                (item_id, new_val)
            )

    db.commit()
    db.close()
    return {"ok": True, "new_value": new_val}


@app.post("/api/status/payment-zakupka/{buyer_name}")
async def toggle_payment_zakupka(buyer_name: str, zakupka_id: int = Form(...)):
    db = get_db()
    items = db.execute(
        "SELECT zi.id FROM zakaz_items zi WHERE zi.zakupka_id = ? AND zi.buyer_name = ?",
        (zakupka_id, buyer_name)
    ).fetchall()

    if items:
        current = db.execute(
            "SELECT payment_zakupka FROM statuses WHERE zakaz_item_id = ?",
            (items[0]["id"],)
        ).fetchone()

        new_val = 1 if current["payment_zakupka"] == 0 else 0

        for item in items:
            db.execute(
                "UPDATE statuses SET payment_zakupka = ? WHERE zakaz_item_id = ?",
                (new_val, item["id"])
            )

        db.commit()

    db.close()
    return {"ok": True, "new_value": new_val}


@app.post("/api/status/payment-nalichie/{buyer_name}")
async def toggle_payment_nalichie(buyer_name: str, zakupka_id: int = Form(...)):
    db = get_db()
    items = db.execute(
        "SELECT no.id FROM nalichie_orders no WHERE (no.zakupka_id = ? OR no.zakupka_id IS NULL) AND no.buyer_name = ?",
        (zakupka_id, buyer_name)
    ).fetchall()

    if items:
        current = db.execute(
            "SELECT payment_nalichie FROM statuses WHERE nalichie_order_id = ?",
            (items[0]["id"],)
        ).fetchone()

        # Правильный toggle
        if current is None:
            new_val = 1  # записи нет, создадим с значением 1
        else:
            new_val = 1 if current["payment_nalichie"] == 0 else 0

        for item in items:
            existing = db.execute(
                "SELECT id FROM statuses WHERE nalichie_order_id = ?",
                (item["id"],)
            ).fetchone()

            if existing:
                db.execute(
                    "UPDATE statuses SET payment_nalichie = ? WHERE nalichie_order_id = ?",
                    (new_val, item["id"])
                )
            else:
                db.execute(
                    "INSERT INTO statuses (nalichie_order_id, payment_nalichie) VALUES (?, ?)",
                    (item["id"], new_val)
                )

        db.commit()

    db.close()
    return {"ok": True, "new_value": new_val}


@app.post("/api/status/shipped/{buyer_name}")
async def toggle_shipped(buyer_name: str, zakupka_id: int = Form(...)):
    db = get_db()
    items = db.execute(
        "SELECT zi.id FROM zakaz_items zi WHERE zi.zakupka_id = ? AND zi.buyer_name = ?",
        (zakupka_id, buyer_name)
    ).fetchall()

    if items:
        current = db.execute(
            "SELECT shipped FROM statuses WHERE zakaz_item_id = ?",
            (items[0]["id"],)
        ).fetchone()

        new_val = 1 if current["shipped"] == 0 else 0

        for item in items:
            db.execute(
                "UPDATE statuses SET shipped = ? WHERE zakaz_item_id = ?",
                (new_val, item["id"])
            )

        db.commit()

    db.close()
    return {"ok": True, "new_value": new_val}


@app.post("/zakupka/{zakupka_id}/close")
async def close_zakupka(zakupka_id: int):
    db = get_db()
    db.execute("UPDATE zakupkas SET status = 'closed' WHERE id = ?", (zakupka_id,))
    db.commit()
    db.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/zakupka/{zakupka_id}/reopen")
async def reopen_zakupka(zakupka_id: int):
    db = get_db()
    db.execute("UPDATE zakupkas SET status = 'active' WHERE id = ?", (zakupka_id,))
    db.commit()
    db.close()
    return RedirectResponse(url=f"/zakupka/{zakupka_id}", status_code=303)


# === Покупатели ===
@app.get("/buyers", response_class=HTMLResponse)
def buyers_list(request: Request):
    db = get_db()
    buyers_raw = db.execute("SELECT * FROM buyers ORDER BY name").fetchall()
    buyers = [row_to_dict(b) for b in buyers_raw]
    db.close()

    # Мост «имя → телефон»: тянем получателей из листа, считаем автоподсказку.
    # Если лист недоступен — страница всё равно работает, просто без подсказок.
    import buyers_sheet
    pick_options, sheet_error = [], None
    try:
        recipients = buyers_sheet.list_recipients()
        pick_options = buyers_sheet.picker_options(recipients)
        for b in buyers:
            b["linked_phone"] = buyers_sheet.normalize_phone(b.get("phone") or "")
            b["suggested_phone"] = (
                "" if b["linked_phone"]
                else buyers_sheet.suggest_phone(b.get("name") or "", recipients)
            )
    except Exception as e:
        sheet_error = str(e)
        for b in buyers:
            b["linked_phone"] = (b.get("phone") or "")
            b["suggested_phone"] = ""

    return templates.TemplateResponse("buyers.html", {
        "request": request,
        "buyers": buyers,
        "pick_options": pick_options,
        "sheet_error": sheet_error,
    })


@app.post("/buyers/link")
def link_buyer_phone(buyer_id: int = Form(...), phone: str = Form("")):
    """Привязать покупателя дашборда к телефону получателя (канон 7XXXXXXXXXX)."""
    import buyers_sheet
    canon = buyers_sheet.normalize_phone(phone) if phone.strip() else ""
    db = get_db()
    db.execute("UPDATE buyers SET phone = ? WHERE id = ?", (canon, buyer_id))
    db.commit()
    db.close()
    return RedirectResponse(url="/buyers", status_code=303)


@app.post("/buyers/add")
async def add_buyer(
    request: Request,
    name: str = Form(...),
    phone: str = Form(""),
    address: str = Form(""),
    middle_name: str = Form("")
):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO buyers (name, phone, address, middle_name) VALUES (?, ?, ?, ?)",
            (name.strip(), phone.strip(), address.strip(), middle_name.strip())
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Если покупатель уже существует — обновляем
        db.execute(
            "UPDATE buyers SET phone = ?, address = ?, middle_name = ? WHERE name = ?",
            (phone.strip(), address.strip(), middle_name.strip(), name.strip())
        )
        db.commit()
    db.close()
    return RedirectResponse(url="/buyers", status_code=303)


@app.post("/buyers/edit/{buyer_id}")
async def edit_buyer(
    buyer_id: int,
    name: str = Form(...),
    phone: str = Form(""),
    address: str = Form(""),
    middle_name: str = Form("")
):
    db = get_db()
    db.execute(
        "UPDATE buyers SET name = ?, phone = ?, address = ?, middle_name = ? WHERE id = ?",
        (name.strip(), phone.strip(), address.strip(), middle_name.strip(), buyer_id)
    )
    db.commit()
    db.close()
    return RedirectResponse(url="/buyers", status_code=303)


@app.post("/buyers/delete/{buyer_id}")
async def delete_buyer(buyer_id: int):
    # ВАЖНО: только POST. Раньше был GET — и поисковые боты, прелоадеры браузера
    # и превью-боты мессенджеров ходили по ссылке-корзине GET-запросом,
    # молча удаляя покупателей (JS-confirm их не останавливает). Это и была
    # причина "покупатели отваливаются сами по чуть-чуть".
    db = get_db()
    db.execute("DELETE FROM buyers WHERE id = ?", (buyer_id,))
    db.commit()
    db.close()
    return RedirectResponse(url="/buyers", status_code=303)


# === Доставки (Яндекс): получатели из листа «Покупатели» + выбор ПВЗ ===
# Хендлеры СИНХРОННЫЕ (def): чтение гуглшита и запросы к Яндексу блокирующие и
# небыстрые — FastAPI выполнит их в пуле потоков, не блокируя остальные запросы.
import buyers_sheet
from yandex_delivery import YandexDeliveryClient
from yandex_delivery.errors import YandexDeliveryError


@app.get("/dostavka", response_class=HTMLResponse)
def dostavka_list(request: Request):
    # закупки — для перехода к массовой доставке по каждой
    db = get_db()
    zakupkas = [row_to_dict(z) for z in db.execute(
        "SELECT id, name, status FROM zakupkas ORDER BY status, created_at DESC"
    ).fetchall()]
    db.close()

    error = None
    recipients = []
    try:
        recipients = buyers_sheet.list_recipients()
    except FileNotFoundError as e:
        error = f"Не найден ключ сервисного аккаунта: {e}"
    except Exception as e:
        error = f"Не удалось прочитать лист «Покупатели»: {e}"
    ready = sum(1 for r in recipients if r["delivery_ready"])
    return templates.TemplateResponse("dostavka.html", {
        "request": request,
        "recipients": recipients,
        "ready": ready,
        "total": len(recipients),
        "error": error,
        "zakupkas": zakupkas,
        "origin_pvz_address": get_setting("origin_pvz_address", ""),
        "origin_pvz_id": get_setting("origin_pvz_id", ""),
    })


@app.post("/dostavka/origin")
def dostavka_set_origin(pvz_id: str = Form(""), pvz_address: str = Form("")):
    """Сохранить ПВЗ отправления (точка А) в настройки."""
    set_setting("origin_pvz_id", pvz_id.strip())
    set_setting("origin_pvz_address", pvz_address.strip())
    return RedirectResponse(url="/dostavka", status_code=303)


@app.post("/dostavka/fio")
def dostavka_set_fio(
    phone: str = Form(...),
    last_name: str = Form(""),
    first_name: str = Form(""),
    patronymic: str = Form(""),
    city: str = Form(""),
):
    try:
        buyers_sheet.set_fio(phone, last_name, first_name, patronymic)
        if city.strip():
            buyers_sheet.set_city(phone, city.strip())
    except Exception:
        pass
    return RedirectResponse(url="/dostavka", status_code=303)


@app.get("/dostavka/pvz")
def dostavka_pvz_search(city: str = "", limit: int = 30):
    """JSON-поиск ПВЗ по городу для пикера в модалке."""
    city = (city or "").strip()
    if not city:
        return JSONResponse({"ok": False, "error": "Укажите город"})
    try:
        c = YandexDeliveryClient()  # окружение из YANDEX_DELIVERY_ENV (по умолч. test)
        gid = c.geo_id(city)
        points = c.list_pickup_points(geo_id=gid)
        data = [{"id": p.id, "name": p.name, "address": p.full_address} for p in points[:limit]]
        return JSONResponse({"ok": True, "env": c.env, "count": len(points), "points": data})
    except YandexDeliveryError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/dostavka/pvz")
def dostavka_set_pvz(
    phone: str = Form(...),
    pvz_address: str = Form(""),
    pvz_id: str = Form(""),
):
    try:
        buyers_sheet.set_pvz(phone, pvz_address, pvz_id)
    except Exception:
        pass
    return RedirectResponse(url="/dostavka", status_code=303)


def _delivery_block_reason(phone, rec):
    """Короткая причина, почему покупатель не готов к доставке (или '')."""
    if not phone:
        return "нет привязки телефона (страница «Покупатели»)"
    if rec is None:
        return "телефон не найден в листе «Покупатели»"
    if not (rec.get("first_name") or rec.get("last_name")):
        return "не заполнено ФИО получателя"
    if not rec.get("pvz_id"):
        return "не выбран ПВЗ"
    return ""


@app.get("/dostavka/zakupka/{zakupka_id}", response_class=HTMLResponse)
def dostavka_zakupka(request: Request, zakupka_id: int):
    """Превью массовой доставки по закупке: покупатель → телефон → получатель
    из листа → расчёт посылки (вес/коробка) → готовность. Без вызовов API."""
    from yandex_delivery import parcel

    db = get_db()
    zakupka = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zakupka:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    items = db.execute(
        "SELECT buyer_name, aroma_name, volume_ml, total_sum "
        "FROM zakaz_items WHERE zakupka_id = ? ORDER BY buyer_name",
        (zakupka_id,),
    ).fetchall()
    phones = {}
    for b in db.execute("SELECT name, phone FROM buyers").fetchall():
        phones[b["name"]] = buyers_sheet.normalize_phone(b["phone"] or "")
    db.close()

    # получателей из листа читаем ОДИН раз, кладём в словарь по телефону
    recips, sheet_error = {}, None
    try:
        for r in buyers_sheet.list_recipients():
            recips[r["phone"]] = r
    except Exception as e:
        sheet_error = str(e)

    by_buyer = {}
    for it in items:
        by_buyer.setdefault(it["buyer_name"], []).append(it)

    rows = []
    for buyer_name, its in by_buyer.items():
        phone = phones.get(buyer_name, "")
        rec = recips.get(phone) if phone else None
        lines = [
            parcel.ParcelLine(
                it["aroma_name"], it["volume_ml"], 1,
                int(round((it["total_sum"] or 0) * 100)),
            )
            for it in its
        ]
        calc = parcel.calc(lines, barcode=f"Z{zakupka_id}-{phone or buyer_name}")
        reason = _delivery_block_reason(phone, rec)
        rows.append({
            "buyer_name": buyer_name,
            "phone": phone,
            "fio": rec["fio"] if rec else "",
            "pvz_address": rec["pvz_address"] if rec else "",
            "positions": len(its),
            "weight_g": calc.weight_g,
            "box": calc.box.code,
            "ready": (reason == ""),
            "reason": reason,
        })
    rows.sort(key=lambda x: (not x["ready"], x["buyer_name"].lower()))
    ready_count = sum(1 for r in rows if r["ready"])

    return templates.TemplateResponse("dostavka_zakupka.html", {
        "request": request,
        "zakupka": row_to_dict(zakupka),
        "rows": rows,
        "ready_count": ready_count,
        "total": len(rows),
        "sheet_error": sheet_error,
    })


# === Заказы с наличия ===
@app.get("/nalichie/new", response_class=HTMLResponse)
async def new_nalichie_form(request: Request):
    db = get_db()
    buyers_raw = db.execute("SELECT * FROM buyers ORDER BY name").fetchall()
    buyers = [row_to_dict(b) for b in buyers_raw]
    zakupkas_raw = db.execute(
        "SELECT id, name FROM zakupkas WHERE status = 'active' ORDER BY created_at DESC"
    ).fetchall()
    zakupkas = [row_to_dict(z) for z in zakupkas_raw]
    db.close()
    return templates.TemplateResponse("nalichie_new.html", {
        "request": request,
        "buyers": buyers,
        "zakupkas": zakupkas
    })


@app.post("/nalichie/new")
async def create_nalichie_order(
        request: Request,
        buyer_name: str = Form(...),
        aroma_name: str = Form(...),
        volume_ml: int = Form(...),
        price: float = Form(...),
        zakupka_id: str = Form("none")
):
    db = get_db()

    zakupka_id_val = None if zakupka_id == "none" else int(zakupka_id)

    db.execute(
        "INSERT INTO nalichie_orders (zakupka_id, buyer_name, aroma_name, volume_ml, price, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (zakupka_id_val, buyer_name.strip(), aroma_name.strip(), volume_ml, price,
         datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    db.commit()
    db.close()

    return RedirectResponse(url="/", status_code=303)


# === Массовое добавление заказов с наличия (список текстом) ===
@app.post("/nalichie/new-bulk")
async def create_nalichie_bulk(
        request: Request,
        bulk_text: str = Form(...),
        zakupka_id: str = Form("none")
):
    db = None
    try:
        zakupka_id_val = None if zakupka_id == "none" else int(zakupka_id)

        added = 0
        errors = []

        db = get_db()

        # Разбираем построчно
        lines = bulk_text.splitlines()
        for line_num, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue  # пустые строки пропускаем

            # Разделитель — точка с запятой; если её нет, пробуем запятую
            if ";" in line:
                parts = [p.strip() for p in line.split(";")]
            else:
                parts = [p.strip() for p in line.split(",")]

            if len(parts) < 4:
                errors.append(f"Строка {line_num}: нужно 4 поля (Покупатель; Аромат; Объём; Цена) — «{line}»")
                continue

            buyer_name = parts[0]
            aroma_name = parts[1]

            # Объём — берём только цифры
            volume_digits = ''.join(filter(str.isdigit, parts[2]))
            if not volume_digits:
                errors.append(f"Строка {line_num}: не понял объём «{parts[2]}»")
                continue
            volume_ml = int(volume_digits)

            # Цена — убираем пробелы, запятую делаем точкой
            price_str = parts[3].replace(" ", "").replace(",", ".")
            try:
                price = float(price_str)
            except ValueError:
                errors.append(f"Строка {line_num}: не понял цену «{parts[3]}»")
                continue

            if not buyer_name or not aroma_name:
                errors.append(f"Строка {line_num}: пустое имя покупателя или аромата")
                continue

            # Добавляем заказ
            db.execute(
                "INSERT INTO nalichie_orders (zakupka_id, buyer_name, aroma_name, volume_ml, price, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (zakupka_id_val, buyer_name, aroma_name, volume_ml, price,
                 datetime.now().strftime("%Y-%m-%d %H:%M"))
            )

            # Если покупателя нет в базе — заводим
            existing = db.execute("SELECT id FROM buyers WHERE name = ?", (buyer_name,)).fetchone()
            if not existing:
                db.execute("INSERT INTO buyers (name) VALUES (?)", (buyer_name,))

            added += 1

        db.commit()
        db.close()
        db = None

        # Если были ошибки в отдельных строках — показываем их, но добавленное сохранено
        if errors:
            # Заново готовим данные для формы
            db2 = get_db()
            buyers_raw = db2.execute("SELECT * FROM buyers ORDER BY name").fetchall()
            buyers = [row_to_dict(b) for b in buyers_raw]
            zakupkas_raw = db2.execute(
                "SELECT id, name FROM zakupkas WHERE status = 'active' ORDER BY created_at DESC"
            ).fetchall()
            zakupkas = [row_to_dict(z) for z in zakupkas_raw]
            db2.close()

            error_msg = f"Добавлено заказов: {added}. Не удалось обработать строки:\n" + "\n".join(errors)
            return templates.TemplateResponse("nalichie_new.html", {
                "request": request,
                "buyers": buyers,
                "zakupkas": zakupkas,
                "error": error_msg
            })

        return RedirectResponse(url="/nalichie", status_code=303)

    except Exception as e:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        # Готовим данные для формы, чтобы показать ошибку
        db2 = get_db()
        buyers_raw = db2.execute("SELECT * FROM buyers ORDER BY name").fetchall()
        buyers = [row_to_dict(b) for b in buyers_raw]
        zakupkas_raw = db2.execute(
            "SELECT id, name FROM zakupkas WHERE status = 'active' ORDER BY created_at DESC"
        ).fetchall()
        zakupkas = [row_to_dict(z) for z in zakupkas_raw]
        db2.close()
        return templates.TemplateResponse("nalichie_new.html", {
            "request": request,
            "buyers": buyers,
            "zakupkas": zakupkas,
            "error": f"Ошибка при добавлении списка: {str(e)}"
        })


@app.get("/nalichie", response_class=HTMLResponse)
async def nalichie_list(request: Request):
    db = get_db()
    orders_raw = db.execute("""
        SELECT 
            no.*,
            COALESCE(z.name, 'Свободный заказ') as zakupka_name,
            COALESCE(s.upakovka, 0) as upakovka,
            COALESCE(s.payment_nalichie, 0) as payment_nalichie
        FROM nalichie_orders no
        LEFT JOIN zakupkas z ON no.zakupka_id = z.id
        LEFT JOIN statuses s ON s.nalichie_order_id = no.id
        ORDER BY no.created_at DESC
    """).fetchall()
    orders = [row_to_dict(o) for o in orders_raw]
    db.close()
    return templates.TemplateResponse("nalichie_list.html", {
        "request": request,
        "orders": orders
    })


# === Редактирование заказа наличия ===
@app.get("/nalichie/edit/{order_id}", response_class=HTMLResponse)
async def edit_nalichie_form(request: Request, order_id: int):
    db = get_db()
    order = db.execute("SELECT * FROM nalichie_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        raise HTTPException(status_code=404, detail="Заказ не найден")

    buyers_raw = db.execute("SELECT * FROM buyers ORDER BY name").fetchall()
    buyers = [row_to_dict(b) for b in buyers_raw]
    zakupkas_raw = db.execute("SELECT id, name FROM zakupkas ORDER BY created_at DESC").fetchall()
    zakupkas = [row_to_dict(z) for z in zakupkas_raw]

    db.close()
    return templates.TemplateResponse("nalichie_edit.html", {
        "request": request,
        "order": row_to_dict(order),
        "buyers": buyers,
        "zakupkas": zakupkas
    })


@app.post("/nalichie/edit/{order_id}")
async def update_nalichie_order(
        order_id: int,
        buyer_name: str = Form(...),
        aroma_name: str = Form(...),
        volume_ml: int = Form(...),
        price: float = Form(...),
        zakupka_id: str = Form("none")
):
    db = get_db()
    zakupka_id_val = None if zakupka_id == "none" else int(zakupka_id)

    db.execute(
        "UPDATE nalichie_orders SET zakupka_id = ?, buyer_name = ?, aroma_name = ?, volume_ml = ?, price = ? WHERE id = ?",
        (zakupka_id_val, buyer_name.strip(), aroma_name.strip(), volume_ml, price, order_id)
    )
    db.commit()
    db.close()
    return RedirectResponse(url="/nalichie", status_code=303)


@app.post("/nalichie/delete/{order_id}")
async def delete_nalichie_order(order_id: int):
    # Только POST — та же причина, что и с покупателями (боты дёргали GET-ссылку).
    db = get_db()
    # Удаляем связанные статусы (если есть)
    db.execute("DELETE FROM statuses WHERE nalichie_order_id = ?", (order_id,))
    db.execute("DELETE FROM nalichie_orders WHERE id = ?", (order_id,))
    db.commit()
    db.close()
    return RedirectResponse(url="/nalichie", status_code=303)

# ============================================================
# СЛУЖЕБНЫЕ СТРАНИЦЫ + СЛЕДОПЫТ (временные, удалить после отладки)
# ============================================================
import time as _time_module
from models import DB_PATH as _DB_PATH

_PROCESS_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_WATCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_watch.log")


def _snapshot(tag=""):
    """Пишет строку состояния базы в отдельный лог-файл.
    Лог живёт отдельно от data.db, поэтому переживёт любой откат базы."""
    try:
        line = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tag": tag}
        # inode и размер файла базы
        try:
            st = os.stat(_DB_PATH)
            line["inode"] = st.st_ino
            line["size"] = st.st_size
            line["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            line["stat_err"] = str(e)
        # счётчики
        try:
            db = get_db()
            for t in ["buyers", "nalichie_orders", "zakaz_items", "statuses", "zakupkas"]:
                line[f"cnt_{t}"] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            seqs = dict(db.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
            for t in ["buyers", "nalichie_orders", "zakaz_items", "statuses"]:
                line[f"seq_{t}"] = seqs.get(t, "-")
            db.close()
        except Exception as e:
            line["db_err"] = str(e)
        with open(_WATCH_LOG, "a", encoding="utf-8") as f:
            import json
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass  # логгер никогда не должен ронять приложение


@app.get("/admin/watchlog", response_class=HTMLResponse)
async def admin_watchlog(request: Request):
    """Показывает журнал наблюдений: как менялись счётчики и inode во времени."""
    _snapshot("watchlog_open")
    rows = []
    try:
        import json
        with open(_WATCH_LOG, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    except FileNotFoundError:
        rows = []
    rows = rows[-200:]  # последние 200 записей

    header = ["ts", "tag", "inode", "size", "cnt_buyers", "seq_buyers",
              "cnt_nalichie_orders", "seq_nalichie_orders", "cnt_zakaz_items", "cnt_statuses"]
    th = "".join(f"<th>{h}</th>" for h in header)
    trs = ""
    prev_inode = None
    for r in rows:
        inode = r.get("inode")
        # подсветим строку, если inode сменился или счётчики упали
        style = ""
        if prev_inode is not None and inode != prev_inode:
            style = ' style="background:#ffe0b2;"'  # оранжевый — файл подменён
        prev_inode = inode
        tds = "".join(f"<td>{r.get(h,'')}</td>" for h in header)
        trs += f"<tr{style}>{tds}</tr>"

    html = f"""
    <!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Журнал наблюдений</title>
    <style>body{{font-family:monospace;margin:20px;font-size:12px;}}
    table{{border-collapse:collapse;}} td,th{{border:1px solid #ccc;padding:3px 7px;}}
    th{{background:#f0f0f0;position:sticky;top:0;}}
    .leg{{font-family:sans-serif;font-size:13px;margin-bottom:10px;}}</style></head><body>
    <h2>Журнал наблюдений за базой</h2>
    <div class="leg">
        Оранжевая строка = у файла data.db сменился <b>inode</b> (файл физически подменили — заливка/восстановление).<br>
        Если inode стабилен, а <b>cnt_buyers</b> упал в 0 при высоком <b>seq_buyers</b> — значит данные удаляли внутри того же файла.<br>
        Запись добавляется при каждом заходе на главную и на служебные страницы.
    </div>
    <table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>
    <p style="font-family:sans-serif;"><a href="/admin/watchlog">🔄 Обновить</a></p>
    </body></html>
    """
    return HTMLResponse(content=html)


@app.get("/admin/debug", response_class=HTMLResponse)
async def admin_debug(request: Request):
    _snapshot("debug_open")
    db_path = _DB_PATH
    exists = os.path.exists(db_path)
    if exists:
        st = os.stat(db_path)
        size_bytes = st.st_size
        inode = st.st_ino
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    else:
        size_bytes = 0; inode = "-"; mtime = "— файла нет —"
    real_path = os.path.realpath(db_path)
    cwd = os.getcwd()

    db = get_db()
    counts = {}
    for table in ["buyers", "zakupkas", "zakaz_items", "nalichie_orders", "statuses"]:
        try:
            counts[table] = db.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
        except Exception as e:
            counts[table] = f"ошибка: {e}"
    try:
        seqs = dict(db.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
    except Exception:
        seqs = {}
    try:
        last_orders = db.execute("SELECT id, buyer_name, aroma_name, created_at FROM nalichie_orders ORDER BY id DESC LIMIT 5").fetchall()
        last_orders = [row_to_dict(o) for o in last_orders]
    except Exception:
        last_orders = []
    db.close()

    project_dir = os.path.dirname(real_path)
    db_files = []
    try:
        for f in os.listdir(project_dir):
            if f.endswith(".db") or ".db-" in f:
                full = os.path.join(project_dir, f)
                db_files.append(f"{f} — {os.path.getsize(full)} байт — изменён {datetime.fromtimestamp(os.path.getmtime(full)).strftime('%H:%M:%S')}")
    except Exception as e:
        db_files.append(f"ошибка: {e}")

    counts_html = "".join(f"<tr><td>{k}</td><td class='num'>{v}</td><td class='seq'>seq={seqs.get(k,'-')}</td></tr>" for k, v in counts.items())
    orders_html = "".join(f"<li>#{o['id']} — {o['buyer_name']} — {o['aroma_name']} — {o.get('created_at','')}</li>" for o in last_orders) or "<li>— заказов нет —</li>"
    dbfiles_html = "".join(f"<li>{f}</li>" for f in db_files) or "<li>— нет —</li>"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""
    <!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Диагностика</title>
    <style>body{{font-family:monospace;max-width:820px;margin:30px auto;padding:0 16px;font-size:14px;}}
    h2,h3{{font-family:sans-serif;}} .box{{border:1px solid #ccc;border-radius:8px;padding:12px 16px;margin-bottom:14px;}}
    table{{border-collapse:collapse;width:100%;}} td{{padding:4px 10px;border-bottom:1px solid #eee;}}
    .num{{font-weight:bold;text-align:right;font-size:18px;}} .seq{{color:#888;}} .key{{color:#666;}} .big{{font-size:16px;}}</style></head><body>
    <h2>🔍 Диагностика базы</h2>
    <div class="box">
        <div><span class="key">Сейчас на сервере:</span> <b>{now}</b></div>
        <div><span class="key">Процесс запущен в:</span> <b>{_PROCESS_STARTED_AT}</b></div>
        <div><span class="key">inode файла базы:</span> <b>{inode}</b> (если менялся — файл подменяли)</div>
    </div>
    <div class="box"><h3>Файл базы</h3>
        <div class="big"><b>{db_path}</b></div>
        <div><span class="key">realpath:</span> {real_path}</div>
        <div><span class="key">cwd:</span> {cwd}</div>
        <div><span class="key">размер:</span> {size_bytes} байт · <span class="key">изменён:</span> <b>{mtime}</b></div>
    </div>
    <div class="box"><h3>Строк сейчас (и seq — сколько прошло за всю историю)</h3>
        <table>{counts_html}</table>
        <div style="color:#a00;margin-top:8px;">seq &gt; cnt означает, что записи БЫЛИ и были удалены.</div>
    </div>
    <div class="box"><h3>Последние заказы наличия</h3><ul>{orders_html}</ul></div>
    <div class="box"><h3>Все .db-файлы в папке</h3><ul>{dbfiles_html}</ul></div>
    <p style="font-family:sans-serif;"><a href="/admin/debug">🔄 Обновить</a> · <a href="/admin/watchlog">📊 Журнал наблюдений</a></p>
    </body></html>
    """
    return HTMLResponse(content=html)


# --- Перенос покупателей из закупок в справочник ---
@app.get("/admin/pochinit", response_class=HTMLResponse)
async def pochinit_view(request: Request):
    db = get_db()
    buyers_count = db.execute("SELECT COUNT(*) as c FROM buyers").fetchone()["c"]
    items_buyers_count = db.execute("SELECT COUNT(DISTINCT buyer_name) as c FROM zakaz_items").fetchone()["c"]
    missing_raw = db.execute("""SELECT DISTINCT buyer_name FROM zakaz_items WHERE buyer_name NOT IN (SELECT name FROM buyers) ORDER BY buyer_name""").fetchall()
    missing = [row_to_dict(m)["buyer_name"] for m in missing_raw]
    db.close()
    rows_html = "".join(f"<li>{name}</li>" for name in missing) or "<li>— всё на месте —</li>"
    html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Перенос покупателей</title>
    <style>body{{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;}}
    .num{{font-size:32px;font-weight:bold;}} .box{{border:1px solid #ccc;border-radius:8px;padding:16px;margin-bottom:16px;}}
    button{{font-size:16px;padding:10px 20px;border:none;border-radius:6px;background:#0d6efd;color:#fff;cursor:pointer;}}
    ul{{columns:2;}}</style></head><body>
    <h2>Перенос покупателей в справочник</h2>
    <div class="box"><div>Сейчас в справочнике:</div><div class="num">{buyers_count}</div></div>
    <div class="box"><div>Уникальных имён в закупках:</div><div class="num">{items_buyers_count}</div></div>
    <div class="box"><div>Не хватает: <b>{len(missing)}</b></div><ul>{rows_html}</ul></div>
    <form method="POST" action="/admin/pochinit"><button type="submit">Перенести {len(missing)} покупателей</button></form>
    <p><a href="/admin/debug">→ Диагностика</a></p></body></html>"""
    return HTMLResponse(content=html)


@app.post("/admin/pochinit", response_class=HTMLResponse)
async def pochinit_apply(request: Request):
    db = get_db()
    missing_raw = db.execute("""SELECT DISTINCT buyer_name FROM zakaz_items WHERE buyer_name NOT IN (SELECT name FROM buyers) ORDER BY buyer_name""").fetchall()
    missing = [row_to_dict(m)["buyer_name"] for m in missing_raw]
    added = 0
    for name in missing:
        db.execute("INSERT INTO buyers (name) VALUES (?)", (name,))
        added += 1
    db.commit()
    db.close()
    _snapshot("pochinit_applied")
    html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Готово</title>
    <style>body{{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;}} a{{color:#0d6efd;}}</style></head><body>
    <h2>Готово ✅</h2><p>Добавлено: <b>{added}</b></p>
    <p><a href="/admin/debug">→ Диагностика</a> · <a href="/admin/watchlog">→ Журнал</a></p></body></html>"""
    return HTMLResponse(content=html)


# === Запуск приложения ===
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)