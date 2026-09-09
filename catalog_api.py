# -*- coding: utf-8 -*-
# ============================================================
# catalog_api.py — отдача каталога и статики Between
# для сайта betweenatelier.com вместо Cloudflare (заблокирован в РФ).
#
# Что делает:
#   1) включает CORS (allow_origins=["*"]) — чтобы каталог и квиз
#      с betweenatelier.com могли забирать данные с aroma-manager.ru;
#   2) отдаёт готовые JSON-файлы по /catalog?kind=perfume
#      или /catalog?kind=aromadesign — тот же формат, что был у Worker;
#   3) раздаёт папку assets/ как статику по адресу /assets/...
#      (движок каталога between-catalog.js, туман fog.js и любые
#      будущие эффекты — просто клади файл в assets/, код не трогай).
#
# Что где лежит (всё РЯДОМ с app.py, в /opt/aroma-manager):
#   catalog-perfume.json        — данные парфюма
#   catalog-aromadesign.json    — данные аромадизайна
#   assets/between-catalog.js    — движок каталога
#   assets/fog.js                — эффект тумана
#   assets/...                   — любые другие эффекты в будущем
#
# Обновить каталог = перезалить JSON. Добавить эффект = кинуть файл
# в assets/. Код catalog_api.py трогать не нужно.
#
# Как подключается: в app.py одной строкой (см. инструкцию в чате):
#   from catalog_api import setup_catalog; setup_catalog(app)
# ============================================================

import os
import json
from fastapi import Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Папка, где лежит ЭТОТ файл (и рядом — JSON-файлы и папка assets/).
# Абсолютный путь от расположения catalog_api.py: неважно, из какой
# директории стартует uvicorn — файлы всегда находятся.
_HERE = os.path.dirname(os.path.abspath(__file__))

# Разрешённые разделы каталога: kind -> имя файла.
# Новый раздел (например, наборы) — дописать строку и положить файл.
_CATALOG_FILES = {
    "perfume":     "catalog-perfume.json",
    "aromadesign": "catalog-aromadesign.json",
}

# Папка со статикой (движок, эффекты). Создастся автоматически, если её нет.
_ASSETS_DIR = os.path.join(_HERE, "assets")


# Своя обёртка над StaticFiles: заставляет .js отдаваться как
# исполняемый JavaScript. Иначе некоторые конфигурации отдают .js
# как text/plain, и браузер отказывается его выполнять.
class _JsStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith(".js"):
            response.headers["content-type"] = "application/javascript; charset=utf-8"
        # умеренное кэширование (10 минут): обновил файл — за 10 мин разойдётся
        response.headers["cache-control"] = "public, max-age=600"
        return response


def setup_catalog(app):
    """Подключает к приложению CORS, маршрут /catalog и раздачу /assets.
    Вызывается один раз из app.py: setup_catalog(app)."""

    # --- CORS: пускаем все домены (каталог и эффекты публичные) ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,   # со звёздочкой '*' credentials должны быть False
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    # --- маршрут каталога (данные) ---
    @app.get("/catalog")
    async def catalog(kind: str = Query("perfume")):
        filename = _CATALOG_FILES.get(kind)
        if not filename:
            raise HTTPException(
                status_code=404,
                detail="Неизвестный раздел каталога: " + str(kind)
            )

        path = os.path.join(_HERE, filename)
        if not os.path.exists(path):
            raise HTTPException(
                status_code=404,
                detail="Файл каталога не найден на сервере: " + filename
            )

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail="Файл каталога повреждён: " + str(e)
            )

        return JSONResponse(
            content=data,
            media_type="application/json; charset=utf-8",
        )

    # --- раздача папки assets/ как статики ---
    # На случай, если папку ещё не создали — создаём, чтобы монтирование
    # не упало при старте. Файлы в неё положишь отдельно.
    os.makedirs(_ASSETS_DIR, exist_ok=True)
    app.mount("/assets", _JsStaticFiles(directory=_ASSETS_DIR), name="bt_assets")