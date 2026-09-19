"""Эндпоинт срока жизни Seller-ключа.

Ключ живёт три месяца и после этого начинает отказывать молча — ни один внешний
механизм об этом не узнает, пока кто-нибудь не заметит, что данные перестали
приходить. Эндпоинт нужен сторожу снаружи, и его главное свойство — **не сворачивать
«спросить не удалось» в «всё хорошо»**: сеть, протухший ключ и смена схемы ответа
выглядят одинаково молча.

Ключи наружу не отдаются: только дата, остаток в днях и имена ролей.
"""

import datetime

import pytest
from fastapi.testclient import TestClient

from ozon_mcp import app as app_mod
from ozon_mcp.settings import save_shops


def _seed(tmp_path, **extra):
    save_shops(tmp_path, {"main": {"name": "Ozon", "ozon_client_id": "1",
                                   "ozon_api_key": "k", **extra}})


def _stub_roles(monkeypatch, result=None, exc=None):
    class _Stub:
        async def roles(self):
            if exc is not None:
                raise exc
            return result

    monkeypatch.setattr(app_mod, "get_seller_for_shop", lambda _s: _Stub())


def test_ok_reports_days_left(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=42)
    _stub_roles(monkeypatch, {"expires_at": when.isoformat().replace("+00:00", "Z"),
                              "roles": [{"name": "Admin read only"}]})
    body = TestClient(app_mod.fastapi_app).get("/api/key-expiry").json()["shops"][0]
    assert body["state"] == "ok"
    assert 41 <= body["days_left"] <= 42
    assert body["roles"] == ["Admin read only"]


def test_expired_is_not_ok(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    _stub_roles(monkeypatch, {"expires_at": when.isoformat().replace("+00:00", "Z"), "roles": []})
    assert TestClient(app_mod.fastapi_app).get("/api/key-expiry").json()["shops"][0]["state"] == "expired"


@pytest.mark.parametrize("payload,exc,why", [
    ({"roles": []}, None, "нет expires_at"),
    ({"expires_at": "позавчера"}, None, "не разобрана дата"),
    (None, RuntimeError("сеть отвалилась"), "исключение"),
])
def test_unknown_is_its_own_state(tmp_path, monkeypatch, payload, exc, why):
    """Три разных отказа — и ни один не должен выглядеть как «срок в порядке»."""
    _seed(tmp_path)
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    _stub_roles(monkeypatch, payload, exc)
    body = TestClient(app_mod.fastapi_app).get("/api/key-expiry").json()["shops"][0]
    assert body["state"] == "unknown", why
    assert body.get("reason"), "отказ обязан называть причину"
    assert "days_left" not in body


def test_missing_keys_are_unknown_not_ok(tmp_path, monkeypatch):
    save_shops(tmp_path, {"main": {"name": "Ozon"}})
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    body = TestClient(app_mod.fastapi_app).get("/api/key-expiry").json()["shops"][0]
    assert body["state"] == "unknown"


def test_no_secrets_leak(tmp_path, monkeypatch):
    """Смысл эндпоинта — отдать срок, НЕ отдавая ключи."""
    _seed(tmp_path)
    monkeypatch.setattr(app_mod, "DATA_DIR", tmp_path)
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
    _stub_roles(monkeypatch, {"expires_at": when.isoformat().replace("+00:00", "Z"), "roles": []})
    text = TestClient(app_mod.fastapi_app).get("/api/key-expiry").text
    assert "ozon_api_key" not in text
    assert '"k"' not in text
