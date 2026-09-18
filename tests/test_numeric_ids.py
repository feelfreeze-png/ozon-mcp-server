"""Идентификаторы: Ozon отдаёт строками, принимает числами.

`ozon_ad_campaigns` возвращает `{"id":"42297392"}` — строкой. `ozon_ad_statistics_daily`
объявлял `campaign_id` как integer, и модель, честно скопировавшая id из предыдущего
ответа, получала `Input validation error: '42297392' is not of type 'integer'` на
первом же шаге цикла «посмотрел статистику → поправил ставку». Выбора у неё нет: в
ответе тип один, в запросе другой.

Поэтому схемы принимают оба типа, а приведение делает сервер. Проверено живьём
2026-09-18 на кампании 42297392.
"""

import pytest

from ozon_mcp.server import NUMERIC_ID, TOOLS, _coerce_numeric_ids


def test_digit_string_becomes_int():
    assert _coerce_numeric_ids({"campaign_id": "42297392"})["campaign_id"] == 42297392


def test_list_is_coerced_elementwise():
    out = _coerce_numeric_ids({"campaigns": ["1", 2, "3"]})
    assert out["campaigns"] == [1, 2, 3]
    assert all(isinstance(x, int) for x in out["campaigns"])


def test_non_digit_strings_are_left_to_ozon():
    """Тихо превратить мусор в ноль хуже, чем отдать отказ с формулировкой Ozon."""
    out = _coerce_numeric_ids({"campaign_id": "abc", "skus": ["12x", "34"]})
    assert out["campaign_id"] == "abc"
    assert out["skus"] == ["12x", 34]


def test_unrelated_fields_are_untouched():
    """Список полей закрытый: строка из цифр бывает значением, а не идентификатором."""
    args = {"date_from": "2026-09-01", "offer_id": "107", "limit": "50"}
    assert _coerce_numeric_ids(args) is args


def test_source_dict_is_not_mutated():
    args = {"campaign_id": "42297392"}
    _coerce_numeric_ids(args)
    assert args["campaign_id"] == "42297392"


@pytest.mark.parametrize("tool_name,field", [
    ("ozon_ad_statistics_daily", "campaigns"),
    ("ozon_ad_campaign_bids", "campaign_id"),
    ("ozon_ad_campaign_budget_update", "campaign_id"),
])
def test_schemas_accept_what_ozon_returns(tool_name, field):
    """Схема обязана принимать строку: именно в таком виде id приходит из Ozon."""
    tool = next(t for t in TOOLS if t.name == tool_name)
    prop = tool.inputSchema["properties"][field]
    types = prop.get("type") if field != "campaigns" else prop["items"]["type"]
    assert "string" in types and "integer" in types, (tool_name, field, prop)


def test_no_campaign_id_is_integer_only():
    """Сторож на будущее: новый инструмент не должен вернуть прежние грабли."""
    strict = []
    for t in TOOLS:
        for key, prop in (t.inputSchema.get("properties") or {}).items():
            if key not in ("campaign_id", "campaign_ids", "campaigns"):
                continue
            types = prop["items"]["type"] if prop.get("type") == "array" else prop.get("type")
            if types == "integer":
                strict.append((t.name, key))
    assert not strict, strict


def test_numeric_id_shape():
    assert set(NUMERIC_ID["type"]) == {"integer", "string"}
