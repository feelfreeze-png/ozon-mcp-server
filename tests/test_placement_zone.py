"""Зоны размещения товаров: POST /v1/product/placement-zone/info.

Спека снята с `docs.ozon.ru/api/seller/swagger.json` (operationId
`ProductAPI_GetProductPlacementZoneInfo`) 2026-09-18, а не с описания в блогах:
тело `{"skus": [<int>]}`, ответ `{"products_placement": [{"sku", "placement_zone"}]}`.

Отдельного внимания стоит тип SKU. Соседний `/v1/analytics/stocks` ждёт строки, а
этот — числа. Перепутать легко, ошибка возвращается как `400` без указания поля.
"""

import pytest

from ozon_mcp import toolsets
from ozon_mcp.client import OzonSellerClient
from ozon_mcp.server import TOOLS


class _Spy(OzonSellerClient):
    """Клиент, который никуда не ходит и запоминает, что собирался отправить."""

    def __init__(self):
        super().__init__("1", "k")
        self.sent: tuple[str, dict] | None = None

    async def _post(self, path: str, body: dict | None = None) -> dict:
        self.sent = (path, body or {})
        return {"products_placement": [{"sku": 1, "placement_zone": "PRODUCTS"}]}


@pytest.mark.asyncio
async def test_path_and_body_match_the_spec():
    c = _Spy()
    await c.placement_zone_info([913050946, 913079716])
    assert c.sent == ("/v1/product/placement-zone/info",
                      {"skus": [913050946, 913079716]})


@pytest.mark.asyncio
async def test_skus_are_sent_as_integers_not_strings():
    """Схема Ozon объявляет массив integer; строки дают 400 без указания поля."""
    c = _Spy()
    await c.placement_zone_info(["913050946", 913079716])
    assert c.sent is not None
    assert all(isinstance(s, int) for s in c.sent[1]["skus"])


def test_tool_is_registered_and_requires_skus():
    tool = next((t for t in TOOLS if t.name == "ozon_placement_zone"), None)
    assert tool is not None, "инструмент не объявлен в TOOLS"
    assert tool.inputSchema.get("required") == ["skus"]
    assert tool.inputSchema["properties"]["skus"]["items"]["type"] == "integer"


def test_tool_lives_in_analytics_profile():
    """Читается вместе с остатками, значит едет с ними же — не с каталогом.

    Если бы он попал в catalog, включить его можно было бы только вместе с 28
    ручками карточки товара; а провались он в core — ехал бы даже там, где
    Seller API выключен целиком.
    """
    assert toolsets.profile_of("ozon_placement_zone") == "analytics"
