"""Сквозная проверка привязки: живой сервер, настоящий MCP-клиент, два арендатора.

Юнит-тесты в test_tenancy.py проверяют логику подстановки, но не главное допущение
этой схемы: что привязка, поставленная в обработчике GET /sse, доживает до вызова
инструмента, который приходит отдельным POST /messages. Между ними — цикл mcp_app.run
и задачи anyio; наследование контекста здесь свойство рантайма, а не нашего кода,
и проверяться должно запуском, а не рассуждением.

Поэтому тест поднимает два сервера-клиента к одному процессу и смотрит, что каждый
видит только своё.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from mcp import ClientSession
from mcp.client.sse import sse_client

from ozon_mcp.settings import save_shops

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_ALFA = "token-alfa-placeholder"
TOKEN_BETA = "token-beta-placeholder"
#: Владелец двух кабинетов. Именно этот шов — «запись в переменной → токен →
#: привязка → список магазинов» — и дал дефект 23.09; юнит-тесты его не видят,
#: потому что ставят привязку сами вызовом `tenancy.pin(...)`.
TOKEN_OWNER = "token-owner-placeholder"


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Один процесс, два магазина, два личных токена.

    Ключей Performance ни у кого нет намеренно: инструмент обязан упереться
    в их отсутствие и назвать магазин, за который он полез. Это и есть замер —
    чужой сети при этом не касаемся.
    """
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("data")
    save_shops(data_dir, {
        "alfa": {"name": "Альфа", "ozon_client_id": "1", "ozon_api_key": "a"},
        "beta": {"name": "Бета", "ozon_client_id": "2", "ozon_api_key": "b"},
    })
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "HEALTH_CHECK_INTERVAL_MIN": "0",
        "MCP_AUTH_TOKEN": "",
        "MCP_CLIENT_TOKENS": (f"{TOKEN_ALFA}:alfa,{TOKEN_BETA}:beta,"
                              f"{TOKEN_OWNER}:alfa|beta"),
        "OZON_TOOLSETS": "",
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ozon_mcp.app:fastapi_app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail(f"Сервер не запустился:\n{proc.stdout.read()}")
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            pytest.fail("Сервер не поднялся за 20 секунд")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _probe(base: str, token: str, tool: str, arguments: dict):
    async with sse_client(f"{base}/sse", headers={"Authorization": f"Bearer {token}"},
                          timeout=15) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(tool, arguments)
            return tools.tools, result


def _run(base: str, token: str, tool: str = "ozon_list_shops", arguments: dict | None = None):
    return asyncio.run(asyncio.wait_for(_probe(base, token, tool, arguments or {}), 60))


def test_each_tenant_sees_only_its_own_shop(server):
    """Два клиента, один процесс — каждый видит свой магазин и только его."""
    tools_a, result_a = _run(server, TOKEN_ALFA)
    text_a = result_a.content[0].text
    assert "alfa" in text_a and "beta" not in text_a, text_a

    tools_b, result_b = _run(server, TOKEN_BETA)
    text_b = result_b.content[0].text
    assert "beta" in text_b and "alfa" not in text_b, text_b


def test_shop_id_is_absent_from_every_schema(server):
    """Выбора магазина у модели нет — параметра нет ни в одной схеме."""
    tools, _ = _run(server, TOKEN_ALFA)
    leaking = [t.name for t in tools if "shop_id" in (t.inputSchema.get("properties") or {})]
    assert not leaking, leaking


def test_foreign_shop_id_over_the_wire_changes_nothing(server):
    """Клиент «Альфы» прямым текстом просит магазин «Бета» — и получает свой.

    Это тот самый сценарий, ради которого патч и делался: аргумент приходит
    от модели, и отличить «ошиблась» от «пробует чужое» на стороне сервера нельзя.
    """
    _, result = _run(server, TOKEN_ALFA, "ozon_ad_campaigns", {"shop_id": "beta"})
    text = result.content[0].text
    assert "alfa" in text, text
    assert "beta" not in text, f"сервер полез в чужой магазин: {text}"


def test_owner_of_two_cabinets_sees_both_over_the_wire(server):
    """🔴 Воспроизведение дефекта 23.09 на живом сервере, от переменной до ответа.

    Юнит-тесты ставят привязку сами (`tenancy.pin([...])`) и поэтому шов
    «строка в MCP_CLIENT_TOKENS → разбор → GET /sse → contextvar → вызов»
    не проверяют вовсе. Дефект жил именно там: разбор отдавал ОДИН магазин,
    всё остальное работало безупречно, и бриф честно отчитался по одному
    кабинету из четырёх, написав, что покрытие полное.

    Мутация `pin(shops[:1])` в app.py роняет этот тест и не роняет ни одного
    юнит-теста — ради этого он и написан.
    """
    tools, result = _run(server, TOKEN_OWNER)
    text = result.content[0].text
    assert "alfa" in text and "beta" in text, f"владелец обязан видеть оба: {text}"

    # Вторая половина: видеть два магазина и не иметь чем их выбрать — то же
    # самое, что видеть один. Параметр обязан присутствовать в схемах.
    selectable = [t.name for t in tools if "shop_id" in (t.inputSchema.get("properties") or {})]
    assert selectable, "при двух магазинах shop_id обязан остаться в схемах"


def test_owner_is_refused_a_shop_outside_his_token(server):
    """Свои — оба, чужой — отказ, а не молчаливая подмена своим."""
    _, result = _run(server, TOKEN_OWNER, "ozon_ad_campaigns", {"shop_id": "gamma"})
    text = " ".join(c.text for c in result.content)
    assert "forbidden" in text, text
    assert "gamma" in text


def test_single_shop_tenant_is_unaffected_by_the_multishop_change(server):
    """Сторож регрессии: у односкладочного арендатора всё как было.

    На проде сейчас живут только такие токены. Если правка многомагазинного
    режима заденет их, каждый существующий клиент начнёт получать отказы там,
    где раньше молча работал.
    """
    tools, result = _run(server, TOKEN_ALFA)
    assert "beta" not in result.content[0].text
    leaking = [t.name for t in tools if "shop_id" in (t.inputSchema.get("properties") or {})]
    assert not leaking, "у одного магазина параметр по-прежнему скрыт"


def test_unknown_token_is_rejected(server):
    assert httpx.get(f"{server}/sse", timeout=5,
                     headers={"Authorization": "Bearer nope"}).status_code == 401
    assert httpx.get(f"{server}/sse", timeout=5).status_code == 401
