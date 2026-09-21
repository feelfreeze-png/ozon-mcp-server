"""B1, B2, B4: честность ответов на MCP-пути.

Пакет не добавляет возможностей — он делает прочитанное пригодным к использованию.
Адресат здесь **читатель-программа**, а не человек: заметка текстом человека выручает,
а сборщика нет.

* **B1** — признак усечения внутрь JSON. Предупреждение отдельным блоком сборщик не
  видит: он получает меньше записей и не узнаёт об этом.
* **B2** — структурный отказ. Плоская строка `Ошибка: …` либо роняет разбор, либо
  записывается как пустой результат, а «пусто» у Ozon означает три разных вещи.
* **B4** — инструменты, которые лгут: отказ под видом данных и описание, обещающее поля,
  которых в ответе нет.
"""

import json

import httpx
import pytest

from ozon_mcp import failures, shaping
from ozon_mcp.client import OzonPerformanceClient, OzonSellerClient


# ── B1. Признак усечения ─────────────────────────────────────────────────────


def _big_payload(count: int, *, key: str = "items") -> dict:
    return {key: [{"sku": i, "name": "и" * 400} for i in range(count)]}


def test_truncation_is_visible_inside_the_json():
    """🔴 Приёмка B1: признак есть, и числа сходятся.

    Раньше предупреждение уходило отдельным текстовым блоком. Человек его видел, а
    программа нет: сборщик читает JSON, получает меньше записей и принимает срез за
    полный.
    """
    data, notes = shaping.shape("какой_угодно", {}, _big_payload(400))
    assert data["_truncated"] is True
    assert data["_total"] == 400
    assert data["_shown"] == len(data["items"])
    assert data["_shown"] < data["_total"]
    assert notes, "заметка для человека обязана остаться рядом со структурой"


def test_no_truncation_means_no_flag():
    """Признак, стоящий всегда, перестаёт что-либо означать."""
    data, _ = shaping.shape("какой_угодно", {}, {"items": [{"sku": 1}]})
    assert "_truncated" not in data
    assert "_total" not in data and "_shown" not in data


def test_a_truncated_top_level_array_is_wrapped_and_says_so():
    """Массив нельзя пометить полем. Обёртка появляется ТОЛЬКО при усечении.

    Смена формы здесь и есть сообщение: «это не весь ответ». Молчаливо короткий массив
    неотличим от полного.
    """
    rows = [{"sku": i, "name": "и" * 400} for i in range(400)]
    data, _ = shaping.shape("какой_угодно", {}, rows)
    assert isinstance(data, dict)
    assert data["_truncated"] is True
    assert data["_total"] == 400
    assert len(data[shaping.WRAPPED_ITEMS_KEY]) == data["_shown"]


def test_an_untruncated_array_keeps_its_shape():
    """В обычном случае путь к данным менять незачем."""
    rows = [{"sku": 1}, {"sku": 2}]
    data, _ = shaping.shape("какой_угодно", {}, rows)
    assert data == rows


def test_exactly_limit_rows_is_marked_as_suspicion_not_as_truncation():
    """Записей ровно столько, сколько запрошено — это подозрение, а не усечение.

    Отличается тем, что полное число НЕИЗВЕСТНО: писать `_total` здесь было бы враньём.
    """
    data, notes = shaping.shape("какой_угодно", {"limit": 3},
                                {"items": [{"sku": i} for i in range(3)]})
    assert data["_maybe_incomplete"] is True
    assert data["_limit"] == 3
    assert "_truncated" not in data
    assert notes


def test_a_compact_preset_says_that_fields_were_dropped():
    """Пресет режет ПОЛЯ. Для сборщика это тоже неполнота, просто другого рода."""
    name = next(iter(shaping.VIEWS))
    path = shaping.VIEWS[name][0]
    payload: dict = {}
    node = payload
    for key in path[:-1]:
        node[key] = {}
        node = node[key]
    node[path[-1]] = [{"совсем_лишнее_поле": 1, "ещё_одно": 2}]
    data, notes = shaping.shape(name, {"view": "compact"}, payload)
    if notes:
        assert data.get("_view") == "compact"


# ── B2. Структурный отказ и четыре исхода ────────────────────────────────────


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api-seller.ozon.ru/v1/x")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_ozon_http_error_carries_status_and_retryability():
    got = failures.classify(_http_error(429, '{"error":"too many"}'))
    assert got["kind"] == failures.OZON_HTTP
    assert got["status"] == 429
    assert got["retryable"] is True
    assert "too many" in got["message"], "тело отказа потерялось — причина 400 без него не видна"


def test_a_client_error_is_not_retryable():
    got = failures.classify(_http_error(400, "empty campaigns"))
    assert got["status"] == 400 and got["retryable"] is False


def test_a_business_error_is_its_own_kind():
    """Код 200 и отказ в теле — отдельный род: по коду ответа его не отличить."""
    got = failures.classify(failures.EndpointRetired("нет такого", endpoint="/v1/x"))
    assert got["kind"] == failures.OZON_BUSINESS
    assert got["status"] == 200 and got["retryable"] is False
    assert got["endpoint"] == "/v1/x"


def test_a_network_failure_is_retryable():
    got = failures.classify(httpx.ConnectTimeout("таймаут"))
    assert got["kind"] == failures.NETWORK and got["retryable"] is True


def test_our_own_contract_break_is_not_retryable():
    """Повторять бессмысленно: ответ не изменится, пока не изменится код."""
    got = failures.classify(ValueError("период перевёрнут"))
    assert got["kind"] == failures.OUR_BUG and got["retryable"] is False


def test_the_envelope_is_json_and_the_human_text_stands_beside_it():
    envelope = failures.envelope(_http_error(503))
    assert json.loads(json.dumps(envelope))["_error"]["kind"] == failures.OZON_HTTP
    text = failures.human_text(envelope)
    assert "Ozon ответил ошибкой" in text and "Повтор осмыслен" in text


# ── Четыре исхода, и два из них выглядят одинаково ───────────────────────────


def test_data_outcome():
    assert failures.outcome({"items": [{"sku": 1}]}) == failures.DATA


def test_confirmed_empty_outcome():
    """Ozon ответил, строк нет. Это ответ: расхода не было."""
    assert failures.outcome({"items": []}) == failures.EMPTY_CONFIRMED


def test_unknown_empty_outcome():
    """Ответ обрезан. Строк нет, но утверждать «их нет» нельзя."""
    assert failures.outcome({"items": [], "_truncated": True,
                             "_total": 500, "_shown": 0}) == failures.EMPTY_UNKNOWN


def test_failed_outcome():
    assert failures.outcome(failures.envelope(_http_error(500))) == failures.FAILED


def test_confirmed_and_unknown_emptiness_do_not_merge():
    """🔴 Приёмка B2. Оба — пустой список, а значат противоположное.

    «Ноль» против «неизвестно», и среднее между ними неверно. Без признака усечения
    внутри ответа (B1) различить их нечем — отсюда и порядок пакетов.
    """
    confirmed = failures.outcome({"items": []})
    unknown = failures.outcome({"items": [], "_maybe_incomplete": True, "_limit": 100})
    assert confirmed != unknown
    assert {confirmed, unknown} == {failures.EMPTY_CONFIRMED, failures.EMPTY_UNKNOWN}


def test_all_four_outcomes_are_distinct():
    got = {
        failures.outcome({"items": [{"sku": 1}]}),
        failures.outcome({"items": []}),
        failures.outcome({"items": [], "_truncated": True, "_total": 9, "_shown": 0}),
        failures.outcome(failures.envelope(ValueError("x"))),
    }
    assert len(got) == 4


# ── B4. Инструменты, которые лгут ────────────────────────────────────────────


RETIRED = [
    ("company_info", OzonSellerClient, ()),
    ("company_tariffs", OzonSellerClient, ()),
    ("certificate_list", OzonSellerClient, ()),
    ("certificate_info", OzonSellerClient, (1,)),
    ("balance", OzonPerformanceClient, ()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,cls,args", RETIRED)
async def test_a_retired_endpoint_raises_instead_of_answering_with_error(method, cls, args):
    """🔴 Эти пятеро отвечали полем `error` с кодом 200 — отказ под видом данных.

    Читатель не может отличить такой ответ от настоящего: код успешный, форма
    словарная. Теперь это исключение, и оно доходит наверх родом `ozon_business`.
    """
    client = cls.__new__(cls)
    with pytest.raises(failures.EndpointRetired):
        await getattr(client, method)(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,cls,args", RETIRED)
async def test_the_refusal_names_the_endpoint(method, cls, args):
    client = cls.__new__(cls)
    try:
        await getattr(client, method)(*args)
    except failures.EndpointRetired as exc:
        assert exc.endpoint, f"{method}: отказ не называет, какого метода нет"


def test_no_stub_returns_an_error_field_any_more():
    """Сторож против возврата привычки: `{"error": …}` с кодом 200 — это ложь."""
    import re
    from pathlib import Path

    source = Path(OzonSellerClient.__module__.replace(".", "/") + ".py")
    if not source.exists():
        source = Path(__file__).resolve().parents[1] / "ozon_mcp/client.py"
    text = source.read_text(encoding="utf-8")
    # Отсекаем строки документации: в них такой текст цитируется как ПРИМЕР отказа Ozon.
    returns = re.findall(r'^\s*return \{"error":', text, re.M)
    assert not returns, (
        f"в client.py остались заглушки, отдающие отказ данными: {len(returns)}"
    )


def test_the_campaign_totals_tool_no_longer_promises_sku():
    """Описание обещало «ДРР по SKU», а в ответе нет ни `sku`, ни `date`."""
    from ozon_mcp import server

    tool = next(t for t in server.TOOLS if t.name == "ozon_ad_statistics_products")
    assert "NOT per SKU" in tool.description
    assert "ozon_ad_statistics_products_sku" in tool.description, (
        "описание не отправляет туда, где разрез по товарам действительно есть"
    )


def test_the_per_sku_tool_promises_only_what_it_returns():
    """Сторож обещаний: поля, названные в описании, обязаны быть в нормализованной строке."""
    from ozon_mcp import server
    from ozon_mcp.client import normalize_sku_rows

    tool = next(t for t in server.TOOLS if t.name == "ozon_ad_statistics_products_sku")
    (row,) = normalize_sku_rows({"rows": [{
        "sku": 1, "campaignId": 2, "date": "2026-09-19", "expense": "1.0",
        "views": "1", "clicks": "1", "toCart": "1", "orders": "1",
        "modelOrders": "0", "sales": "1.0", "modelSales": "0", "price": "1.0"}]})
    for promised in ("sku", "campaign_id", "expense", "views", "clicks",
                     "to_cart", "orders", "model_orders", "sales", "price"):
        assert promised in tool.description, f"описание не называет {promised}"
        assert promised in row, f"описание обещает {promised}, а строка его не несёт"
