"""Тесты веб-эндпоинтов."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    import ozon_mcp.app as app_module
    import ozon_mcp.server as server_module
    app_module.DATA_DIR = tmp_path
    server_module.DATA_DIR = tmp_path
    from ozon_mcp.app import fastapi_app
    with TestClient(fastapi_app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    # /api/health отдаёт ещё и сводку диагностики — проверяем ключевые поля
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_enabled"] is False


def test_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_shops_page(client):
    r = client.get("/shops")
    assert r.status_code == 200
    assert "Магазины" in r.text


def test_add_shop(client):
    r = client.post("/api/shops", json={
        "shop_id": "test1",
        "name": "Test Shop",
        "ozon_client_id": "111",
        "ozon_api_key": "222",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Проверим что появился
    r2 = client.get("/shops")
    assert "Test Shop" in r2.text


def test_add_shop_no_id(client):
    r = client.post("/api/shops", json={"name": "No ID"})
    assert r.status_code == 400


def test_delete_shop(client):
    client.post("/api/shops", json={"shop_id": "del1", "name": "To Delete"})
    r = client.delete("/api/shops/del1")
    assert r.status_code == 200

    r2 = client.delete("/api/shops/del1")
    assert r2.status_code == 404


def test_stats_api(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "total" in data
    assert "shops" in data


def test_stats_api_with_shop_filter(client):
    r = client.get("/api/stats?shop=test")
    assert r.status_code == 200


# ── Сборщик: его состояние спрашивается, а не выводится из молчания ──────────


def test_health_reports_that_collection_is_alive(client):
    """🔴 Зелёный сервис и растущий ряд — разные утверждения.

    `asyncio.Task` сборщика, упавшая с исключением, умирает молча: сервер отвечает
    как обычно, healthcheck контейнера зелёный, а ряд просто перестаёт расти. До этой
    правки узнать об этом было неоткуда, кроме блока покрытия в отчёте — то есть
    через сутки в лучшем случае.
    """
    body = client.get("/api/health").json()
    assert body["collect_enabled"] is True
    assert body["collect_alive"] is True, "задача сбора не запущена"
    assert body["collect_stopped_reason"] is None
    assert body["collect_next_at_msk"], "время ближайшего сбора не названо"


def test_health_says_when_the_next_collection_is(client):
    """Время ближайшего прохода — по Москве и с поясом, как весь ряд."""
    at = client.get("/api/health").json()["collect_next_at_msk"]
    assert at.endswith("+03:00"), f"метка без московского пояса: {at}"


def test_a_dead_collector_is_visible_in_health(client):
    """Ради этого случая поле и заведено: сервис жив, а ряд не растёт."""
    import ozon_mcp.app as app_module

    app_module._collect_task.cancel()
    app_module._collect_stopped = "RuntimeError: ряд не открылся"

    body = client.get("/api/health").json()
    assert body["collect_alive"] is False
    assert "RuntimeError" in body["collect_stopped_reason"]
    assert body["status"] == "ok", "живость сервиса и живость сбора — разные вещи"


@pytest.mark.asyncio
async def test_the_loop_survives_a_failed_day(monkeypatch):
    """Потерять сутки из-за отказа плохо; потерять из-за них ВСЕ последующие — хуже."""
    import asyncio

    import ozon_mcp.app as app_module

    calls = []

    async def failing_once():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("Ozon недоступен")

    async def _no_wait(_seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(app_module, "_collect_once", failing_once)
    monkeypatch.setattr(app_module.asyncio, "sleep", _no_wait)

    with pytest.raises(asyncio.CancelledError):
        await app_module._collect_loop()

    assert len(calls) == 2, "цикл не пережил неудачный день"
    assert app_module._collect_stopped is None, "успех обязан снимать прежнюю причину"


@pytest.mark.asyncio
async def test_a_failed_day_is_printed_loudly(monkeypatch, capsys):
    import asyncio

    import ozon_mcp.app as app_module

    async def failing_once():
        raise RuntimeError("Ozon недоступен")

    async def _no_wait(_seconds):
        if "СБОР УПАЛ ЦЕЛИКОМ" in capsys.readouterr().out:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(app_module, "_collect_once", failing_once)
    monkeypatch.setattr(app_module.asyncio, "sleep", _no_wait)
    with pytest.raises(asyncio.CancelledError):
        await app_module._collect_loop()
    assert app_module._collect_stopped.startswith("RuntimeError")


# ── Форма входа ──────────────────────────────────────────────────────────────

#: Латиница: cookie по стандарту не несёт других символов (см. сторож ниже).
ADMIN = "s3cret-token"


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """Сервер с включённой админской охраной — так он и стоит на проде."""
    import ozon_mcp.app as app_module
    import ozon_mcp.server as server_module

    app_module.DATA_DIR = tmp_path
    server_module.DATA_DIR = tmp_path
    monkeypatch.setattr(app_module, "ADMIN_TOKEN", ADMIN)
    from ozon_mcp.app import fastapi_app
    with TestClient(fastapi_app, follow_redirects=False) as c:
        yield c


def test_a_browser_without_a_token_is_sent_to_the_form(guarded):
    """🔴 Голый 401 не говорит, ЧТО делать, и выглядит поломкой сервера.

    Ровно это и случилось живьём 22.09.2026: страница `/shops` отдала слово
    «Unauthorized», и человек не мог понять, сломан сервер или нужен вход.
    """
    r = guarded.get("/shops", headers={"accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_a_script_without_a_token_still_gets_401(guarded):
    """curl и скрипты не должны разбирать HTML, чтобы понять, что их не пустили."""
    r = guarded.get("/api/stats")
    assert r.status_code == 401
    assert r.text == "Unauthorized"


def test_the_form_itself_is_reachable_without_a_token(guarded):
    r = guarded.get("/login")
    assert r.status_code == 200 and "Админский токен" in r.text


def test_a_correct_token_sets_a_cookie_and_lets_in(guarded):
    r = guarded.post("/login", data={"token": ADMIN})
    assert r.status_code == 303 and r.headers["location"] == "/"

    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie, "скрипт страницы не должен читать вход"
    assert "samesite=strict" in cookie.lower(), "чужой сайт не должен слать нашу cookie"

    from ozon_mcp.app import ADMIN_COOKIE

    guarded.cookies.set(ADMIN_COOKIE, ADMIN)
    assert guarded.get("/shops", headers={"accept": "text/html"}).status_code == 200


@pytest.mark.parametrize("bad", ["", "  ", "не тот", ADMIN[:-1], ADMIN + "x"])
def test_a_wrong_token_is_refused_the_same_way(guarded, bad):
    """Одна ошибка на все случаи: разные тексты подсказывали бы подбирающему."""
    r = guarded.post("/login", data={"token": bad})
    assert r.status_code == 401
    assert "Неверный токен" in r.text
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_logout_removes_the_cookie(guarded):
    from ozon_mcp.app import ADMIN_COOKIE

    guarded.cookies.set(ADMIN_COOKIE, ADMIN)
    r = guarded.get("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "ozon_admin=" in r.headers["set-cookie"]
    assert "Max-Age=0" in r.headers["set-cookie"] or "expires" in r.headers["set-cookie"].lower()


def test_the_old_query_token_keeps_working(guarded):
    """Его нельзя убрать: MCP-клиенты, не умеющие заголовок, ходят только так."""
    r = guarded.get("/shops?token=" + ADMIN + "", headers={"accept": "text/html"})
    assert r.status_code == 200


def test_the_header_wins_over_a_stale_cookie(guarded):
    """Порядок источников: заголовок, потом cookie, потом адрес."""
    from ozon_mcp.app import ADMIN_COOKIE

    guarded.cookies.set(ADMIN_COOKIE, "stale-token")
    r = guarded.get("/shops", headers={"accept": "text/html",
                                       "authorization": "Bearer " + ADMIN})
    assert r.status_code == 200


def test_health_stays_open_without_any_token(guarded):
    """Его дёргает healthcheck контейнера, у которого токена нет и быть не должно."""
    assert guarded.get("/api/health").status_code == 200


def test_a_pasted_token_with_stray_spaces_is_accepted(guarded):
    """Копирование из терминала приносит пробел на конце — это не повод не пустить."""
    r = guarded.post("/login", data={"token": f"  {ADMIN}  "})
    assert r.status_code == 303


def test_a_non_ascii_token_refuses_instead_of_crashing(monkeypatch, tmp_path):
    """🔴 `secrets.compare_digest` на строках требует ASCII и иначе бросает TypeError.

    В охране админки это означало не отказ, а 500: весь веб-интерфейс падал вместо
    того, чтобы не пустить. Найдено этим тестом 22.09.2026.
    """
    import ozon_mcp.app as app_module
    import ozon_mcp.server as server_module

    app_module.DATA_DIR = tmp_path
    server_module.DATA_DIR = tmp_path
    monkeypatch.setattr(app_module, "ADMIN_TOKEN", "кириллический-токен")
    from ozon_mcp.app import fastapi_app

    with TestClient(fastapi_app, follow_redirects=False) as c:
        assert c.get("/api/stats").status_code == 401
        assert c.get("/api/stats?token=что-то").status_code == 401
        assert c.get("/api/stats?token=кириллический-токен").status_code == 200
