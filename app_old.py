from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import pandas as pd
from urllib.parse import urlparse, parse_qs
import sqlite3
from datetime import datetime
import os

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

from models import get_db, init_db

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
async def buyers_list(request: Request):
    db = get_db()
    buyers_raw = db.execute("SELECT * FROM buyers ORDER BY name").fetchall()
    buyers = [row_to_dict(b) for b in buyers_raw]
    db.close()
    return templates.TemplateResponse("buyers.html", {
        "request": request,
        "buyers": buyers
    })


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


@app.get("/buyers/delete/{buyer_id}")
async def delete_buyer(buyer_id: int):
    db = get_db()
    db.execute("DELETE FROM buyers WHERE id = ?", (buyer_id,))
    db.commit()
    db.close()
    return RedirectResponse(url="/buyers", status_code=303)


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


@app.get("/nalichie/delete/{order_id}")
async def delete_nalichie_order(order_id: int):
    db = get_db()
    # Удаляем связанные статусы (если есть)
    db.execute("DELETE FROM statuses WHERE nalichie_order_id = ?", (order_id,))
    db.execute("DELETE FROM nalichie_orders WHERE id = ?", (order_id,))
    db.commit()
    db.close()
    return RedirectResponse(url="/nalichie", status_code=303)

# === Запуск приложения ===
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)