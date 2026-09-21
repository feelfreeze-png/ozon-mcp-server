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
