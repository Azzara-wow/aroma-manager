import sqlite3
from datetime import datetime

DB_PATH = "data.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Таблица покупателей
    c.execute('''
        CREATE TABLE IF NOT EXISTS buyers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            phone TEXT DEFAULT '',
            address TEXT DEFAULT '',
            middle_name TEXT DEFAULT ''
        )
    ''')

    # Проверяем, есть ли уже столбец middle_name (для существующих баз)
    try:
        c.execute("SELECT middle_name FROM buyers LIMIT 1")
    except sqlite3.OperationalError:
        # Если столбца нет, добавляем
        c.execute("ALTER TABLE buyers ADD COLUMN middle_name TEXT DEFAULT ''")

    # Таблица закупок
    c.execute('''
        CREATE TABLE IF NOT EXISTS zakupkas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            google_sheet_url TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT ''
        )
    ''')

    # Таблица позиций закупки
    c.execute('''
        CREATE TABLE IF NOT EXISTS zakaz_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zakupka_id INTEGER NOT NULL,
            buyer_name TEXT NOT NULL,
            aroma_name TEXT NOT NULL,
            volume_ml INTEGER NOT NULL,
            price_per_10ml REAL NOT NULL,
            total_sum REAL NOT NULL,
            FOREIGN KEY (zakupka_id) REFERENCES zakupkas (id)
        )
    ''')

    # Таблица заказов с наличия
    c.execute('''
        CREATE TABLE IF NOT EXISTS nalichie_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zakupka_id INTEGER,
            buyer_name TEXT NOT NULL,
            aroma_name TEXT NOT NULL,
            volume_ml INTEGER NOT NULL,
            price REAL NOT NULL,
            created_at TEXT DEFAULT '',
            FOREIGN KEY (zakupka_id) REFERENCES zakupkas (id)
        )
    ''')

    # Таблица статусов
    c.execute('''
        CREATE TABLE IF NOT EXISTS statuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zakaz_item_id INTEGER,
            nalichie_order_id INTEGER,
            rozliv INTEGER DEFAULT 0,
            upakovka INTEGER DEFAULT 0,
            payment_zakupka INTEGER DEFAULT 0,
            payment_nalichie INTEGER DEFAULT 0,
            shipped INTEGER DEFAULT 0,
            FOREIGN KEY (zakaz_item_id) REFERENCES zakaz_items (id),
            FOREIGN KEY (nalichie_order_id) REFERENCES nalichie_orders (id)
        )
    ''')

    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Инициализация при старте
init_db()