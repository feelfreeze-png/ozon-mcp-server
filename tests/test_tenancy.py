"""Привязка клиента к магазину токеном.

Главный тест здесь — `test_foreign_shop_id_is_ignored_not_rejected`: он проверяет
не то, что чужой `shop_id` отвергается, а то, что он **ни на что не влияет**.
Разница существенная. Отказ — это правило, которое кто-то должен не забыть
применить в каждой из 150 веток диспетчера; подстановка работает одна на всех,
и забыть её негде.
"""

import pytest

from ozon_mcp import server, tenancy
from ozon_mcp.server import TOOLS, _call_tool_impl, _visible_tools
from ozon_mcp.settings import save_shops


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Каждый тест стартует с выключенным режимом и полным каталогом."""
    monkeypatch.delenv("MCP_CLIENT_TOKENS", raising=False)
    monkeypatch.delenv("OZON_TOOLSETS", raising=False)
    yield


@pytest.fixture
def pinned_to_alfa():
    """Сессия привязана к «alfa». Магазина с таким id в хранилище нет намеренно."""
    token = tenancy.pin("alfa")
    yield "alfa"
    tenancy.unpin(token)


# ─── Разбор переменной ──────────────────────────────────────

def test_disabled_while_variable_is_empty(monkeypatch):
    assert tenancy.client_tokens() == {}
    assert tenancy.is_enabled() is False
    assert tenancy.pinned() is None


def test_parses_pairs(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1, bbb:shop2 ;ccc:shop3")
    assert tenancy.client_tokens() == {"aaa": "shop1", "bbb": "shop2", "ccc": "shop3"}


def test_garbage_entries_are_dropped_not_guessed(monkeypatch):
    """Строка без двоеточия — не «токен без магазина», а мусор: пропускаем."""
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,broken,:no-token,ddd:")
    assert tenancy.client_tokens() == {"aaa": "shop1"}


def test_resolve(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,bbb:shop2")
    assert tenancy.resolve("aaa") == "shop1"
    assert tenancy.resolve("bbb") == "shop2"
    assert tenancy.resolve("ccc") is None
    assert tenancy.resolve("") is None


# ─── Подстановка ────────────────────────────────────────────

def test_enforce_is_a_noop_without_pinning():
    args = {"shop_id": "beta", "campaign_id": 7}
    assert tenancy.enforce(args) is args


def test_enforce_replaces_foreign_shop(pinned_to_alfa):
    args = {"shop_id": "beta", "campaign_id": 7}
    result = tenancy.enforce(args)
    assert result["shop_id"] == "alfa"
    assert result["campaign_id"] == 7
    assert args["shop_id"] == "beta", "исходный словарь мутировать нельзя"


def test_enforce_fills_in_missing_shop(pinned_to_alfa):
    assert tenancy.enforce({})["shop_id"] == "alfa"


# ─── Поведение сервера ──────────────────────────────────────

def test_pinned_session_hides_shop_id_even_with_many_shops(monkeypatch, tmp_path, pinned_to_alfa):
    """Без привязки два магазина возвращают параметр в схемы; с привязкой — нет."""
    save_shops(tmp_path, {
        "alfa": {"name": "Альфа", "ozon_client_id": "1", "ozon_api_key": "a"},
        "beta": {"name": "Бета", "ozon_client_id": "2", "ozon_api_key": "b"},
    })
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    with_pin = {t.name for t in _visible_tools()
                if "shop_id" in (t.inputSchema.get("properties") or {})}
    assert with_pin == set(), "в привязанной сессии shop_id не должен быть виден"

    tenancy.unpin(tenancy.pin(None))  # проверяем обратное — без привязки
    token = tenancy.pin(None)
    try:
        without_pin = [t for t in _visible_tools()
                       if "shop_id" in (t.inputSchema.get("properties") or {})]
    finally:
        tenancy.unpin(token)
    assert without_pin, "без привязки при двух магазинах параметр обязан вернуться"


@pytest.mark.asyncio
async def test_list_shops_hides_neighbours(monkeypatch, tmp_path):
    save_shops(tmp_path, {
        "alfa": {"name": "Альфа", "ozon_client_id": "1", "ozon_api_key": "a"},
        "beta": {"name": "Бета", "ozon_client_id": "2", "ozon_api_key": "b"},
    })
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    token = tenancy.pin("alfa")
    try:
        blocks = await _call_tool_impl("ozon_list_shops", {})
    finally:
        tenancy.unpin(token)
    text = blocks[0].text
    assert "alfa" in text
    assert "beta" not in text, "чужой shop_id не должен попадать клиенту"


@pytest.mark.asyncio
async def test_foreign_shop_id_is_ignored_not_rejected(monkeypatch, tmp_path, pinned_to_alfa):
    """Клиент называет чужой магазин — сервер работает со своим.

    В хранилище есть только «beta». Сессия привязана к «alfa», которого нет.
    Если подстановка работает, поиск ключей уйдёт за «alfa» и упрётся в его
    отсутствие. Если не работает — сервер возьмёт ключи «beta», то есть чужие.
    """
    save_shops(tmp_path, {"beta": {"name": "Бета", "ozon_perf_client_id": "2",
                                   "ozon_perf_client_secret": "b"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    with pytest.raises(ValueError, match="Магазин 'alfa' не найден"):
        await _call_tool_impl("ozon_ad_campaigns", {"shop_id": "beta"})


@pytest.mark.asyncio
async def test_stats_are_attributed_to_the_pinned_shop(monkeypatch, tmp_path, pinned_to_alfa):
    """Расход должен записываться на владельца сессии, а не на названный магазин."""
    save_shops(tmp_path, {"beta": {"name": "Бета"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    seen: list[str] = []

    async def _record(name, duration_ms, success, error_text, shop_id):
        seen.append(shop_id)

    server.set_stats_callback(_record)
    try:
        await server.call_tool("ozon_ad_campaigns", {"shop_id": "beta"})
    finally:
        server.set_stats_callback(None)
    assert seen == ["alfa"]


# ─── Авторизация ────────────────────────────────────────────

def _request(token: str):
    """Минимальный Request с заголовком Authorization."""
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/sse", "query_string": b"",
             "headers": [(b"authorization", f"Bearer {token}".encode())]}
    return Request(scope)


def test_client_token_resolves_to_its_shop(monkeypatch):
    from ozon_mcp import app as app_mod
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,bbb:shop2")
    assert app_mod._resolve_mcp_auth(_request("aaa")) == (True, "shop1")
    assert app_mod._resolve_mcp_auth(_request("bbb")) == (True, "shop2")
    assert app_mod._resolve_mcp_auth(_request("zzz")) == (False, None)


def test_shared_token_stops_working_once_client_tokens_exist(monkeypatch):
    """Общий токен не привязан к магазину — оставить его значит оставить обход."""
    from ozon_mcp import app as app_mod
    monkeypatch.setattr(app_mod, "MCP_AUTH_TOKEN", "shared")
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1")
    assert app_mod._resolve_mcp_auth(_request("shared")) == (False, None)


def test_original_behaviour_is_untouched_while_disabled(monkeypatch):
    from ozon_mcp import app as app_mod
    monkeypatch.setattr(app_mod, "MCP_AUTH_TOKEN", "shared")
    assert app_mod._resolve_mcp_auth(_request("shared")) == (True, None)
    assert app_mod._resolve_mcp_auth(_request("nope")) == (False, None)
