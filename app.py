from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import pandas as pd
from urllib.parse import urlparse, parse_qs
import sqlite3
from datetime import datetime
from typing import List
import os

app = FastAPI()
from catalog_api import setup_catalog; setup_catalog(app)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

from models import get_db, init_db, get_setting, set_setting
import dash_auth

dash_auth.init()

# ================== Вход: каждый запрос — только после логина ==================
# Открыто без входа: страница входа/первой настройки, статика и каталог для сайта
# betweenatelier.com (catalog_api: /catalog и /assets/...).
_PUBLIC_EXACT = {"/login", "/logout", "/setup", "/catalog", "/favicon.ico", "/sw.js"}
_PUBLIC_PREFIX = ("/static/", "/assets/")


def _is_admin(request):
    u = getattr(request.state, "user", None)
    return bool(u and u["role"] == dash_auth.ROLE_ADMIN)


templates.env.globals["is_admin"] = _is_admin
templates.env.globals["ROLE_LABELS"] = dash_auth.ROLE_LABELS


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX) or request.method == "OPTIONS":
        return await call_next(request)
    if not dash_auth.enabled():            # вход выключен — как раньше, все организаторы
        if path.startswith("/users"):      # но учётки заводить нельзя, пока дверь открыта
            return HTMLResponse("<div style='font-family:system-ui;padding:40px;text-align:center'>"
                                "<h3>Вход сейчас выключен</h3><p>Пользователи настраиваются после включения входа.</p>"
                                "<p><a href='/'>← На главную</a></p></div>", status_code=403)
        request.state.user = dash_auth.GUEST_ADMIN
        return await call_next(request)
    from urllib.parse import quote
    user = dash_auth.user_from_cookie(request.cookies.get(dash_auth.COOKIE, ""))
    wants_html = request.method in ("GET", "HEAD") and not path.startswith("/api/")
    if not user:
        if wants_html:
            return RedirectResponse(url=f"/login?next={quote(path)}", status_code=303)
        return JSONResponse({"ok": False, "error": "Нужно войти заново"}, status_code=401)
    if not dash_auth.allowed(user, request.method, path):
        if wants_html:
            return HTMLResponse("<div style='font-family:system-ui;padding:40px;text-align:center'>"
                                "<h3>Нет доступа</h3><p>Этот раздел доступен только организатору.</p>"
                                "<p><a href='/'>← На главную</a></p></div>", status_code=403)
        return JSONResponse({"ok": False, "error": "Недоступно для вашей роли"}, status_code=403)
    request.state.user = user
    return await call_next(request)


def _client_ip(request):
    return request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
        or (request.client.host if request.client else "")


def _safe_next(nxt):
    return nxt if (nxt or "").startswith("/") and not nxt.startswith("//") else "/"


def _login_cookie(resp, user, request):
    # за nginx по https — кука только для https; локально (http) — без флага, иначе вход не работает
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(dash_auth.COOKIE, dash_auth.make_session(user), max_age=dash_auth.SESSION_DAYS * 86400,
                    httponly=True, secure=https, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", err: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "next": _safe_next(next), "err": err})


@app.post("/login")
def login_submit(request: Request, login: str = Form(""), password: str = Form(""), next: str = Form("/")):
    from urllib.parse import quote
    ip = _client_ip(request)
    if dash_auth.blocked(ip):
        return RedirectResponse(url=f"/login?err={quote('Слишком много попыток. Подожди 10 минут.')}", status_code=303)
    u = dash_auth.find_login(login)
    if not u or not u["active"] or not dash_auth.verify_password(password, u["pass_hash"]):
        dash_auth.note_fail(ip)
        return RedirectResponse(url=f"/login?next={quote(_safe_next(next))}&err={quote('Неверный логин или пароль')}",
                                status_code=303)
    dash_auth.clear_fails(ip)
    return _login_cookie(RedirectResponse(url=_safe_next(next), status_code=303), u, request)


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(dash_auth.COOKIE)
    return resp


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, code: str = "", err: str = ""):
    ok = dash_auth.check_setup_code(code)
    return templates.TemplateResponse("setup.html", {"request": request, "code": code, "ok": ok, "err": err})


@app.post("/setup")
def setup_submit(request: Request, code: str = Form(""), login: str = Form(""), name: str = Form(""),
                 password: str = Form(""), password2: str = Form("")):
    """Организатор по одноразовому коду сама задаёт логин и пароль (или сбрасывает свой)."""
    from urllib.parse import quote
    back = lambda m: RedirectResponse(url=f"/setup?code={quote(code)}&err={quote(m)}", status_code=303)
    ip = _client_ip(request)
    if dash_auth.blocked(ip):
        return back("Слишком много попыток. Подожди 10 минут.")
    if not dash_auth.check_setup_code(code):
        dash_auth.note_fail(ip)
        return back("Код недействителен или устарел.")
    if password != password2:
        return back("Пароли не совпадают.")
    existing = dash_auth.find_login(login)
    if existing:
        if existing["role"] != dash_auth.ROLE_ADMIN:
            return back("Этот логин занят не организатором.")
        err = dash_auth.set_password(existing["id"], password)
        dash_auth.set_active(existing["id"], True)
    else:
        err = dash_auth.create_user(login, name, dash_auth.ROLE_ADMIN, password)
    if err:
        return back(err)
    dash_auth.burn_setup_code()
    return _login_cookie(RedirectResponse(url="/", status_code=303), dash_auth.find_login(login), request)


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, msg: str = "", err: str = ""):
    return templates.TemplateResponse("users.html", {"request": request, "users": dash_auth.list_users(),
                                                     "msg": msg, "err": err})


@app.post("/users/add")
def users_add(login: str = Form(""), name: str = Form(""), role: str = Form("supplier"), password: str = Form("")):
    from urllib.parse import quote
    err = dash_auth.create_user(login, name, role, password)
    q = f"err={quote(err)}" if err else f"msg={quote('Добавлен: ' + dash_auth.clean_login(login))}"
    return RedirectResponse(url=f"/users?{q}", status_code=303)


@app.post("/users/{uid}/password")
def users_password(uid: int, password: str = Form("")):
    from urllib.parse import quote
    err = dash_auth.set_password(uid, password)
    q = f"err={quote(err)}" if err else f"msg={quote('Пароль изменён — старые входы этого пользователя погашены.')}"
    return RedirectResponse(url=f"/users?{q}", status_code=303)


@app.post("/users/{uid}/active")
def users_active(request: Request, uid: int, active: int = Form(0)):
    from urllib.parse import quote
    me = request.state.user
    u = dash_auth.get_user(uid) if active == 0 else None
    if not active and uid == me["id"]:
        return RedirectResponse(url=f"/users?err={quote('Себя отключить нельзя.')}", status_code=303)
    if not active and u and u["role"] == dash_auth.ROLE_ADMIN and dash_auth.admins_count() <= 1:
        return RedirectResponse(url=f"/users?err={quote('Нельзя отключить последнего организатора.')}", status_code=303)
    dash_auth.set_active(uid, bool(active))
    return RedirectResponse(url="/users", status_code=303)

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
async def new_zakupka_form(request: Request, err: str = ""):
    return templates.TemplateResponse("zakupka_new.html", {"request": request, "error": err or None})


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
async def view_zakupka(request: Request, zakupka_id: int, paymsg: str = ""):
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
            s.rozliv,
            COALESCE(zi.is_piece, 0) as is_piece
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
            s.upakovka,
            s.rozliv,
            COALESCE(zi.is_piece, 0) as is_piece
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
            COALESCE(s.upakovka, 0) as upakovka,
            1 as rozliv,
            0 as is_piece
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

    # счёт: доставка (Яндекс за наш счёт), итого, способ оплаты (ссылка/карта)
    import billing
    inv_by_buyer = {i["buyer"]: i for i in billing.invoices(db, zakupka_id)}
    for buyer_name, data in buyers_summary.items():
        inv = inv_by_buyer.get(buyer_name)
        data["delivery"] = inv["delivery"] if inv else 0
        data["delivery_auto"] = inv["delivery_auto"] if inv else 0
        data["delivery_manual"] = inv["delivery_manual"] if inv else None
        data["bill_total"] = inv["total"] if inv else 0
        data["method"] = inv["method"] if inv else billing.METHOD_LINK
        data["payto"] = inv["payto"] if inv else ""
        data["payto_own"] = inv["payto_own"] if inv else ""

    buyers_summary_sorted = sorted(buyers_summary.items(), key=lambda x: x[0])

    total_buyers = len(buyers_summary_sorted)
    paid_zakupka = sum(1 for _, data in buyers_summary_sorted if data["payment_zakupka"] == 1)
    shipped_count = sum(1 for _, data in buyers_summary_sorted if data["shipped"] == 1)

    payment_zakupka_percent = int((paid_zakupka / total_buyers * 100)) if total_buyers > 0 else 0
    shipped_percent = int((shipped_count / total_buyers * 100)) if total_buyers > 0 else 0

    # === СОСТАВ (редактируемые позиции закупки) ===
    sostav_items = [row_to_dict(r) for r in db.execute(
        "SELECT id, buyer_name, aroma_name, volume_ml, price_per_10ml, total_sum, "
        "COALESCE(ext_gone, 0) AS ext_gone "
        "FROM zakaz_items WHERE zakupka_id = ? ORDER BY buyer_name, aroma_name",
        (zakupka_id,),
    ).fetchall()]

    db.close()

    return templates.TemplateResponse(
        "zakupka.html",
        {
            "request": request,
            "zakupka": zakupka_dict,
            "sostav_items": sostav_items,
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
            "shipped_percent": shipped_percent,
            "paymsg": paymsg,
            "has_nalichie": any((d.get("sum_nalichie") or 0) > 0 for _, d in buyers_summary_sorted),
            "payto_default": billing.get_payto_default(),
            "settings_info": _settings_info(zakupka_id),
        }
    )


PAY_SECTION_LINK = "ПО ССЫЛКЕ — впишите ссылку на оплату"
PAY_SECTION_CARD = "НА КАРТУ — ссылка не нужна (можно перенести сюда строку сверху)"
PAY_SECTION_PAID = "УЖЕ ОПЛАЧЕНО — для сведения, при загрузке не читается"


@app.get("/zakupka/{zakupka_id}/pay-export")
def pay_export(zakupka_id: int):
    """Excel для поставщика: сверху — кто платит по ссылке (колонка для ссылки),
    ниже — кто платит на карту, в конце — уже оплатившие. Закупка · Доставка · Итого."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    import billing

    db = get_db()
    zak = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zak:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    db.close()
    try:
        inv = _vitrina_invoices(zakupka_id)
    except Exception as e:
        from urllib.parse import quote
        return RedirectResponse(url=f"/zakupka/{zakupka_id}?paymsg={quote(str(e))}#oplata", status_code=303)
    try:
        recips = {r["phone"]: r for r in buyers_sheet.list_recipients()}
    except Exception:
        recips = {}

    wb = Workbook()
    ws = wb.active
    ws.title = "Оплата"
    _xlsx_header(ws, ["Телефон", "Имя Фамилия", "E-mail", "Закупка, ₽", "Доставка, ₽",
                      "Итого к оплате, ₽", "Ссылка на оплату"], [16, 26, 28, 12, 12, 16, 44])

    def section(title, color, items):
        ws.append([title])
        ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=7)
        c = ws.cell(row=ws.max_row, column=1)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor=color)
        for it in items:
            rec = recips.get(it["phone"]) if it["phone"] else None
            fio = (((rec.get("first_name", "") + " " + rec.get("last_name", "")).strip() if rec else "")
                   or it["buyer"].split(" - ", 1)[-1])
            # у «на карту» в последней колонке — реквизиты (для сведения, ссылка не нужна)
            ws.append([it["phone"], fio, rec.get("email", "") if rec else "",
                       it["goods"], it["delivery"] or "", it["total"],
                       it["payto"] if it["method"] == billing.METHOD_CARD else ""])
            for cell in ws[ws.max_row]:
                cell.border = ws._thin_border

    section(PAY_SECTION_LINK, "E2EFDA", [i for i in inv if not i["paid"] and i["method"] == billing.METHOD_LINK])
    ws.append([])
    section(PAY_SECTION_CARD, "FCE4D6", [i for i in inv if not i["paid"] and i["method"] == billing.METHOD_CARD])
    ws.append([])
    section(PAY_SECTION_PAID, "EDEDED", [i for i in inv if i["paid"]])
    return _xlsx_response(wb, f"oplata_{zakupka_id}.xlsx")


def _vitrina_invoices(zakupka_id):
    """Счета закупки: сумма заказа — ИЗ ВИТРИНЫ (истина), доставка/способ/оплата — из
    дашборда. Витрина недоступна → исключение с понятным текстом (счёт не выставляем)."""
    import billing
    import vitrina_sync
    try:
        rows = vitrina_sync.fetch()["rows"]
    except Exception as e:
        raise RuntimeError(f"Не удалось взять суммы из витрины, счета не тронуты. {e}")
    db = get_db()
    inv = billing.invoices_from_vitrina(db, zakupka_id, rows)
    db.close()
    return inv


def _sheet_fields(it):
    """Поля счёта для листа «Покупатели» (без ссылки — её решает вызывающий)."""
    import billing
    return {"amount": it["total"], "delivery": it["delivery"] or "",
            "paid": buyers_sheet.PAID_MARK if it["paid"] else "",
            # реквизиты перевода — только тем, кто платит на карту
            "payto": it["payto"] if it["method"] == billing.METHOD_CARD else ""}


def _push_invoices_quiet(zakupka_id):
    try:
        _push_invoices(zakupka_id)
    except Exception as e:
        print(f"[счета→витрина] {e}")


def _push_one(zakupka_id, buyer):
    """Сразу отправить в витрину счёт ОДНОЙ девочки (после смены способа, реквизитов,
    доставки) — как галочка «оплачено». Сумма заказа — из витрины. Ошибки — в лог."""
    import billing
    try:
        inv = _vitrina_invoices(zakupka_id)
        db = get_db()
        phone = billing.phone_of(buyer, billing.phone_by_name_map(db))
        db.close()
        it = next((i for i in inv if i["buyer"] == buyer or (phone and i["phone"] == phone)), None)
        if not it or not it["phone"]:
            return
        u = _sheet_fields(it)
        if it["method"] == billing.METHOD_CARD:
            u["link"] = ""          # на карту — ссылки нет; по ссылке — ссылку не трогаем
        buyers_sheet.set_pay_row(it["phone"], u)
    except Exception as e:
        print(f"[счёт→витрина] {buyer}: {e}")


def _push_invoices(zakupka_id, links=None, clear_links=(), inv=None):
    """Отправить счета закупки в витрину (лист «Покупатели», Q:T): итого, доставка,
    честная отметка оплаты; ссылки — из links, у clear_links ссылка стирается."""
    import billing
    if inv is None:
        inv = _vitrina_invoices(zakupka_id)
    links = links or {}
    updates = {}
    for it in inv:
        if not it["phone"]:
            continue
        u = _sheet_fields(it)
        if it["phone"] in links:
            u["link"] = links[it["phone"]]
        elif it["phone"] in clear_links or it["method"] == billing.METHOD_CARD:
            u["link"] = ""
        updates[it["phone"]] = u
    return buyers_sheet.set_pay_fields_bulk(updates), len(inv)


@app.post("/zakupka/{zakupka_id}/pay-import")
def pay_import(zakupka_id: int, file: UploadFile = File(...)):
    """Загрузка Excel от поставщика. Блок «по ссылке» — ссылки в витрину; строки блока
    «на карту» — способ оплаты «на карту» (ссылки нет). Суммы заказа — из витрины
    (там истина), доставка — из дашборда; из файла суммы не читаем."""
    import io
    from urllib.parse import quote
    from openpyxl import load_workbook
    import billing

    def _back(msg):
        return RedirectResponse(url=f"/zakupka/{zakupka_id}?paymsg={quote(msg)}#oplata", status_code=303)

    try:
        wb = load_workbook(io.BytesIO(file.file.read()), read_only=True, data_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
    except Exception as e:
        return _back(f"Не удалось прочитать файл: {e}")
    if not rows:
        return _back("Файл пустой.")

    header = [str(c or "").strip().lower() for c in rows[0]]
    col_phone = next((i for i, h in enumerate(header) if "телефон" in h or "phone" in h), None)
    col_link = next((i for i, h in enumerate(header) if "ссылк" in h or "link" in h), None)
    if col_phone is None or col_link is None:
        return _back("В файле нет колонок «Телефон» и «Ссылка на оплату».")

    links, card_phones, link_phones = {}, set(), set()
    mode = "link"   # старые файлы без разделов — всё считается «по ссылке»
    for r in rows[1:]:
        first = str(r[0] or "").strip().upper() if r else ""
        if first.startswith("ПО ССЫЛКЕ"):
            mode = "link"; continue
        if first.startswith("НА КАРТУ"):
            mode = "card"; continue
        if first.startswith("УЖЕ ОПЛАЧЕНО"):
            mode = "skip"; continue
        if mode == "skip" or col_phone >= len(r):
            continue
        ph = buyers_sheet.normalize_phone(r[col_phone] or "")
        if not buyers_sheet.valid_phone(ph):
            continue
        if mode == "card":
            card_phones.add(ph)
            continue
        link_phones.add(ph)
        link = str(r[col_link] or "").strip() if col_link < len(r) else ""
        # ссылка — только веб-адрес (реквизиты «+7913… Яндекс» ссылкой не считаем)
        if link.lower().startswith(("http://", "https://")):
            links[ph] = link

    try:
        inv = _vitrina_invoices(zakupka_id)
    except Exception as e:
        return _back(str(e))
    # способ оплаты в дашборде — как разложено в файле (строку можно перенести вниз руками)
    for it in inv:
        if it["phone"] in card_phones:
            billing.set_method(zakupka_id, it["buyer"], billing.METHOD_CARD)
            it["method"] = billing.METHOD_CARD
        elif it["phone"] in link_phones:
            billing.set_method(zakupka_id, it["buyer"], billing.METHOD_LINK)
            it["method"] = billing.METHOD_LINK

    try:
        res, n = _push_invoices(zakupka_id, links=links, clear_links=card_phones, inv=inv)
    except Exception as e:
        return _back(f"Ошибка записи в лист: {e}")
    msg = (f"Счета отправлены в витрину: {n}. Ссылок внесено: {len(links)}"
           f"{f', на карту: {len(card_phones)}' if card_phones else ''}.")
    nf = res.get("not_found") or []
    if nf:
        msg += f" Не нашлись в листе «Покупатели»: {len(nf)} (тел.: {', '.join(nf[:5])}{'…' if len(nf) > 5 else ''})."
    return _back(msg)


@app.post("/zakupka/{zakupka_id}/pay-push")
def pay_push(zakupka_id: int):
    """Без файла: обновить в витрине суммы, доставку и отметки оплаты (ссылки не трогаем)."""
    from urllib.parse import quote
    try:
        res, n = _push_invoices(zakupka_id)
        msg = f"Счета обновлены в витрине: {n}."
        if res.get("not_found"):
            msg += f" Не нашлись в листе: {len(res['not_found'])}."
    except Exception as e:
        msg = str(e) if "витрин" in str(e) else f"Ошибка записи в лист: {e}"
    return RedirectResponse(url=f"/zakupka/{zakupka_id}?paymsg={quote(msg)}#oplata", status_code=303)


@app.post("/api/billing/{zakupka_id}")
def api_billing(zakupka_id: int, background: BackgroundTasks, buyer: str = Form(...),
                field: str = Form(...), value: str = Form("")):
    """Ручные поля счёта: field=delivery (₽, пусто — цена Яндекса) | payto (реквизиты)."""
    import billing
    v = (value or "").strip()
    if field == "delivery":
        try:
            if v:
                float(v.replace(",", "."))
        except ValueError:
            return JSONResponse({"ok": False, "error": "Сумму доставки — числом"})
        billing.set_fee(zakupka_id, buyer, v)
    elif field == "payto":
        billing.set_payto(zakupka_id, buyer, v)
    elif field == "payto_default":
        billing.set_payto_default(v)
        background.add_task(_push_invoices_quiet, zakupka_id)
        return {"ok": True}
    else:
        return JSONResponse({"ok": False, "error": "unknown field"})
    db = get_db()
    inv = next((i for i in billing.invoices(db, zakupka_id) if i["buyer"] == buyer), None)
    db.close()
    background.add_task(_push_one, zakupka_id, buyer)     # девочка видит сразу
    return {"ok": True, "delivery": inv["delivery"] if inv else 0, "total": inv["total"] if inv else 0}


@app.post("/api/paymethod/{zakupka_id}")
def api_paymethod(zakupka_id: int, background: BackgroundTasks, buyer: str = Form(...), method: str = Form(...)):
    """Способ оплаты покупателя: link | card (разметка, не факт оплаты). Сразу в витрину."""
    import billing
    billing.set_method(zakupka_id, buyer, method)
    background.add_task(_push_one, zakupka_id, buyer)
    return {"ok": True, "method": billing.get_method(zakupka_id, buyer)}


def _xlsx_response(wb, filename):
    import io
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _xlsx_header(ws, headers, widths):
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    ws.append(headers)
    fill = PatternFill("solid", fgColor="D9E1F2")
    thin = Side(style="thin", color="BBBBBB")
    ws._thin_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = ws._thin_border
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"


@app.get("/zakupka/{zakupka_id}/rozliv-export")
def rozliv_export(zakupka_id: int):
    """Реестр розлива для разливщика: Наименование → Общее количество (мл) + Позиций."""
    from openpyxl import Workbook
    db = get_db()
    zak = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zak:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    rows = db.execute(
        "SELECT aroma_name, SUM(volume_ml) AS total, COUNT(*) AS cnt, MAX(COALESCE(is_piece, 0)) AS piece "
        "FROM zakaz_items WHERE zakupka_id = ? "
        "GROUP BY aroma_name ORDER BY aroma_name",
        (zakupka_id,),
    ).fetchall()
    db.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр розлива"
    _xlsx_header(ws, ["Наименование", "Общее количество, мл", "Позиций"], [36, 22, 10])
    for r in rows:
        ws.append([r["aroma_name"], f"{r['total'] or 0} шт" if r["piece"] else (r["total"] or 0), r["cnt"]])
        for c in ws[ws.max_row]:
            c.border = ws._thin_border
    return _xlsx_response(wb, f"reestr_rozliv_{zakupka_id}.xlsx")


@app.get("/zakupka/{zakupka_id}/upakovka-export")
def upakovka_export(zakupka_id: int):
    """Накладная для упаковки: Покупатель → список товаров и количество (закупка+наличие)."""
    from openpyxl import Workbook
    db = get_db()
    zak = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zak:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    zk = db.execute(
        "SELECT buyer_name, aroma_name, volume_ml, COALESCE(is_piece, 0) AS is_piece "
        "FROM zakaz_items WHERE zakupka_id = ?",
        (zakupka_id,),
    ).fetchall()
    nl = db.execute(
        "SELECT buyer_name, aroma_name, volume_ml FROM nalichie_orders "
        "WHERE zakupka_id = ? OR zakupka_id IS NULL", (zakupka_id,),
    ).fetchall()
    db.close()
    items = [{"buyer": r["buyer_name"], "aroma": r["aroma_name"],
              "vol": f"{r['volume_ml']} шт" if r["is_piece"] else r["volume_ml"], "src": "закупка"} for r in zk]
    items += [{"buyer": r["buyer_name"], "aroma": r["aroma_name"], "vol": r["volume_ml"], "src": "наличие"} for r in nl]
    items.sort(key=lambda x: (x["buyer"], 0 if x["src"] == "закупка" else 1, x["aroma"]))

    wb = Workbook()
    ws = wb.active
    ws.title = "Накладная"
    _xlsx_header(ws, ["Покупатель", "Товар", "Кол-во", "Источник"], [28, 32, 10, 12])
    prev = None
    for it in items:
        buyer = it["buyer"] if it["buyer"] != prev else ""   # имя покупателя — один раз на группу
        ws.append([buyer, it["aroma"], it["vol"], it["src"]])
        for c in ws[ws.max_row]:
            c.border = ws._thin_border
        prev = it["buyer"]
    return _xlsx_response(wb, f"nakladnaya_{zakupka_id}.xlsx")


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


def _sheet_paid(buyer_name, paid):
    """Честная отметка оплаты → витрина (лист «Покупатели», кол. T). Ошибки не роняют клик."""
    import billing
    try:
        db = get_db()
        phone = billing.phone_of(buyer_name, billing.phone_by_name_map(db))
        db.close()
        if phone:
            buyers_sheet.set_paid(phone, paid)
    except Exception as e:
        print(f"[paid→витрина] {buyer_name}: {e}")


@app.post("/api/status/payment-zakupka/{buyer_name}")
def toggle_payment_zakupka(buyer_name: str, background: BackgroundTasks, zakupka_id: int = Form(...)):
    new_val = 0
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
        background.add_task(_sheet_paid, buyer_name, bool(new_val))
        background.add_task(_push_one, zakupka_id, buyer_name)   # и сам счёт, если ещё не выставлен

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


# === Редактирование состава закупки (позиции zakaz_items) прямо в дашборде ===
@app.post("/zakupka/{zakupka_id}/item/add")
def item_add(zakupka_id: int, buyer_name: str = Form(""), aroma_name: str = Form(""),
             volume_ml: int = Form(0), price_per_10ml: float = Form(0), total_sum: float = Form(0)):
    db = get_db()
    cur = db.execute(
        "INSERT INTO zakaz_items (zakupka_id, buyer_name, aroma_name, volume_ml, price_per_10ml, total_sum) "
        "VALUES (?,?,?,?,?,?)",
        (zakupka_id, buyer_name.strip(), aroma_name.strip(),
         int(volume_ml or 0), float(price_per_10ml or 0), float(total_sum or 0)),
    )
    iid = cur.lastrowid
    import piece
    db.execute("UPDATE zakaz_items SET is_piece = ? WHERE id = ?", (1 if piece.looks_piece(aroma_name) else 0, iid))
    db.execute("INSERT INTO statuses (zakaz_item_id, rozliv, upakovka, payment_zakupka, shipped) VALUES (?,0,0,0,0)", (iid,))
    bn = buyer_name.strip()
    if bn and not db.execute("SELECT id FROM buyers WHERE name = ?", (bn,)).fetchone():
        db.execute("INSERT INTO buyers (name) VALUES (?)", (bn,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True, "id": iid})


@app.post("/zakupka/{zakupka_id}/item/{item_id}/edit")
def item_edit(zakupka_id: int, item_id: int, buyer_name: str = Form(""), aroma_name: str = Form(""),
              volume_ml: int = Form(0), price_per_10ml: float = Form(0), total_sum: float = Form(0)):
    db = get_db()
    db.execute(
        "UPDATE zakaz_items SET buyer_name=?, aroma_name=?, volume_ml=?, price_per_10ml=?, total_sum=?, is_piece=? "
        "WHERE id=? AND zakupka_id=?",
        (buyer_name.strip(), aroma_name.strip(), int(volume_ml or 0),
         float(price_per_10ml or 0), float(total_sum or 0),
         1 if __import__("piece").looks_piece(aroma_name) else 0, item_id, zakupka_id),
    )
    bn = buyer_name.strip()
    if bn and not db.execute("SELECT id FROM buyers WHERE name = ?", (bn,)).fetchone():
        db.execute("INSERT INTO buyers (name) VALUES (?)", (bn,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@app.post("/zakupka/{zakupka_id}/item/{item_id}/delete")
def item_delete(zakupka_id: int, item_id: int):
    db = get_db()
    db.execute("DELETE FROM statuses WHERE zakaz_item_id = ?", (item_id,))
    db.execute("DELETE FROM zakaz_items WHERE id = ? AND zakupka_id = ?", (item_id, zakupka_id))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


# === Синхронизация с витриной (aroma_web): импорт и обновление без гуглшита ===
@app.post("/zakupka/import-vitrina")
def import_vitrina(name: str = Form(...), clear_bills: str = Form("")):
    """Новая закупка сразу со всем составом из витрины. clear_bills — стереть в витрине
    счета прошлой закупки (ссылки, суммы, доставку, «оплачено»)."""
    from urllib.parse import quote
    import vitrina_sync
    try:
        data = vitrina_sync.fetch()
    except Exception as e:
        return RedirectResponse(url=f"/zakupka/new?err={quote(str(e))}", status_code=303)
    db = get_db()
    cur = db.execute(
        "INSERT INTO zakupkas (name, google_sheet_url, status, created_at) VALUES (?, 'vitrina', 'active', ?)",
        (name.strip() or "Закупка", datetime.now().strftime("%Y-%m-%d %H:%M")))
    zid = cur.lastrowid
    vitrina_sync.apply_diff(db, zid, vitrina_sync.compute_diff(db, zid, data["rows"]))
    db.commit()
    db.close()
    msg = ""
    if clear_bills:
        try:
            buyers_sheet.set_pay_fields_bulk({}, clear_others=True)
            msg = "Счета прошлой закупки в витрине очищены."
        except Exception as e:
            msg = f"Закупка создана, но счета в витрине очистить не удалось: {e}"
    return RedirectResponse(url=f"/zakupka/{zid}?paymsg={quote(msg)}" if msg else f"/zakupka/{zid}",
                            status_code=303)


@app.get("/zakupka/{zakupka_id}/sync")
def sync_preview(zakupka_id: int):
    """Что изменится при «Обновить из витрины» (ничего не пишет)."""
    import vitrina_sync
    try:
        data = vitrina_sync.fetch()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    db = get_db()
    diff = vitrina_sync.compute_diff(db, zakupka_id, data["rows"])
    db.close()
    return JSONResponse({"ok": True, "diff": diff, "problems": data.get("problems", [])})


@app.post("/zakupka/{zakupka_id}/sync/apply")
def sync_apply(zakupka_id: int):
    """Применить: берём свежий состав витрины (на момент нажатия) и раскладываем разницу."""
    import vitrina_sync
    try:
        data = vitrina_sync.fetch()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    db = get_db()
    diff = vitrina_sync.compute_diff(db, zakupka_id, data["rows"])
    vitrina_sync.apply_diff(db, zakupka_id, diff)
    db.commit()
    db.close()
    return JSONResponse({"ok": True, "counts": {k: len(diff[k]) for k in ("added", "changed", "removed", "kept")}})


@app.post("/zakupka/{zakupka_id}/delete")
async def delete_zakupka(zakupka_id: int):
    """Полное удаление закупки со всем связанным. Только для архивных (closed)."""
    db = get_db()
    z = db.execute("SELECT status FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not z or z["status"] != "closed":
        db.close()
        return RedirectResponse(url="/", status_code=303)  # активные не удаляем — сперва в архив
    db.execute("DELETE FROM statuses WHERE zakaz_item_id IN (SELECT id FROM zakaz_items WHERE zakupka_id=?)", (zakupka_id,))
    db.execute("DELETE FROM statuses WHERE nalichie_order_id IN (SELECT id FROM nalichie_orders WHERE zakupka_id=?)", (zakupka_id,))
    db.execute("DELETE FROM zakaz_items WHERE zakupka_id = ?", (zakupka_id,))
    db.execute("DELETE FROM nalichie_orders WHERE zakupka_id = ?", (zakupka_id,))
    db.execute("DELETE FROM deliveries WHERE zakupka_id = ?", (zakupka_id,))
    db.execute("DELETE FROM settings WHERE key LIKE ?", (f"box:{zakupka_id}:%",))
    for pref in ("paymethod", "delivfee", "payto"):
        db.execute("DELETE FROM settings WHERE key LIKE ?", (f"{pref}:{zakupka_id}:%",))
    db.execute("DELETE FROM zakupkas WHERE id = ?", (zakupka_id,))
    db.commit()
    db.close()
    return RedirectResponse(url="/", status_code=303)


# === Покупатели ===
@app.get("/buyers", response_class=HTMLResponse)
def buyers_list(request: Request, msg: str = ""):
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
        "msg": msg,
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


@app.post("/buyers/autolink")
def buyers_autolink():
    """Массово проставить телефон покупателям, у кого он есть в имени («79… - Имя»).
    Уже привязанных не трогаем."""
    import buyers_sheet
    from urllib.parse import quote
    db = get_db()
    linked = 0
    for b in db.execute("SELECT id, name, phone FROM buyers").fetchall():
        if buyers_sheet.normalize_phone(b["phone"] or ""):
            continue  # уже привязан вручную
        ph = buyers_sheet.phone_from_name(b["name"] or "")
        if ph:
            db.execute("UPDATE buyers SET phone = ? WHERE id = ?", (ph, b["id"]))
            linked += 1
    db.commit()
    db.close()
    return RedirectResponse(
        url=f"/buyers?msg={quote(f'Привязано по телефону из имени: {linked}')}",
        status_code=303,
    )


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


@app.post("/buyers/delete-bulk")
def delete_buyers_bulk(ids: List[int] = Form(default=[])):
    """Удалить отмеченных покупателей разом. Тех, кто есть в АКТИВНОЙ закупке, не трогаем.
    Позиции закупок не удаляются — там имя хранится текстом."""
    from urllib.parse import quote
    db = get_db()
    active = {r["buyer_name"] for r in db.execute(
        "SELECT DISTINCT zi.buyer_name FROM zakaz_items zi JOIN zakupkas z ON z.id = zi.zakupka_id "
        "WHERE z.status = 'active'").fetchall()}
    deleted, kept = 0, 0
    for bid in set(ids):
        row = db.execute("SELECT name FROM buyers WHERE id = ?", (bid,)).fetchone()
        if not row:
            continue
        if row["name"] in active:
            kept += 1
            continue
        db.execute("DELETE FROM buyers WHERE id = ?", (bid,))
        deleted += 1
    db.commit()
    db.close()
    msg = f"Удалено: {deleted}."
    if kept:
        msg += f" Не тронуты (есть в активной закупке): {kept}."
    return RedirectResponse(url=f"/buyers?msg={quote(msg)}", status_code=303)


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
from cdek_delivery import CdekClient
from cdek_delivery.errors import CdekError


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
        "origin_cdek_address": get_setting("origin_cdek_address", ""),
        "origin_cdek_code": get_setting("origin_cdek_code", ""),
    })


@app.post("/dostavka/origin")
def dostavka_set_origin(pvz_id: str = Form(""), pvz_address: str = Form(""),
                        carrier: str = Form("yandex")):
    """Сохранить ПВЗ отправления (точка А) для перевозчика в настройки."""
    if carrier == "cdek":
        set_setting("origin_cdek_code", pvz_id.strip())
        set_setting("origin_cdek_address", pvz_address.strip())
    else:
        set_setting("origin_pvz_id", pvz_id.strip())
        set_setting("origin_pvz_address", pvz_address.strip())
    return RedirectResponse(url="/dostavka", status_code=303)


def _op_msg(text):
    """Простая страница-уведомление об ошибке операции с кнопкой «назад»."""
    return HTMLResponse(
        f"<div style='font-family:system-ui,sans-serif;padding:2rem;max-width:640px'>"
        f"<h3>Не выполнено</h3><p>{text}</p>"
        f"<p><a href='/dostavka'>← к доставкам</a></p></div>",
        status_code=200,
    )


@app.post("/dostavka/fio")
def dostavka_set_fio(
    phone: str = Form(...),
    last_name: str = Form(""),
    first_name: str = Form(""),
    patronymic: str = Form(""),
    city: str = Form(""),
):
    try:
        res = buyers_sheet.set_fio(phone, last_name, first_name, patronymic)
        if not res.get("ok"):
            return _op_msg(f"Не удалось записать ФИО: «{res.get('reason')}» "
                          f"(телефон {phone or '—'} не найден в листе «Покупатели»).")
        if city.strip():
            buyers_sheet.set_city(phone, city.strip())
    except Exception as e:
        return _op_msg(f"Ошибка записи ФИО в лист: {e}")
    return RedirectResponse(url="/dostavka", status_code=303)


@app.get("/dostavka/pvz")
def dostavka_pvz_search(city: str = "", limit: int = 40, dropoff: int = 0, carrier: str = "yandex"):
    """JSON-поиск ПВЗ отправления по городу (для пикера точки А).
    dropoff=1 — только точки приёма посылок. carrier=yandex|cdek."""
    city = (city or "").strip()
    if not city:
        return JSONResponse({"ok": False, "error": "Укажите город"})
    try:
        if carrier == "cdek":
            c = CdekClient()
            code = c.city_code(city)
            if not code:
                return JSONResponse({"ok": False, "error": "Город не найден"})
            # для отправления — точки ПРИЁМА (is_reception)
            pts = c.list_pickup_points(city_code=code, is_reception=bool(dropoff) or None, size=300)
            data = [{"id": p.code, "name": p.name or "ПВЗ", "address": p.address_full} for p in pts[:limit]]
            return JSONResponse({"ok": True, "env": c.env, "count": len(pts), "points": data})
        c = YandexDeliveryClient()
        gid = c.geo_id(city)
        points = c.list_pickup_points(geo_id=gid)
        # Для ПВЗ ОТПРАВЛЕНИЯ (точка А) нужны только точки приёма — фильтр на нашей
        # стороне: Яндекс на параметр available_for_dropoff отвечает 400.
        if dropoff:
            points = [p for p in points if p.available_for_dropoff]
        data = [{"id": p.id, "name": p.name, "address": p.full_address} for p in points[:limit]]
        return JSONResponse({"ok": True, "env": c.env, "count": len(points), "points": data})
    except (YandexDeliveryError, CdekError) as e:
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
        res = buyers_sheet.set_pvz(phone, pvz_address, pvz_id)
        if not res.get("ok"):
            return _op_msg(f"Не удалось записать ПВЗ: «{res.get('reason')}» "
                          f"(телефон {phone or '—'} не найден в листе «Покупатели»).")
    except Exception as e:
        return _op_msg(f"Ошибка записи ПВЗ в лист: {e}")
    return RedirectResponse(url="/dostavka", status_code=303)


def _pline(it):
    """Строка посылки: флакон (мл) или штучный товар (База — вес 1 шт × штук)."""
    from yandex_delivery import parcel
    import piece
    pw = piece.weight_g(it["aroma_name"]) if it["is_piece"] else 0
    return parcel.ParcelLine(it["aroma_name"], it["volume_ml"], 1,
                             int(round((it["total_sum"] or 0) * 100)), piece_weight_g=pw)


@app.post("/dostavka/zakupka/{zakupka_id}/piece-weight")
def dostavka_piece_weight(zakupka_id: int, name: List[str] = Form(default=[]),
                          grams: List[str] = Form(default=[]), back: str = Form("")):
    """Вес 1 шт штучных товаров (База, ММБ). Пусто — оценка по названию."""
    from urllib.parse import quote
    import piece
    saved = 0
    for n, g in zip(name, grams):
        g = (g or "").strip()
        if g and not g.isdigit():
            continue
        piece.set_weight(n, g)
        saved += 1
    if back == "settings":
        return RedirectResponse(url=f"/zakupka/{zakupka_id}#nastroyki", status_code=303)
    return RedirectResponse(url=f"/dostavka/zakupka/{zakupka_id}?msg={quote(f'Вес штучных сохранён: {saved}')}",
                            status_code=303)


def _settings_info(zakupka_id):
    """Настроечное для вкладки «Настройки»: откуда отправляем, кто платит доставку, вес базы."""
    import carriers
    import piece
    db = get_db()
    pieces = [{"name": r["aroma_name"], "count": r["n"], "manual": piece.manual_weight(r["aroma_name"]),
               "auto": piece.default_weight_g(r["aroma_name"])}
              for r in db.execute(
                  "SELECT aroma_name, SUM(volume_ml) AS n FROM zakaz_items "
                  "WHERE zakupka_id = ? AND COALESCE(is_piece, 0) = 1 GROUP BY aroma_name ORDER BY aroma_name",
                  (zakupka_id,)).fetchall()]
    db.close()
    return {
        "origin_yandex": get_setting("origin_pvz_address", ""),
        "origin_cdek": get_setting("origin_cdek_address", ""),
        "paid_by_yandex": carriers.paid_by(carrier="yandex"),
        "paid_by_cdek": carriers.paid_by(carrier="cdek"),
        "pieces": pieces,
    }


def _ship_readiness(db, zakupka_id):
    """{buyer_name: {"ready": bool, "wait": [...]}} — можно ли отправлять: всё разлито,
    всё упаковано (закупка + наличие), оплачено (закупка; наличие — если оно есть)."""
    out = {}
    for r in db.execute(
        "SELECT zi.buyer_name, SUM(CASE WHEN COALESCE(s.rozliv,0)=0 THEN 1 ELSE 0 END) AS pour, "
        "SUM(CASE WHEN COALESCE(s.upakovka,0)=0 THEN 1 ELSE 0 END) AS pack, "
        "MAX(COALESCE(s.payment_zakupka,0)) AS paid, MAX(COALESCE(s.shipped,0)) AS shipped "
        "FROM zakaz_items zi LEFT JOIN statuses s ON s.zakaz_item_id = zi.id "
        "WHERE zi.zakupka_id = ? GROUP BY zi.buyer_name", (zakupka_id,),
    ).fetchall():
        out[r["buyer_name"]] = {"pour": r["pour"] or 0, "pack": r["pack"] or 0, "paid": bool(r["paid"]),
                                "shipped": bool(r["shipped"])}
    for r in db.execute(
        "SELECT no.buyer_name, SUM(CASE WHEN COALESCE(s.upakovka,0)=0 THEN 1 ELSE 0 END) AS pack, "
        "MAX(COALESCE(s.payment_nalichie,0)) AS paid "
        "FROM nalichie_orders no LEFT JOIN statuses s ON s.nalichie_order_id = no.id "
        "WHERE no.zakupka_id = ? OR no.zakupka_id IS NULL GROUP BY no.buyer_name", (zakupka_id,),
    ).fetchall():
        d = out.setdefault(r["buyer_name"], {"pour": 0, "pack": 0, "paid": True})
        d["pack"] += r["pack"] or 0
        d["paid"] = d["paid"] and bool(r["paid"])
    for r in db.execute(
        "SELECT zi.buyer_name, zi.aroma_name, zi.volume_ml, COALESCE(zi.is_piece, 0) AS piece "
        "FROM zakaz_items zi LEFT JOIN statuses s ON s.zakaz_item_id = zi.id "
        "WHERE zi.zakupka_id = ? AND COALESCE(s.rozliv, 0) = 0 ORDER BY zi.aroma_name", (zakupka_id,),
    ).fetchall():
        d = out.get(r["buyer_name"])
        if d is not None:
            d.setdefault("pour_list", []).append(
                f"{r['aroma_name']} {r['volume_ml']} {'шт' if r['piece'] else 'мл'}")
    for d in out.values():
        d.setdefault("pour_list", [])
        wait = []
        if d["pour"]:
            wait.append(f"розлив {d['pour']}")
        if d["pack"]:
            wait.append(f"упаковка {d['pack']}")
        if not d["paid"]:
            wait.append("оплата")
        d["wait"] = wait
        d["ready"] = not wait
    return out


def _delivery_block_reason(phone, rec):
    """Короткая причина, почему покупатель не готов к доставке (или '')."""
    if not phone:
        return "нет привязки телефона (страница «Покупатели»)"
    if rec is None:
        return "телефон не найден в листе «Покупатели»"
    if rec.get("carrier_manual"):
        return f"вручную: {rec['carrier_manual']}"
    if not (rec.get("first_name") or rec.get("last_name")):
        return "не заполнено ФИО получателя"
    if not rec.get("pvz_id"):
        return "не выбран ПВЗ"
    return ""


@app.get("/dostavka/zakupka/{zakupka_id}", response_class=HTMLResponse)
def dostavka_zakupka(request: Request, zakupka_id: int, msg: str = ""):
    """Превью массовой доставки по закупке: покупатель → телефон → получатель
    из листа → расчёт посылки (вес/коробка) → готовность. Без вызовов API."""
    from yandex_delivery import parcel

    db = get_db()
    zakupka = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zakupka:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    items = db.execute(
        "SELECT buyer_name, aroma_name, volume_ml, total_sum, COALESCE(is_piece, 0) AS is_piece "
        "FROM zakaz_items WHERE zakupka_id = ? ORDER BY buyer_name",
        (zakupka_id,),
    ).fetchall()
    phones = {}
    for b in db.execute("SELECT name, phone FROM buyers").fetchall():
        phones[b["name"]] = buyers_sheet.normalize_phone(b["phone"] or "")
    deliveries = {}
    for d in db.execute(
        "SELECT id, phone, status, price, request_id, tracking_url, carrier, cdek_number "
        "FROM deliveries WHERE zakupka_id = ? AND status != 'cancelled'",
        (zakupka_id,),
    ).fetchall():
        deliveries[d["phone"]] = row_to_dict(d)
    readiness = _ship_readiness(db, zakupka_id)
    import piece
    piece_items = [{"name": r["aroma_name"], "count": r["n"], "manual": piece.manual_weight(r["aroma_name"]),
                    "auto": piece.default_weight_g(r["aroma_name"])}
                   for r in db.execute(
                       "SELECT aroma_name, SUM(volume_ml) AS n FROM zakaz_items "
                       "WHERE zakupka_id = ? AND COALESCE(is_piece, 0) = 1 GROUP BY aroma_name ORDER BY aroma_name",
                       (zakupka_id,)).fetchall()]
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
        phone = phones.get(buyer_name, "") or buyers_sheet.phone_from_name(buyer_name)
        rec = recips.get(phone) if phone else None
        lines = [_pline(it) for it in its]
        box_override = get_setting(f"box:{zakupka_id}:{phone}", "") if phone else ""
        calc = parcel.calc(lines, barcode=f"Z{zakupka_id}-{phone or buyer_name}",
                           box=parcel.get_supplier_box(box_override))
        reason = _delivery_block_reason(phone, rec)
        rows.append({
            "buyer_name": buyer_name,
            "phone": phone,
            "fio": rec["fio"] if rec else "",
            "pvz_address": rec["pvz_address"] if rec else "",
            "carrier": (rec.get("carrier") if rec else ""),  # пусто, пока покупатель не выбрал ТК
            "carrier_manual": (rec.get("carrier_manual") if rec else ""),
            "ship": readiness.get(buyer_name, {"ready": False, "wait": [], "pour": 0, "pack": 0,
                                               "paid": False, "pour_list": [], "shipped": False}),
            "positions": len(its),
            "weight_g": calc.weight_g,
            "box": calc.box.code,                         # выбранная коробка поставщика
            "box_name": calc.box.name,
            "box_yandex": calc.yandex_ref.code if calc.yandex_ref else "",
            "box_manual": bool(box_override),             # выбрана вручную
            "ready": (reason == ""),
            "reason": reason,
            "delivery": deliveries.get(phone),
        })
    for r in rows:
        sh, dl = r["ship"], r["delivery"] or {}
        if dl.get("status") in ("confirmed", "labeled") or sh.get("shipped"):
            r["stage"] = "sent"
        elif sh.get("pour"):
            r["stage"] = "pour"
        elif sh.get("pack"):
            r["stage"] = "pack"
        elif not sh.get("paid"):
            r["stage"] = "pay"
        else:
            r["stage"] = "ship"
    rows.sort(key=lambda x: (not x["ready"], x["buyer_name"].lower()))
    ready_count = sum(1 for r in rows if r["ready"])
    # перевозчики среди подтверждённых — для кнопок печати ярлыков по каждому
    confirmed_carriers = sorted({
        (d.get("carrier") or "yandex") for d in deliveries.values()
        if d.get("status") in ("confirmed", "labeled")
    })

    return templates.TemplateResponse("dostavka_zakupka.html", {
        "request": request,
        "zakupka": row_to_dict(zakupka),
        "rows": rows,
        "ready_count": ready_count,
        "total": len(rows),
        "sheet_error": sheet_error,
        "origin_pvz_id": get_setting("origin_pvz_id", ""),
        "origin_pvz_address": get_setting("origin_pvz_address", ""),
        "origin_cdek_code": get_setting("origin_cdek_code", ""),
        "origin_cdek_address": get_setting("origin_cdek_address", ""),
        "confirmed_carriers": confirmed_carriers,
        "piece_items": piece_items,
        "paid_by_yandex": __import__("carriers").paid_by(carrier="yandex"),
        "paid_by_cdek": __import__("carriers").paid_by(carrier="cdek"),
        "supplier_boxes": [{"code": b.code, "name": b.name} for b in parcel.SUPPLIER_BOXES],
        "msg": msg,
    })


@app.get("/dostavka/zakupka/{zakupka_id}/export")
def dostavka_export(zakupka_id: int):
    """Выгрузка таблицы посылок в Excel для разливщика: состав, вес, коробка
    (+ пустые «ОК?» и «Заменить на» для его пометок)."""
    import io
    from yandex_delivery import parcel
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    db = get_db()
    zak = db.execute("SELECT * FROM zakupkas WHERE id = ?", (zakupka_id,)).fetchone()
    if not zak:
        db.close()
        raise HTTPException(status_code=404, detail="Закупка не найдена")
    items = db.execute(
        "SELECT buyer_name, aroma_name, volume_ml, total_sum, COALESCE(is_piece, 0) AS is_piece "
        "FROM zakaz_items WHERE zakupka_id = ? ORDER BY buyer_name", (zakupka_id,),
    ).fetchall()
    phone_by_name = {b["name"]: buyers_sheet.normalize_phone(b["phone"] or "")
                     for b in db.execute("SELECT name, phone FROM buyers").fetchall()}

    by_buyer = {}
    for it in items:
        by_buyer.setdefault(it["buyer_name"], []).append(it)
    try:
        recips = {r["phone"]: r for r in buyers_sheet.list_recipients()}
    except Exception:
        recips = {}

    def _how(rec):
        """Как едет посылка: Яндекс/СДЭК или ручная доставка с ФИО и адресом."""
        if not rec:
            return ""
        if rec.get("carrier_manual"):
            return f"✋ {rec['carrier_manual']}: {rec.get('fio', '')}, {rec.get('pvz_address', '')}".strip(", ")
        return {"cdek": "СДЭК", "yandex": "Яндекс"}.get(rec.get("carrier"), "")

    wb = Workbook()
    ws = wb.active
    ws.title = "Доставка"
    headers = ["№", "Покупатель", "Доставка", "Состав заказа", "Позиций", "Вес, г",
               "Коробка (поставщик)", "Ориентир Яндекса", "ОК?", "Заменить на"]
    ws.append(headers)

    head_fill = PatternFill("solid", fgColor="D9E1F2")
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border

    n = 0
    for buyer_name, its in by_buyer.items():
        n += 1
        phone = phone_by_name.get(buyer_name, "") or buyers_sheet.phone_from_name(buyer_name)
        lines = [_pline(it) for it in its]
        box = parcel.get_supplier_box(get_setting(f"box:{zakupka_id}:{phone}", "")) if phone else None
        calc = parcel.calc(lines, barcode=f"X{zakupka_id}-{n}", box=box)
        contents = "; ".join(f"{it['aroma_name']} ×{it['volume_ml']}{' шт' if it['is_piece'] else 'мл'}" for it in its)
        ws.append([n, buyer_name, _how(recips.get(phone)), contents, len(its), calc.weight_g,
                   calc.box.name, calc.yandex_ref.code if calc.yandex_ref else "", "", ""])
        for c in ws[ws.max_row]:
            c.border = border
            c.alignment = Alignment(vertical="top", wrap_text=(c.column in (3, 4)))
    db.close()

    widths = [4, 26, 30, 42, 8, 8, 20, 16, 6, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"dostavka_{zakupka_id}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/dostavka/zakupka/{zakupka_id}/box")
def dostavka_set_box(zakupka_id: int, phone: str = Form(...), box: str = Form(...)):
    """Ручная смена коробки поставщика для получателя. Пусто → сброс на авто-подбор."""
    from yandex_delivery import parcel
    box = (box or "").strip()
    if box and parcel.get_supplier_box(box) is None:
        return JSONResponse({"ok": False, "error": "неизвестная коробка"}, status_code=400)
    set_setting(f"box:{zakupka_id}:{phone.strip()}", box)
    return JSONResponse({"ok": True, "manual": bool(box)})


def _zakupka_lines_by_phone(db, zakupka_id):
    """Собирает позиции закупки по телефону покупателя: {phone: ([ParcelLine], buyer_name)}."""
    from yandex_delivery import parcel
    phone_by_name = {}
    for b in db.execute("SELECT name, phone FROM buyers").fetchall():
        phone_by_name[b["name"]] = buyers_sheet.normalize_phone(b["phone"] or "")
    items = db.execute(
        "SELECT buyer_name, aroma_name, volume_ml, total_sum, COALESCE(is_piece, 0) AS is_piece "
        "FROM zakaz_items WHERE zakupka_id = ?",
        (zakupka_id,),
    ).fetchall()
    out = {}
    for it in items:
        # привязка вручную (buyers.phone) или телефон прямо из имени «7… - Имя»
        ph = phone_by_name.get(it["buyer_name"], "") or buyers_sheet.phone_from_name(it["buyer_name"])
        if not ph:
            continue
        lines, _ = out.setdefault(ph, ([], it["buyer_name"]))
        lines.append(_pline(it))
    return out


@app.post("/dostavka/zakupka/{zakupka_id}/create")
def dostavka_create(zakupka_id: int, phones: List[str] = Form(default=[])):
    """ШАГ ①: черновики по отмеченным. Яндекс — offers/create (цена);
    СДЭК — локальная пометка (реальный заказ создаётся при подтверждении)."""
    import uuid
    import carriers
    from yandex_delivery import YandexDeliveryClient, parcel
    from yandex_delivery.errors import YandexDeliveryError
    from urllib.parse import quote

    def _back(msg):
        return RedirectResponse(
            url=f"/dostavka/zakupka/{zakupka_id}?msg={quote(msg)}", status_code=303)

    if not phones:
        return _back("Не отмечено ни одного получателя.")
    try:
        recips = {r["phone"]: r for r in buyers_sheet.list_recipients()}
    except Exception as e:
        return _back(f"Не удалось прочитать лист «Покупатели»: {e}")

    origin_y = get_setting("origin_pvz_id", "")
    origin_c = get_setting("origin_cdek_code", "")

    db = get_db()
    lines_by_phone = _zakupka_lines_by_phone(db, zakupka_id)
    yclient = YandexDeliveryClient()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    ins = ("INSERT INTO deliveries (zakupka_id, buyer_name, phone, operator_request_id, "
           "offer_id, price, status, carrier, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)")

    created, skipped, errors = 0, 0, []
    for ph in set(phones):
        rec = recips.get(ph)
        if not rec or rec.get("carrier_manual") or not rec.get("pvz_id")                 or not (rec.get("first_name") or rec.get("last_name")):
            skipped += 1   # ручная доставка (Почта России и т.п.) через API не идёт
            continue
        pair = lines_by_phone.get(ph)
        if not pair or not pair[0]:
            skipped += 1
            continue
        lines, buyer_name = pair
        if db.execute(
            "SELECT id FROM deliveries WHERE zakupka_id=? AND phone=? AND status IN ('offered','confirmed')",
            (zakupka_id, ph),
        ).fetchone():
            skipped += 1
            continue

        carrier = carriers.normalize(rec.get("carrier"))
        opid = "luzi-" + uuid.uuid4().hex[:12]
        box = parcel.get_supplier_box(get_setting(f"box:{zakupka_id}:{ph}", ""))
        calc = parcel.calc(lines, barcode=opid, box=box)

        if carrier == "cdek":
            if not origin_c:
                errors.append(f"{buyer_name}: не задан ПВЗ отправления СДЭК")
                continue
            # СДЭК: заказ создаётся при подтверждении → сейчас только черновик-пометка
            db.execute(ins, (zakupka_id, buyer_name, ph, opid, "", "", "offered", "cdek", now, now))
            db.commit()
            created += 1
            continue

        # Яндекс: offers/create (черновик + цена)
        if not origin_y:
            errors.append(f"{buyer_name}: не задан ПВЗ отправления Яндекс")
            continue
        recipient = {"phone": "+" + ph,
                     "first_name": rec.get("first_name") or rec.get("last_name") or "Получатель"}
        if rec.get("last_name"):
            recipient["last_name"] = rec["last_name"]
        if rec.get("patronymic"):
            recipient["patronymic"] = rec["patronymic"]
        payload = {
            "info": {"operator_request_id": opid},
            "source": {"platform_station": {"platform_id": origin_y}},
            "destination": {"type": "platform_station",
                            "platform_station": {"platform_id": rec["pvz_id"]}},
            "items": [i.to_dict() for i in calc.items],
            "places": [calc.place.to_dict()],
            "billing_info": {"payment_method": "already_paid", "delivery_cost": 0},
            "recipient_info": recipient,
            "last_mile_policy": "self_pickup",
        }
        try:
            resp = yclient.create_offers(payload)
            offers = resp.get("offers") or []
            if not offers:
                errors.append(f"{buyer_name}: нет вариантов доставки")
                continue
            off = offers[0]
            det = off.get("offer_details") or {}
            price = det.get("pricing_total") or det.get("pricing") or ""
            offer_id = off.get("offer_id", "")
            # Покупатель платит доставку → пересоздаём оффер с наложенным платежом
            # (сумму знаем только после первого оффера).
            if carriers.paid_by(carrier="yandex") == "recipient":
                payload["info"]["operator_request_id"] = opid + "-cod"
                payload["billing_info"] = {"payment_method": "card_on_receipt",
                                           "delivery_cost": carriers.price_to_kopecks(price)}
                r2 = yclient.create_offers(payload)
                o2 = (r2.get("offers") or [{}])[0]
                if not o2.get("offer_id"):
                    errors.append(f"{buyer_name}: наложка не оформилась")
                    continue
                offer_id = o2["offer_id"]
            db.execute(ins, (zakupka_id, buyer_name, ph, opid,
                             offer_id, price, "offered", "yandex", now, now))
            db.commit()
            created += 1
        except YandexDeliveryError as e:
            errors.append(f"{buyer_name}: {getattr(e, 'message', e)}")
        except Exception as e:
            errors.append(f"{buyer_name}: {e}")

    db.close()
    parts = [f"Создано черновиков: {created}"]
    if skipped:
        parts.append(f"пропущено (не готовы/уже есть): {skipped}")
    if errors:
        parts.append("ошибки — " + "; ".join(errors[:5]))
    return _back(". ".join(parts))


def _sync_sheet_tracking(db, phone):
    """Синхронизировать колонку N листа: ставим ссылку АКТИВНОЙ (confirmed) доставки
    этого телефона; если активной нет — очищаем (чтобы не висел трек отменённой)."""
    row = db.execute(
        "SELECT tracking_url FROM deliveries WHERE phone = ? AND status = 'confirmed' "
        "AND tracking_url != '' ORDER BY id DESC LIMIT 1",
        (phone,),
    ).fetchone()
    try:
        buyers_sheet.set_tracking(phone, row["tracking_url"] if row else "")
    except Exception:
        pass


@app.post("/dostavka/delivery/{delivery_id}/cancel")
def dostavka_cancel(delivery_id: int):
    """Отмена доставки. Черновик (offered) — убираем локально (брони не было).
    Подтверждённая — зовём request/cancel в Яндексе; если статус уже не позволяет,
    показываем ответ Яндекса."""
    from urllib.parse import quote

    db = get_db()
    d = db.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    if not d:
        db.close()
        return _op_msg("Доставка не найдена.")
    d = row_to_dict(d)
    zid = d["zakupka_id"]

    def _back(msg):
        return RedirectResponse(url=f"/dostavka/zakupka/{zid}?msg={quote(msg)}", status_code=303)

    # Черновик без брони — просто удаляем строку (в Яндексе заявки не было).
    if not d.get("request_id") or d.get("status") == "offered":
        db.execute("DELETE FROM deliveries WHERE id = ?", (delivery_id,))
        db.commit()
        _sync_sheet_tracking(db, d["phone"])
        db.close()
        return _back(f"Черновик «{d['buyer_name']}» убран (в Яндексе брони не было).")

    # Подтверждённая — отменяем у перевозчика (Яндекс: request/cancel, СДЭК: delete_order).
    import carriers
    try:
        carriers.cancel_delivery(d.get("carrier"), d["request_id"])
    except Exception as e:
        db.close()
        return _back(f"Не удалось отменить «{d['buyer_name']}»: {getattr(e, 'message', e)}")
    db.execute(
        "UPDATE deliveries SET status = 'cancelled', updated_at = ? WHERE id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), delivery_id),
    )
    db.commit()
    _sync_sheet_tracking(db, d["phone"])
    db.close()
    return _back(f"Доставка «{d['buyer_name']}» отменена.")


def _label_msg(text):
    return HTMLResponse(
        f"<div style='font-family:system-ui,sans-serif;padding:2rem;max-width:640px'>"
        f"<h3>🏷 Ярлыки</h3><p>{text}</p>"
        f"<p><a href='#' onclick='history.back();return false'>← назад</a></p></div>"
    )


@app.get("/dostavka/zakupka/{zakupka_id}/labels")
def dostavka_labels(zakupka_id: int, carrier: str = ""):
    """Массовые ярлыки (PDF) по подтверждённым доставкам ОДНОГО перевозчика.
    carrier не задан → берём перевозчика подтверждённых (если он один)."""
    import carriers

    db = get_db()
    rows = [row_to_dict(r) for r in db.execute(
        "SELECT request_id, carrier FROM deliveries WHERE zakupka_id = ? "
        "AND status IN ('confirmed','labeled') AND request_id != ''",
        (zakupka_id,),
    ).fetchall()]
    db.close()
    if not rows:
        return _label_msg("Нет подтверждённых доставок. Сначала «Создать» и «Подтвердить».")

    present = sorted({carriers.normalize(r["carrier"]) for r in rows})
    carrier = carriers.normalize(carrier) if carrier else present[0]
    if carrier not in present:
        return _label_msg(f"Нет подтверждённых доставок перевозчика {carrier}.")
    ids = [r["request_id"] for r in rows if carriers.normalize(r["carrier"]) == carrier]

    try:
        pdf = carriers.labels_pdf(carrier, ids)
    except Exception as e:
        return _label_msg(f"Ярлыки {carrier}: {e}")

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="labels_{carrier}_{zakupka_id}.pdf"'},
    )


@app.post("/dostavka/zakupka/{zakupka_id}/confirm")
def dostavka_confirm(zakupka_id: int, phones: List[str] = Form(default=[])):
    """ШАГ ②: реальная бронь отмеченных черновиков. Яндекс: offers/confirm;
    СДЭК: create_order + опрос номера (заказ строится здесь из получателя и посылки)."""
    import carriers
    from yandex_delivery import parcel
    from urllib.parse import quote

    def _back(msg):
        return RedirectResponse(
            url=f"/dostavka/zakupka/{zakupka_id}?msg={quote(msg)}", status_code=303)

    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    selected = set(phones)
    rows = [r for r in db.execute(
        "SELECT id, offer_id, phone, buyer_name, carrier, operator_request_id "
        "FROM deliveries WHERE zakupka_id=? AND status='offered'", (zakupka_id,),
    ).fetchall() if not selected or r["phone"] in selected]
    if not rows:
        db.close()
        return _back("Нет отмеченных черновиков для подтверждения.")

    # получатели + позиции нужны СДЭК (реальный заказ строится на этом шаге)
    try:
        recips = {r["phone"]: r for r in buyers_sheet.list_recipients()}
    except Exception:
        recips = {}
    lines_by_phone = _zakupka_lines_by_phone(db, zakupka_id)
    origin_y = get_setting("origin_pvz_id", "")
    origin_c = get_setting("origin_cdek_code", "")

    confirmed, errors = 0, []
    for r in rows:
        carrier = carriers.normalize(r["carrier"])
        rec = recips.get(r["phone"])
        pair = lines_by_phone.get(r["phone"])
        _box = parcel.get_supplier_box(get_setting(f"box:{zakupka_id}:{r['phone']}", ""))
        calc = parcel.calc(pair[0], barcode=r["operator_request_id"], box=_box) if (pair and pair[0]) else None
        if carrier == "cdek" and (not rec or not calc):
            errors.append(f"{r['buyer_name']}: нет данных получателя/позиций для СДЭК")
            continue
        origin_id = origin_c if carrier == "cdek" else origin_y

        res = carriers.confirm_delivery(carrier, dict(r), rec, calc,
                                        r["operator_request_id"], origin_id)
        if not res.get("ok"):
            errors.append(f"{r['buyer_name']}: {res.get('error')}")
            continue
        track = res.get("tracking", "")
        if res.get("price"):
            db.execute(
                "UPDATE deliveries SET request_id=?, cdek_number=?, status='confirmed', "
                "tracking_url=?, price=?, updated_at=? WHERE id=?",
                (res.get("request_id", ""), res.get("cdek_number", ""), track,
                 res["price"], now, r["id"]),
            )
        else:
            db.execute(
                "UPDATE deliveries SET request_id=?, cdek_number=?, status='confirmed', "
                "tracking_url=?, updated_at=? WHERE id=?",
                (res.get("request_id", ""), res.get("cdek_number", ""), track, now, r["id"]),
            )
        db.commit()
        confirmed += 1
        if track:  # покупатель увидит ссылку в витрине (лист «Покупатели», колонка N)
            try:
                buyers_sheet.set_tracking(r["phone"], track)
            except Exception:
                pass
    db.close()
    parts = [f"Подтверждено: {confirmed}"]
    if errors:
        parts.append("ошибки — " + "; ".join(errors[:5]))
    return _back(". ".join(parts))


@app.post("/dostavka/delivery/{delivery_id}/track")
def dostavka_refresh_track(delivery_id: int):
    """Обновить ссылку отслеживания из API (по перевозчику)."""
    import carriers
    from urllib.parse import quote

    db = get_db()
    d = db.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    if not d:
        db.close()
        return _op_msg("Доставка не найдена.")
    d = row_to_dict(d)
    zid = d["zakupka_id"]

    def _back(msg):
        return RedirectResponse(url=f"/dostavka/zakupka/{zid}?msg={quote(msg)}", status_code=303)

    if not d.get("request_id"):
        db.close()
        return _back("Ссылка появится после подтверждения заявки.")
    try:
        track, num = carriers.refresh_tracking(d.get("carrier"), d)
    except Exception as e:
        db.close()
        return _back(f"Не удалось получить ссылку: {e}")
    db.execute("UPDATE deliveries SET tracking_url=?, cdek_number=? WHERE id=?",
               (track, num or d.get("cdek_number", ""), delivery_id))
    db.commit()
    db.close()
    if track:
        try:
            buyers_sheet.set_tracking(d["phone"], track)
        except Exception:
            pass
    return _back("Ссылка отслеживания обновлена." if track
                 else "Номер/ссылка ещё готовится — попробуй чуть позже.")


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