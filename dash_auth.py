# dash_auth.py — вход в дашборд: учётные записи, роли, сессии.
#
# Роли:
#   admin    — организатор (Елена): всё.
#   supplier — поставщик/разливщик: розлив, упаковка, «отправлено», доставки (создать,
#              подтвердить, ярлыки, коробки, вес), выгрузки Excel и загрузка ссылок на оплату.
#
# Пароли — только хэш pbkdf2 (как в витрине), сессия — подписанная HMAC кука (секрет
# хранится в settings, заводится сам при первом запуске). Смена пароля гасит старые сессии.
#
# Первый вход без паролей в коде: на сервере `python dash_auth.py setup-code` печатает
# одноразовый код (сутки). По /setup?code=… организатор сама задаёт логин и пароль.

import base64
import hashlib
import hmac
import re
import secrets
import sys
import time
from datetime import datetime

from models import get_db, get_setting, set_setting

ROLE_ADMIN = "admin"
ROLE_SUPPLIER = "supplier"
ROLE_LABELS = {ROLE_ADMIN: "организатор", ROLE_SUPPLIER: "поставщик"}

COOKIE = "dash_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 260000
MIN_PASSWORD = 8


# ---------------- хранилище ----------------

def init():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS dash_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL UNIQUE,
            name TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'supplier',
            pass_hash TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT ''
        )""")
    db.commit()
    db.close()


def _secret():
    s = get_setting("auth:secret", "")
    if not s:
        s = secrets.token_hex(32)
        set_setting("auth:secret", s)
    return s.encode()


# ---------------- пароли ----------------

def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw, stored):
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, AttributeError, TypeError):
        return False


def password_problem(pw):
    if len(pw or "") < MIN_PASSWORD:
        return f"Пароль — минимум {MIN_PASSWORD} символов."
    return ""


def clean_login(login):
    return re.sub(r"\s+", "", (login or "").strip().lower())


# ---------------- пользователи ----------------

def list_users():
    db = get_db()
    rows = [dict(r) for r in db.execute(
        "SELECT id, login, name, role, active, created_at FROM dash_users ORDER BY role, login").fetchall()]
    db.close()
    return rows


def get_user(uid):
    db = get_db()
    r = db.execute("SELECT * FROM dash_users WHERE id = ? AND active = 1", (uid,)).fetchone()
    db.close()
    return dict(r) if r else None


def find_login(login):
    db = get_db()
    r = db.execute("SELECT * FROM dash_users WHERE login = ?", (clean_login(login),)).fetchone()
    db.close()
    return dict(r) if r else None


def create_user(login, name, role, password):
    login = clean_login(login)
    if not login:
        return "Укажи логин."
    if role not in ROLE_LABELS:
        return "Неизвестная роль."
    err = password_problem(password)
    if err:
        return err
    if find_login(login):
        return "Такой логин уже есть."
    db = get_db()
    db.execute("INSERT INTO dash_users (login, name, role, pass_hash, active, created_at) VALUES (?,?,?,?,1,?)",
               (login, (name or "").strip(), role, hash_password(password),
                datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    db.close()
    return ""


def set_password(uid, password):
    err = password_problem(password)
    if err:
        return err
    db = get_db()
    db.execute("UPDATE dash_users SET pass_hash = ? WHERE id = ?", (hash_password(password), uid))
    db.commit()
    db.close()
    return ""


def set_active(uid, active):
    db = get_db()
    db.execute("UPDATE dash_users SET active = ? WHERE id = ?", (1 if active else 0, uid))
    db.commit()
    db.close()


def admins_count():
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM dash_users WHERE role = ? AND active = 1", (ROLE_ADMIN,)).fetchone()[0]
    db.close()
    return n


# ---------------- сессии ----------------

def _sig(uid, exp, pass_hash):
    msg = f"{uid}.{exp}.{pass_hash[-16:]}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_session(user):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{user['id']}.{exp}.{_sig(user['id'], exp, user['pass_hash'])}"


def user_from_cookie(value):
    try:
        uid, exp, sig = (value or "").split(".")
        uid, exp = int(uid), int(exp)
    except ValueError:
        return None
    if exp < time.time():
        return None
    u = get_user(uid)
    if not u or not hmac.compare_digest(sig, _sig(uid, exp, u["pass_hash"])):
        return None
    return u


# ---------------- защита от подбора ----------------

_fails = {}   # ip -> [время неудач]
FAIL_LIMIT, FAIL_WINDOW = 5, 600


def blocked(ip):
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    return len(_fails[ip]) >= FAIL_LIMIT


def note_fail(ip):
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip):
    _fails.pop(ip, None)


# ---------------- одноразовый код первого входа ----------------

def new_setup_code():
    code = secrets.token_urlsafe(18)
    set_setting("auth:setup", hashlib.sha256(code.encode()).hexdigest() + ":" + str(int(time.time()) + 86400))
    return code


def check_setup_code(code):
    v = get_setting("auth:setup", "")
    if not v or not code or ":" not in v:
        return False
    h, exp = v.split(":", 1)
    return int(exp) > time.time() and hmac.compare_digest(h, hashlib.sha256(code.encode()).hexdigest())


def burn_setup_code():
    set_setting("auth:setup", "")


# ---------------- права поставщика ----------------

_SUPPLIER_ALLOWED = [
    ("GET", r"/"),
    ("GET", r"/zakupka/\d+"),
    ("GET", r"/zakupka/\d+/(rozliv-export|upakovka-export|pay-export)"),
    ("GET", r"/dostavka/zakupka/\d+"),
    ("GET", r"/dostavka/zakupka/\d+/(export|labels)"),
    ("POST", r"/api/status/(rozliv|upakovka)/\d+"),
    ("POST", r"/api/status/shipped/.+"),
    ("POST", r"/zakupka/\d+/pay-import"),
    ("POST", r"/dostavka/zakupka/\d+/(create|confirm|box|piece-weight)"),
    ("POST", r"/dostavka/delivery/\d+/(cancel|track)"),
]
_SUPPLIER_RE = [(m, re.compile("^" + p + "$")) for m, p in _SUPPLIER_ALLOWED]


def allowed(user, method, path):
    if user["role"] == ROLE_ADMIN:
        return True
    method = "GET" if method == "HEAD" else method
    return any(m == method and rx.match(path) for m, rx in _SUPPLIER_RE)


# ---------------- CLI на сервере ----------------

if __name__ == "__main__":
    init()
    if len(sys.argv) > 1 and sys.argv[1] == "setup-code":
        print(new_setup_code())
    else:
        print("Использование: python dash_auth.py setup-code")
