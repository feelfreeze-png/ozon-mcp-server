import pytest
"""Схемы инструментов: то, что уходит клиенту в каждой сессии."""


def test_visible_tools_hides_shop_id_for_single_shop(tmp_path, monkeypatch):
    """Один магазин — shop_id из схем убран, несколько — возвращается."""
    import json
    from ozon_mcp import server

    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    with_shop_id = lambda tools: sum(
        1 for t in tools if "shop_id" in (t.inputSchema.get("properties") or {})
    )

    tools = server._visible_tools()
    assert len(tools) == len(server.TOOLS)
    assert with_shop_id(tools) == 0, "при одном магазине shop_id не нужен в схеме"

    (tmp_path / "shops.json").write_text(json.dumps({"a": {"name": "A"}, "b": {"name": "B"}}))
    tools = server._visible_tools()
    assert with_shop_id(tools) > 0, "при нескольких магазинах shop_id обязан вернуться"


def test_json_output_is_compact():
    """Ответы сериализуются без отступов — indent=2 стоил 39% лишних токенов."""
    from ozon_mcp.server import _json

    text = _json({"a": [1, 2], "b": "тест"})[0].text
    assert "\n" not in text and ", " not in text, text
    assert "\\u" not in text, "кириллица не должна экранироваться"


def test_no_duplicate_tools():
    """Нет дублей в TOOLS."""
    from ozon_mcp.server import TOOLS

    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))


def test_limit_defaults_are_modest():
    """Дефолтный limit не должен выдавать ответ крупнее потолка клиента.

    В Claude Code потолок вывода одного вызова — MAX_MCP_OUTPUT_TOKENS,
    по умолчанию 25 000 токенов; ответ на тысячи строк в него не помещается
    и молча обрезается.
    """
    from ozon_mcp.server import TOOLS

    too_big = [
        (t.name, (t.inputSchema.get("properties") or {})["limit"]["default"])
        for t in TOOLS
        if isinstance((t.inputSchema.get("properties") or {}).get("limit"), dict)
        and (t.inputSchema["properties"]["limit"].get("default") or 0) > 500
    ]
    assert not too_big, f"слишком крупный дефолтный limit: {too_big}"


def test_registry_description_fits_the_limit():
    """MCP Registry отклоняет server.json с description длиннее 100 символов.

    Публикация падала с 422 именно на этом: короткое описание легко перерастает
    лимит, когда в него добавляют цифры.
    """
    import json
    import pathlib

    manifest = json.loads((pathlib.Path(__file__).resolve().parent.parent / "server.json").read_text())
    assert len(manifest["description"]) <= 100, len(manifest["description"])


# ── Вопросы покупателей: статус ≠ наличие ответа ─────────────────────────────


class _Questions:
    """Кабинет с вопросами: страницы плюс собственный счётчик Ozon."""

    def __init__(self, pages, counters):
        self.pages = list(pages)
        self.counters = counters
        self.calls = 0

    async def question_list(self, limit=100, last_id="", sort_dir="DESC"):
        index = 0 if not last_id else int(last_id)
        items, cursor, has_next = self.pages[index]
        self.calls += 1
        return {"questions": items, "last_id": cursor, "has_next": has_next}

    async def question_count(self):
        return dict(self.counters)


def _client_with_questions(seller):
    from ozon_mcp.client import OzonSellerClient

    client = OzonSellerClient.__new__(OzonSellerClient)
    client.question_list = seller.question_list
    client.question_count = seller.question_count
    return client


def _q(qid, answers):
    return {"id": qid, "sku": 1, "answers_count": answers, "status": "VIEWED",
            "published_at": "2026-09-01T00:00:00Z", "text": "?"}


@pytest.mark.asyncio
async def test_unanswered_is_counted_by_answers_not_by_status():
    """🔴 Замер 22.09.2026: счётчик Ozon расходится с фактом в ОБЕ стороны.

    `unprocessed` = 28 при 27 вопросах без ответа, и это разные множества:
    три отвеченных числятся необработанными, два неотвеченных — обработанными.
    Статус — отметка оператора, наличие ответа — факт о карточке.
    """
    seller = _Questions(
        pages=[([_q("a", 0), _q("b", 1), _q("c", 0)], "", False)],
        counters={"all": 3, "unprocessed": 1, "processed": 2})
    got = await _client_with_questions(seller).questions_all(pause_s=0)

    assert got["без ответа"] == 2, "посчитано по статусу, а не по answers_count"
    assert got["счётчик Ozon"]["unprocessed"] == 1
    assert "счётчик и факт расходятся" in got, "расхождение обязано быть названо"


@pytest.mark.asyncio
async def test_agreement_between_counter_and_fact_says_nothing():
    seller = _Questions(
        pages=[([_q("a", 0), _q("b", 1)], "", False)],
        counters={"all": 2, "unprocessed": 1})
    got = await _client_with_questions(seller).questions_all(pause_s=0)
    assert got["без ответа"] == 1
    assert "счётчик и факт расходятся" not in got


@pytest.mark.asyncio
async def test_the_walk_covers_every_page():
    seller = _Questions(
        pages=[([_q(f"a{i}", 0) for i in range(100)], "1", True),
               ([_q(f"b{i}", 1) for i in range(50)], "", False)],
        counters={"all": 150, "unprocessed": 100})
    got = await _client_with_questions(seller).questions_all(pause_s=0)

    assert got["всего собрано"] == 150 and got["страниц"] == 2
    assert got["без ответа"] == 100
    assert "ОБХОД НЕПОЛОН" not in got


@pytest.mark.asyncio
async def test_a_truncated_walk_is_named_not_swallowed():
    """Молчаливый недобор выдал бы очередь меньшей, чем она есть."""
    seller = _Questions(
        pages=[([_q("a", 0)], "", False)],
        counters={"all": 369, "unprocessed": 28})
    got = await _client_with_questions(seller).questions_all(pause_s=0)

    assert "ОБХОД НЕПОЛОН" in got
    assert "369" in got["ОБХОД НЕПОЛОН"]


@pytest.mark.asyncio
async def test_the_page_cap_stops_an_endless_cursor():
    """Курсор, который всегда обещает ещё страницу, не должен крутиться вечно."""
    seller = _Questions(
        pages=[([_q("a", 0)], "0", True)] * 3,
        counters={"all": 1, "unprocessed": 0})
    got = await _client_with_questions(seller).questions_all(max_pages=3, pause_s=0)
    assert got["страниц"] == 3
