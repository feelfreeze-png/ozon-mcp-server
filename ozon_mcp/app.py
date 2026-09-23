"""FastAPI-приложение: MCP через SSE + веб-интерфейс (мульти-магазин) + диагностика."""

import os
from datetime import timedelta as _timedelta
import asyncio
import secrets
import uvicorn
from pathlib import Path
from uuid import UUID
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.types import Receive, Scope, Send

from mcp.server.sse import SseServerTransport

from ozon_mcp.server import (
    get_mcp_app, reset_all_clients, reset_shop, set_stats_callback,
    get_seller_for_shop, get_perf_for_shop,
)
from ozon_mcp import (
    catalogue, collector, series, settings as cfg, stocks, timezones,
)
from ozon_mcp import stats
from ozon_mcp import diagnostics as diag
from ozon_mcp import tenancy

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Токен авторизации MCP-эндпоинтов (для доступа извне).
# Пусто = авторизация выключена (только доверенная сеть!).
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

# Интервал фоновой health-проверки, минуты (0 = выключить)
HEALTH_CHECK_INTERVAL_MIN = int(os.environ.get("HEALTH_CHECK_INTERVAL_MIN", "30"))

# Транспорт монтируется как отдельное ASGI-приложение (см. ниже), поэтому путь
# для POST-сообщений объявляется со слешем на конце — так его отдаёт клиенту
# endpoint-событие SSE и так же он смонтирован в роутере.
sse_transport = SseServerTransport("/messages/")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_health_task: asyncio.Task | None = None


# ─── Фоновая диагностика ────────────────────────────────────

async def _run_health_check_all() -> list[dict]:
    """Прогнать полную диагностику по всем магазинам, записать в БД."""
    results = []
    shops = cfg.load_shops(DATA_DIR)
    for shop_id, shop in shops.items():
        if not shop.get("ozon_client_id") or not shop.get("ozon_api_key"):
            continue
        try:
            seller = get_seller_for_shop(shop_id)
            result = await diag.full_diagnostics(shop_id, shop.get("name", shop_id), shop, seller)
            await stats.record_health_check(
                shop_id=shop_id,
                healthy=result["healthy"],
                ping_failures=sum(1 for h in result["hosts"] if not h["ok"]),
                probe_failures=sum(1 for p in result["probes"] if not p["ok"] and not p.get("skipped")),
                warnings=result["warnings"],
                detail=result,
            )
            results.append(result)
        except Exception as e:
            await stats.record_health_check(
                shop_id=shop_id, healthy=False, ping_failures=0, probe_failures=0,
                warnings=[f"Диагностика упала: {type(e).__name__}: {e}"],
            )
    return results


async def _health_loop():
    """Периодическая фоновая проверка всех магазинов."""
    await asyncio.sleep(15)
    while True:
        try:
            await _run_health_check_all()
        except Exception:
            pass
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_MIN * 60)


#: Во сколько по МСК снимать вчерашний день. С запасом после полуночи: Ozon
#: доводит цифры закрытых суток не мгновенно, а ошибиться здесь дорого — окно
#: `products/sku` закрывается через сутки, и пропущенный день невосстановим.
COLLECT_AT_MSK_HOUR = int(os.environ.get("COLLECT_AT_MSK_HOUR", "3"))
COLLECT_ENABLED = os.environ.get("COLLECT_ENABLED", "1") not in ("0", "", "false", "no")

_collect_task: asyncio.Task | None = None
_series_error: str | None = None


def _seconds_until_next_run() -> float:
    """Сколько спать до ближайшего часа сбора по МСК."""
    now = timezones.now_msk()
    target = now.replace(hour=COLLECT_AT_MSK_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += _timedelta(days=1)
    return (target - now).total_seconds()


class _SkipShop(Exception):
    """Магазин пропущен по настройке, а не из-за отказа."""


async def _collect_once() -> None:
    """Один проход сбора по всем магазинам.

    ⚠️ Ошибки НЕ проглатываются, в отличие от соседнего `_health_loop`: пропущенные
    сутки рекламной статистики не восстанавливаются ничем, поэтому отказ обязан
    попасть и в журнал прогонов, и в лог контейнера.
    """
    if not series.is_enabled():
        print(f"СБОР НЕ ИДЁТ: хранилище ряда недоступно ({_series_error}). "
              "Сутки рекламной статистики теряются безвозвратно.", flush=True)
        return
    db = series.connection()
    day = timezones.yesterday_msk()
    for shop in cfg.get_shop_list(DATA_DIR):
        shop_id = shop["id"]
        can = shop["can"]
        if not can["performance"]:
            # Не «провалился», а «не настроен». Разница в том, надо ли идти чинить.
            print(f"магазин {shop_id}: ключей Performance нет — рекламная статистика "
                  f"НЕ собирается. Это настройка, а не отказ; но сутки, прошедшие без "
                  f"ключей, восстановить будет нечем.", flush=True)
        try:
            if not can["performance"]:
                raise _SkipShop
            perf = get_perf_for_shop(shop_id)
            result = await collector.collect_day(perf, db, shop_id=shop_id, day=day)
            print(f"сбор {shop_id} за {day}: строк {result.rows_written}, "
                  f"SKU {result.distinct_sku}, кампаний {result.campaigns}, "
                  f"расход {result.expense_total}", flush=True)
            if result.unknown_fields:
                print(f"  ⚠️ Ozon прислал незнакомые поля: "
                      f"{sorted(result.unknown_fields)}", flush=True)
        except _SkipShop:
            pass
        except Exception as exc:
            # Гасим здесь только ради соседних магазинов: отказ уже записан в
            # collection_run как failed и напечатан. Молчаливого пропуска нет.
            print(f"СБОР ПРОВАЛЕН {shop_id} за {day}: {type(exc).__name__}: {exc}",
                  flush=True)

        # Товары и остатки. Порядок значим: таблица товаров даёт перечень SKU, по
        # которому снимается остаток. Снимок берётся за СЕГОДНЯ, а не за вчера:
        # синхронные ручки показывают состояние на сейчас, прошлого у них нет.
        if not can["seller"]:
            print(f"магазин {shop_id}: ключей Seller нет — каталог и остатки пропущены. "
                  f"Это настройка, а не отказ. Без них не будет названий товаров, "
                  f"проверки «заказы без остатка» и доли рекламных заказов; остатки "
                  f"бэкфиллятся позже, рекламная статистика — нет.", flush=True)
            continue

        try:
            seller = get_seller_for_shop(shop_id)
            table = await catalogue.rebuild(seller, db, shop_id=shop_id)
            print(f"товары {shop_id}: карточек {table.products} "
                  f"(активных {table.active}, архивных {table.archived}), "
                  f"строк sku {table.skus}", flush=True)
            for key in ("без второго источника, всего", "незнакомые схемы"):
                if key in table.detail:
                    print(f"  ⚠️ {key}: {table.detail[key]}", flush=True)

            async with db.execute(
                "SELECT DISTINCT sku FROM product_sku WHERE shop_id = ?", (shop_id,)
            ) as cur:
                skus = [row[0] for row in await cur.fetchall()]
            snapshot = await stocks.snapshot_day(seller, db, shop_id=shop_id, skus=skus)
            print(f"остатки {shop_id} за {snapshot.day_msk}: строк "
                  f"{snapshot.rows_written}, sku со строками {snapshot.skus_with_rows} "
                  f"из {snapshot.requested}, складов {snapshot.warehouses}", flush=True)
            if not snapshot.ok:
                print(f"  СНИМОК НЕПОЛОН: {snapshot.error}", flush=True)
                # ⚠️ Причина отказа СЧИТАЕТСЯ в `stocks.snapshot_day`, но до 22.09.2026
                # печаталось только «порций не снялось: 1» — без единого слова о том,
                # почему. Разбирать неполный снимок приходилось наугад: сам отказ
                # виден, его причина потеряна между вычислением и логом.
                for reason in snapshot.detail.get("неудавшиеся порции") or []:
                    print(f"    порция не снялась: {reason}", flush=True)
        except Exception as exc:
            print(f"ОСТАТКИ ПРОВАЛЕНЫ {shop_id}: {type(exc).__name__}: {exc}",
                  flush=True)


#: Когда ближайший сбор — чтобы это можно было спросить снаружи, а не выводить из
#: отсутствия жалоб. Заполняется циклом перед каждым засыпанием.
_next_collect_at: str | None = None

#: Почему цикл сбора остановился. `None` — значит работает. Непустое значение
#: переживает смерть задачи и отвечает на вопрос «почему ряд перестал расти».
_collect_stopped: str | None = None


async def _collect_loop() -> None:
    """Раз в сутки по МСК, с запасом после полуночи.

    🔴 **Цикл обязан пережить неудачный день.** `asyncio.Task`, упавшая с исключением,
    умирает молча: сервер продолжает отвечать, healthcheck зелёный, а ряд просто
    перестаёт расти — и узнать об этом можно будет только по блоку покрытия в отчёте,
    через сутки или через неделю. Поэтому исключение здесь печатается громко, и цикл
    идёт дальше: пропущенные сутки рекламной статистики не восстанавливаются ничем,
    и терять из-за одного отказа ещё и все последующие — худший обмен из возможных.

    `CancelledError` наружу пропускается: это штатная остановка при выключении.
    """
    global _next_collect_at, _collect_stopped
    while True:
        delay = _seconds_until_next_run()
        _next_collect_at = (timezones.now_msk() + _timedelta(seconds=delay)).isoformat()
        print(f"сбор: следующий проход {_next_collect_at} "
              f"(через {delay / 3600:.1f} ч)", flush=True)
        await asyncio.sleep(delay)
        try:
            await _collect_once()
        except asyncio.CancelledError:
            _collect_stopped = "остановлен штатно"
            raise
        except BaseException as exc:
            _collect_stopped = f"{type(exc).__name__}: {exc}"
            print(f"СБОР УПАЛ ЦЕЛИКОМ: {_collect_stopped}. Цикл продолжает работу, "
                  f"следующая попытка завтра — но сегодняшние сутки потеряны и не "
                  f"восстановятся.", flush=True)
        else:
            _collect_stopped = None


def _watch_collect_task(task: asyncio.Task) -> None:
    """Последний рубеж: сказать вслух, если задача сбора вообще завершилась.

    До этого её никто не ждал, а значит её смерть была неотличима от работы.
    """
    global _collect_stopped
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _collect_stopped = f"{type(exc).__name__}: {exc}"
        print(f"ЗАДАЧА СБОРА ПОГИБЛА: {_collect_stopped}. Ряд больше НЕ пополняется "
              f"до перезапуска сервера.", flush=True)
    else:
        _collect_stopped = "задача завершилась без ошибки — такого быть не должно"
        print(f"ЗАДАЧА СБОРА ЗАВЕРШИЛАСЬ: {_collect_stopped}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _health_task
    if not ADMIN_TOKEN:
        # Громко и на старте: молчаливо открытая админка — это то, что нельзя заметить
        # по поведению. Сервер работает одинаково и с токеном, и без.
        print("ВНИМАНИЕ: ADMIN_TOKEN не задан — веб-интерфейс открыт всем, кто дотянулся "
              "до порта: список магазинов, заведение и удаление, статистика арендаторов. "
              "Допустимо только на localhost в закрытом контуре.", flush=True)
    if ADMIN_TOKEN and not ADMIN_TOKEN.isascii():
        # Cookie по стандарту латинская: вход через форму для такого токена
        # невозможен, и узнать об этом лучше на старте, чем на странице входа.
        print("ВНИМАНИЕ: ADMIN_TOKEN содержит нелатинские символы. Заголовок и "
              "?token= работают, а вход через форму — нет: cookie такого значения "
              "не несёт. Смените токен на латиницу с цифрами.", flush=True)
    await stats.init_db(DATA_DIR)
    set_stats_callback(stats.record_call)
    global _series_error
    try:
        await series.init_db(DATA_DIR)
        _series_error = None
    except Exception as exc:
        # Громко и на старте. Сервер продолжает отвечать на чтение, но накопление
        # ряда встало — а это единственное, что невосстановимо.
        _series_error = f"{type(exc).__name__}: {exc}"
        print(f"ВНИМАНИЕ: хранилище ряда не открылось ({_series_error}). "
              "Ежедневный сбор рекламы НЕ ПОЙДЁТ, и пропущенные сутки не вернуть.",
              flush=True)
    global _collect_task
    if COLLECT_ENABLED:
        _collect_task = asyncio.create_task(_collect_loop())
        _collect_task.add_done_callback(_watch_collect_task)
    else:
        # Выключенный сбор снаружи неотличим от работающего: сервер отвечает так же.
        print("ВНИМАНИЕ: ежедневный сбор ВЫКЛЮЧЕН (COLLECT_ENABLED). Ряд не растёт.",
              flush=True)
    if HEALTH_CHECK_INTERVAL_MIN > 0:
        _health_task = asyncio.create_task(_health_loop())
    yield
    if _health_task:
        _health_task.cancel()
    if _collect_task:
        _collect_task.cancel()
    await reset_all_clients()
    await stats.close_db()
    await series.close_db()


fastapi_app = FastAPI(lifespan=lifespan)


# ─── Авторизация MCP-эндпоинтов ─────────────────────────────

#: Имя cookie со входом в админку.
ADMIN_COOKIE = "ozon_admin"

#: Сколько живёт вход. Сутки: админка нужна эпизодически, а бессрочная cookie на
#: рабочей машине — это тот же токен в открытом виде, только дольше.
ADMIN_COOKIE_MAX_AGE = 24 * 3600


def _request_token(request: Request) -> str:
    """Токен: заголовок `Authorization`, затем cookie, затем `?token=`.

    🔴 **Порядок не случаен, и `?token=` намеренно последний.** Адрес с токеном
    оседает в истории браузера и подставляется при следующем открытии — то есть
    секрет переживает сессию в месте, которое никто не чистит. Для curl и для
    MCP-клиентов, не умеющих ставить заголовок, этот путь оставлен: он не хуже
    заголовка там, где адрес нигде не сохраняется.

    Браузеру предназначена cookie: она ставится ответом на форму входа, помечена
    `HttpOnly` (скрипт страницы её не прочитает) и `SameSite=strict` (чужой сайт
    не заставит браузер её отправить).
    """
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get(ADMIN_COOKIE, "")
    if not token:
        token = request.query_params.get("token", "")
    return token


def _resolve_mcp_auth(request: Request) -> tuple[bool, tuple[str, ...] | None]:
    """`(допущен, привязанные shop_id)` для /sse и /messages.

    Личные токены клиентов, если заданы, ПОЛНОСТЬЮ вытесняют общий MCP_AUTH_TOKEN.
    Иначе общий остался бы входом без привязки к магазину — то есть ровно той дырой,
    ради которой режим и вводится: достаточно было бы предъявить его вместо своего,
    чтобы снова выбирать магазин аргументом.

    Магазинов у токена может быть несколько (`MCP_CLIENT_TOKENS=tok:shop1|shop2`) —
    для владельца нескольких кабинетов это один клиент, а не несколько.
    """
    token = _request_token(request)
    if tenancy.is_enabled():
        shops = tenancy.resolve(token)
        return shops is not None, shops
    if not MCP_AUTH_TOKEN:
        return True, None
    return secrets.compare_digest(token, MCP_AUTH_TOKEN), None


def _check_mcp_auth(request: Request) -> bool:
    """Проверка Bearer-токена для /sse и /messages. Без MCP_AUTH_TOKEN — пропуск."""
    return _resolve_mcp_auth(request)[0]


# ─── Авторизация веб-интерфейса ─────────────────────────────
#
# До появления ADMIN_TOKEN токен проверяли ТОЛЬКО /sse и /messages, а весь веб-интерфейс
# был открыт: `GET /shops` отдавал список магазинов, `POST /api/shops` заводил новый,
# `DELETE /api/shops/{id}` удалял, `/api/stats` показывал вызовы всех арендаторов.
# Привязка клиента к магазину этого не закрывает и не пытается — она про MCP-сессию,
# а не про админку: сосед не стал бы подбирать shop_id, он открыл бы /shops.

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Пути, которых охрана не касается, и почему именно они:
#   /sse, /messages — у них свой механизм (MCP_CLIENT_TOKENS / MCP_AUTH_TOKEN), и
#     закрывать их вторым замком значило бы требовать от MCP-клиента админский токен;
#   /api/health — его дёргает healthcheck самого контейнера (docker-compose.yml), у
#     которого токена нет и быть не должно. Поэтому без токена он отдаёт сокращённый
#     ответ: живость видно, состав проверок — нет.
_MCP_PREFIXES = ("/sse", "/messages")
_LIVENESS_PATH = "/api/health"
_LOGIN_PATH = "/login"


def _same_secret(given: str, expected: str) -> bool:
    """Сравнение постоянного времени, безопасное для любых символов.

    🔴 `secrets.compare_digest` на строках требует ASCII и на кириллице бросает
    `TypeError`. В охране админки это означало не отказ, а **500**: токен с
    нелатинскими символами ронял весь веб-интерфейс вместо того, чтобы не пустить.
    Найдено тестом 22.09.2026. Сравниваем байты — у них такого ограничения нет.
    """
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _check_admin_auth(request: Request) -> bool:
    """Допущен ли запрос к админской поверхности."""
    if not ADMIN_TOKEN:
        return True
    return _same_secret(_request_token(request), ADMIN_TOKEN)


@fastapi_app.middleware("http")
async def _guard_admin_surface(request: Request, call_next):
    """Закрыть весь веб-интерфейс, кроме двух намеренных исключений.

    Охрана стоит списком ИСКЛЮЧЕНИЙ, а не списком защищаемых маршрутов: новый
    эндпоинт тогда защищён по умолчанию, а не до тех пор, пока про него не забыли.
    Именно забывчивость и сделала эту дыру — `/api/key-expiry` добавлялся уже после
    того, как стало известно, что админка открыта.
    """
    path = request.url.path
    if (not ADMIN_TOKEN or path.startswith(_MCP_PREFIXES)
            or path in (_LIVENESS_PATH, _LOGIN_PATH)):
        return await call_next(request)
    if _check_admin_auth(request):
        return await call_next(request)
    # Человеку в браузере отдаём форму, а не слово «Unauthorized»: голый 401 не
    # говорит, ЧТО делать, и выглядит поломкой сервера, а не отсутствием входа.
    # Машине — прежний 401, чтобы curl и скрипты не разбирали HTML.
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(_LOGIN_PATH, status_code=303)
    return Response("Unauthorized", status_code=401)


def _is_live_session(request: Request) -> bool:
    """POST относится к уже авторизованной SSE-сессии?

    session_id выдаётся только по успешно авторизованному GET /sse, то есть сам
    работает как одноразовый секрет. Нужно для клиентов, которые авторизуются
    через ?token=... : в endpoint-событии токена нет, и они не могут повторить
    его в POST-запросе.
    """
    sid = request.query_params.get("session_id", "")
    if not sid:
        return False
    writers = getattr(sse_transport, "_read_stream_writers", {})
    try:
        return UUID(hex=sid) in writers
    except ValueError:
        return False


# ─── MCP SSE endpoints ──────────────────────────────────────

@fastapi_app.get("/sse")
async def sse_endpoint(request: Request):
    allowed, shops = _resolve_mcp_auth(request)
    if not allowed:
        return Response("Unauthorized", status_code=401)
    # Привязка ставится ДО mcp_app.run: вызовы инструментов исполняются внутри его
    # цикла, в этой же задаче, и подхватывают контекст сами. POST /messages только
    # кладёт сообщение в поток сессии, своего контекста у него нет.
    pin = tenancy.pin(shops)
    try:
        mcp_app = get_mcp_app()
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_app.run(read_stream, write_stream, mcp_app.create_initialization_options())
    finally:
        tenancy.unpin(pin)
    # Пустой ответ обязателен: без него Starlette попытается вызвать None как
    # ASGI-приложение после закрытия SSE-потока.
    return Response()


async def _messages_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    """POST клиентских сообщений — отдельное ASGI-приложение.

    ВАЖНО: handle_post_message сам отправляет ASGI-ответ. Если завернуть его в
    обычный маршрут FastAPI, фреймворк отправит ответ второй раз и соединение
    рвётся с `RuntimeError: Unexpected ASGI message 'http.response.start' sent,
    after response already completed` (у клиента — httpx.ReadError на initialize).
    Поэтому транспорт монтируется через Mount, а авторизация проверяется здесь
    вручную — middleware FastAPI-маршрута тут нет.
    """
    request = Request(scope, receive)
    if not (_check_mcp_auth(request) or _is_live_session(request)):
        await Response("Unauthorized", status_code=401)(scope, receive, send)
        return
    await sse_transport.handle_post_message(scope, receive, send)


fastapi_app.mount("/messages", _messages_asgi)


# ─── Веб-интерфейс ──────────────────────────────────────────

@fastapi_app.get(_LOGIN_PATH, response_class=HTMLResponse)
async def login_form(request: Request):
    """Форма входа. Единственная страница, открытая без токена."""
    if not ADMIN_TOKEN:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@fastapi_app.post(_LOGIN_PATH, response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    token = str(form.get("token") or "").strip()
    # Сравнение постоянного времени: обычное `==` отвечает тем быстрее, чем раньше
    # расходятся строки, и по времени ответа токен подбирается посимвольно.
    if not (ADMIN_TOKEN and _same_secret(token, ADMIN_TOKEN)):
        # Ошибка одна на оба случая — и на пустой ввод, и на неверный токен:
        # «токен неверный» против «токен не введён» подсказывало бы подбирающему,
        # что он хотя бы в правильном поле.
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный токен"}, status_code=401)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True, samesite="strict", path="/")
    return response


@fastapi_app.get("/logout")
async def logout():
    response = RedirectResponse(_LOGIN_PATH, status_code=303)
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response


@fastapi_app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    shop_filter = request.query_params.get("shop", None)
    summary = await stats.get_summary(shop_id=shop_filter)
    degradations = await stats.get_tool_degradations()
    health = await stats.get_health_history(limit=1)
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": summary, "current_shop": shop_filter or "",
        "degradations": degradations,
        "last_health": health[0] if health else None,
    })


@fastapi_app.get("/shops", response_class=HTMLResponse)
async def shops_page(request: Request):
    shops = cfg.load_shops(DATA_DIR)
    masked = {sid: cfg.get_masked_shop(s) for sid, s in shops.items()}
    return templates.TemplateResponse(request, "shops.html", {"shops": masked})


@fastapi_app.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request):
    """Страница диагностики: ключи, хосты, пробы, деградации, история."""
    shops = cfg.load_shops(DATA_DIR)
    shop_data = []
    for shop_id, shop in shops.items():
        last = await stats.get_last_health(shop_id)
        shop_data.append({
            "id": shop_id,
            "name": shop.get("name", shop_id),
            "seller_keys_set": bool(shop.get("ozon_client_id") and shop.get("ozon_api_key")),
            "perf_keys_set": bool(shop.get("ozon_perf_client_id") and shop.get("ozon_perf_client_secret")),
            "last_check": last,
        })
    degradations = await stats.get_tool_degradations()
    history = await stats.get_health_history(limit=30)
    return templates.TemplateResponse(request, "diagnostics.html", {
        "shops": shop_data,
        "degradations": degradations,
        "history": history,
        "interval_min": HEALTH_CHECK_INTERVAL_MIN,
    })


@fastapi_app.post("/api/diagnostics/run")
async def run_diagnostics_now():
    """Запустить диагностику всех магазинов прямо сейчас."""
    results = await _run_health_check_all()
    return JSONResponse({
        "ok": True,
        "shops_checked": len(results),
        "results": [
            {"shop_id": r["shop_id"], "healthy": r["healthy"], "warnings": r["warnings"]}
            for r in results
        ],
    })


@fastapi_app.get("/api/diagnostics/{shop_id}")
async def api_diagnostics_shop(shop_id: str):
    """Полная диагностика конкретного магазина (живой запрос)."""
    shops = cfg.load_shops(DATA_DIR)
    if shop_id not in shops:
        return JSONResponse({"ok": False, "error": "Магазин не найден"}, status_code=404)
    shop = shops[shop_id]
    if not shop.get("ozon_client_id") or not shop.get("ozon_api_key"):
        return JSONResponse({"ok": False, "error": "Ключи Seller API не заданы"}, status_code=400)
    seller = get_seller_for_shop(shop_id)
    result = await diag.full_diagnostics(shop_id, shop.get("name", shop_id), shop, seller)
    await stats.record_health_check(
        shop_id=shop_id, healthy=result["healthy"],
        ping_failures=sum(1 for h in result["hosts"] if not h["ok"]),
        probe_failures=sum(1 for p in result["probes"] if not p["ok"] and not p.get("skipped")),
        warnings=result["warnings"], detail=result,
    )
    return JSONResponse(result)


@fastapi_app.post("/api/shops")
async def save_shop(request: Request):
    data = await request.json()
    shop_id = data.get("shop_id", "").strip()
    if not shop_id:
        return JSONResponse({"ok": False, "error": "shop_id обязателен"}, status_code=400)

    shops = cfg.load_shops(DATA_DIR)
    existing = shops.get(shop_id, {})

    shop = {"name": data.get("name", shop_id)}
    for key in cfg.SHOP_KEYS:
        val = data.get(key, "")
        if val and "***" not in val:
            shop[key] = val
        elif key in existing:
            shop[key] = existing[key]
    shops[shop_id] = shop
    cfg.save_shops(DATA_DIR, shops)
    await reset_shop(shop_id)
    return JSONResponse({"ok": True})


@fastapi_app.delete("/api/shops/{shop_id}")
async def delete_shop(shop_id: str):
    shops = cfg.load_shops(DATA_DIR)
    if shop_id not in shops:
        return JSONResponse({"ok": False, "error": "Магазин не найден"}, status_code=404)
    del shops[shop_id]
    cfg.save_shops(DATA_DIR, shops)
    await reset_shop(shop_id)
    return JSONResponse({"ok": True})


@fastapi_app.post("/api/shops/{shop_id}/test")
async def test_shop_connection(shop_id: str):
    shops = cfg.load_shops(DATA_DIR)
    if shop_id not in shops:
        return JSONResponse({"ok": False, "error": "Магазин не найден"}, status_code=404)

    shop = shops[shop_id]
    results = {"seller_ok": False, "perf_ok": False, "errors": []}

    from ozon_mcp.client import OzonSellerClient, OzonPerformanceClient

    cid = shop.get("ozon_client_id", "")
    akey = shop.get("ozon_api_key", "")
    if cid and akey:
        c = OzonSellerClient(cid, akey)
        try:
            await c.rating_summary()
            results["seller_ok"] = True
        except Exception as e:
            results["errors"].append(f"Seller API: {e}")
        finally:
            await c.close()
    else:
        results["errors"].append("Seller API: ключи не заданы")

    pid = shop.get("ozon_perf_client_id", "")
    psecret = shop.get("ozon_perf_client_secret", "")
    if pid and psecret:
        p = OzonPerformanceClient(pid, psecret)
        try:
            await p.campaigns_list()
            results["perf_ok"] = True
        except Exception as e:
            results["errors"].append(f"Performance API: {e}")
        finally:
            await p.close()
    else:
        results["errors"].append("Performance API: ключи не заданы")

    return JSONResponse(results)


@fastapi_app.get("/api/stats")
async def api_stats(shop: str | None = None):
    return JSONResponse(await stats.get_summary(shop_id=shop))


@fastapi_app.get("/api/key-expiry")
async def key_expiry():
    """Срок жизни Seller-ключей всех магазинов.

    Ключ Seller API живёт три месяца, после чего вызовы начинают падать — молча с
    точки зрения любого внешнего механизма. Эндпоинт существует, чтобы сторож снаружи
    мог узнать срок, **не получая самих ключей**: они остаются в шифрованном сторе, а
    наружу уходит только дата.

    ⚠️ `state` различает три вещи, и это главное свойство ответа:
    `ok` — срок известен и не близок; `expired` — истёк; `unknown` — **спросить не
    удалось**. Последнее нельзя сворачивать в «наверное, ок»: сеть, протухший ключ и
    смена схемы ответа выглядят одинаково молча, и именно так выглядела бы авария,
    ради которой сторож и заводится.
    """
    import datetime

    shops = cfg.load_shops(DATA_DIR)
    out: list[dict] = []
    for shop_id, shop in shops.items():
        if not shop.get("ozon_client_id") or not shop.get("ozon_api_key"):
            out.append({"shop_id": shop_id, "state": "unknown",
                        "reason": "Seller API: ключи не заданы"})
            continue
        client = get_seller_for_shop(shop_id)
        try:
            data = await client.roles()
        except Exception as e:
            out.append({"shop_id": shop_id, "state": "unknown",
                        "reason": f"{type(e).__name__}: {e}"})
            continue
        raw = data.get("expires_at")
        if not raw:
            out.append({"shop_id": shop_id, "state": "unknown",
                        "reason": "в ответе /v1/roles нет expires_at"})
            continue
        try:
            when = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            out.append({"shop_id": shop_id, "state": "unknown",
                        "reason": f"не разобрана дата: {raw!r}"})
            continue
        left = when - datetime.datetime.now(datetime.timezone.utc)
        days = left.days
        out.append({
            "shop_id": shop_id,
            "state": "ok" if days > 0 else "expired",
            "expires_at": when.isoformat(),
            "days_left": days,
            "roles": [r.get("name") for r in data.get("roles", []) if isinstance(r, dict)],
        })
    return JSONResponse({"shops": out})


@fastapi_app.get("/api/health")
async def health(request: Request):
    """Здоровье самого сервиса + сводка последних проверок Ozon API.

    Единственный маршрут, доступный без админского токена: его дёргает healthcheck
    контейнера, у которого токена нет и быть не должно. Поэтому без токена ответ
    сокращён до живости — состав проверок и перечень деградаций называют магазины и
    подробности отказов, и отдавать их кому угодно незачем.
    """
    if not _check_admin_auth(request):
        return JSONResponse({"status": "ok"})
    history = await stats.get_health_history(limit=5)
    degradations = await stats.get_tool_degradations()
    return JSONResponse({
        "status": "ok",
        "auth_enabled": bool(MCP_AUTH_TOKEN),
        "admin_auth_enabled": bool(ADMIN_TOKEN),
        "health_check_interval_min": HEALTH_CHECK_INTERVAL_MIN,
        # Состояние сборщика спрашивается, а не выводится из отсутствия жалоб.
        # `collect_alive: false` при зелёном `status` — ровно тот случай, ради
        # которого поле и заведено: сервис жив, а ряд не растёт.
        "collect_enabled": COLLECT_ENABLED,
        "collect_alive": bool(_collect_task and not _collect_task.done()),
        "collect_next_at_msk": _next_collect_at,
        "collect_stopped_reason": _collect_stopped,
        "series_error": _series_error,
        "recent_checks": history,
        "degraded_tools": degradations,
    })


# ─── Точка входа ─────────────────────────────────────────────

def main():
    uvicorn.run(
        "ozon_mcp.app:fastapi_app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
