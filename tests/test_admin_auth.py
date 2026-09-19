"""Охрана веб-интерфейса.

До `ADMIN_TOKEN` токен проверяли только `/sse` и `/messages`, а весь веб-интерфейс был
открыт: список магазинов, их заведение и удаление, статистика всех арендаторов.
Привязка клиента к магазину это не закрывает и не пытается — она про MCP-сессию, а не
про админку: сосед не стал бы подбирать `shop_id`, он открыл бы `/shops`.

Главное свойство охраны — она устроена **списком исключений**, а не списком защищаемых
маршрутов. Поэтому здесь есть тест, который перебирает ВСЕ объявленные маршруты и
требует, чтобы каждый новый был закрыт по умолчанию. Именно забывчивость и сделала
исходную дыру.
"""

import pytest
from fastapi.testclient import TestClient

from ozon_mcp import app as app_mod

TOKEN = "admin-token-placeholder"

# Два намеренных исключения, каждое со своей причиной.
EXEMPT = {"/sse", "/messages", "/api/health"}


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    return TestClient(app_mod.fastapi_app)


@pytest.fixture
def open_server(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "ADMIN_TOKEN", "")
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    return TestClient(app_mod.fastapi_app)


@pytest.mark.parametrize("path", ["/", "/shops", "/diagnostics", "/api/stats",
                                  "/api/key-expiry"])
def test_admin_surface_is_closed_without_token(guarded, path):
    assert guarded.get(path).status_code == 401, path


@pytest.mark.parametrize("path", ["/", "/shops", "/api/stats", "/api/key-expiry"])
def test_token_opens_it(guarded, path):
    r = guarded.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code != 401, path


def test_query_token_also_works(guarded):
    """Браузер заголовок не пошлёт — для дашборда нужен ?token=."""
    assert guarded.get(f"/shops?token={TOKEN}").status_code != 401


def test_wrong_token_is_rejected(guarded):
    assert guarded.get("/shops", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_writes_are_closed_too(guarded):
    """Чтение закрыть и забыть про запись — худший исход: удаление осталось бы открытым."""
    assert guarded.post("/api/shops", json={}).status_code == 401
    assert guarded.delete("/api/shops/main").status_code == 401
    assert guarded.post("/api/shops/main/test").status_code == 401


def test_liveness_stays_open_but_says_little(guarded):
    """Healthcheck контейнера токена не имеет — живость обязана отвечать всем."""
    r = guarded.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok"}, "без токена подробности отдавать незачем"


def test_liveness_is_full_with_token(guarded):
    body = guarded.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert body["admin_auth_enabled"] is True
    assert "recent_checks" in body


def test_mcp_paths_are_not_double_locked():
    """Админский токен не должен становиться вторым замком на /sse и /messages.

    Иначе MCP-клиенту пришлось бы знать админский секрет — ровно то, от чего уводит
    привязка по личному токену. Проверяется состав исключений, а не ответ: поднимать
    SSE-поток в тесте незачем, а забыть путь в списке — единственный способ сломать это.
    """
    assert app_mod._MCP_PREFIXES == ("/sse", "/messages")
    assert app_mod._LIVENESS_PATH == "/api/health"


def test_without_admin_token_everything_stays_open(open_server):
    """Обратная совместимость: пустой ADMIN_TOKEN — поведение как раньше."""
    assert open_server.get("/api/stats").status_code != 401
    assert open_server.get("/api/health").json().get("admin_auth_enabled") is False


def test_every_route_is_closed_unless_explicitly_exempt(guarded):
    """Сторож на будущее: новый маршрут обязан быть закрыт по умолчанию.

    Исходная дыра появилась не злым умыслом, а тем, что защищать надо было помнить.
    Здесь помнить не надо: маршрут, которого нет в EXEMPT, обязан отвечать 401.
    """
    leaked = []
    checked = 0
    for route in app_mod.fastapi_app.routes:
        path = getattr(route, "path", "")
        if not path or path in EXEMPT or path.startswith(("/sse", "/messages")):
            continue
        if "{" in path:  # параметрические проверяются отдельно выше
            continue
        methods = getattr(route, "methods", set()) or {"GET"}
        method = "GET" if "GET" in methods else next(iter(methods))
        checked += 1
        r = guarded.request(method, path)
        if r.status_code != 401:
            leaked.append(f"{method} {path} -> {r.status_code}")
    # Без этой строки тест прошёл бы, не проверив ни одного маршрута: перебор по пустому
    # списку молча зелёный. Ровно тот класс, против которого весь этот файл и написан.
    assert checked >= 10, f"перебор охватил всего {checked} маршрутов — проверка выродилась"
    assert not leaked, leaked
