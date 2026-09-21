"""F1 и F2: тип подписки кабинета и журнал действий.

**F1.** `POST /v1/seller/info` — единственный источник ответа «что нам вообще доступно».
Замерено 21.09.2026: отвечает блоками `company`, `subscription` и `ratings`. Подписка
кабинета — `PREMIUM_PLUS`, а поисковая аналитика требует Premium Plus или Pro, то есть
ограничение, записанное в плане как открытое, снято замером.

🔴 В ответе персональные данные: `legal_name` — ФИО предпринимателя, плюс ИНН. По
умолчанию они скрыты: ответ уходит в MCP-клиент, оттуда в контекст модели и в её
транскрипт.

**F2.** Журнал действий. Заводится сейчас, хотя запись в Ozon начнётся на этапе 2:
прежние значения ставок задним числом восстановить неоткуда, и журнал, созданный вместе
с первой правкой, не помнит, что было до неё.

Пишутся и **несостоявшиеся** действия. К воротам этапа 2 («владелец согласен с
рекомендациями агента») именно эта запись отвечает на вопрос, что агент делал бы, будь
ему позволено.
"""

import json

import pytest
import pytest_asyncio

from ozon_mcp import failures, readonly, series, timezones as tz
from ozon_mcp.client import OzonSellerClient

LIVE_SHAPE = {
    "company": {"name": "StockPot", "ownership_form": "ИП",
                "legal_name": "Петров Иван Александрович", "inn": "782615286904",
                "ogrn": "", "tax_system": "USN", "currency": "RUB", "country": "RUS"},
    "subscription": {"is_premium": True, "type": "PREMIUM_PLUS"},
    "ratings": [{"name": "Оценка товаров", "current_value": {"value": 4.83}}],
}


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


# ── F1: подписка ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seller_info_asks_the_measured_path():
    client = OzonSellerClient.__new__(OzonSellerClient)
    seen = {}

    async def capture(path, body=None, **kwargs):
        seen["path"] = path
        return LIVE_SHAPE

    client._post = capture
    await client.seller_info()
    assert seen["path"] == "/v1/seller/info"


def test_personal_data_is_masked_by_name_not_by_shape():
    """🔴 Угадывать ИНН по форме — значит однажды не угадать."""
    masked = failures.mask_seller_info(LIVE_SHAPE)
    assert masked["company"]["legal_name"] == "СКРЫТО"
    assert masked["company"]["inn"] == "СКРЫТО"
    assert masked["company"]["name"] == "StockPot", "название компании — не ПД"
    assert masked["company"]["tax_system"] == "USN"


def test_masking_replaces_rather_than_removes():
    """Исчезнувшее поле неотличимо от поля, которого Ozon не прислал."""
    masked = failures.mask_seller_info(LIVE_SHAPE)
    assert set(masked["company"]) == set(LIVE_SHAPE["company"])


def test_an_empty_personal_field_stays_empty_not_hidden():
    """`ogrn` пуст у ИП. Писать «СКРЫТО» там, где ничего нет, — вводить в заблуждение."""
    masked = failures.mask_seller_info(LIVE_SHAPE)
    assert masked["company"]["ogrn"] == ""


def test_masking_does_not_touch_the_subscription():
    """Ради этого блока метод и вызывается — скрывать его нечего и незачем."""
    masked = failures.mask_seller_info(LIVE_SHAPE)
    assert masked["subscription"] == {"is_premium": True, "type": "PREMIUM_PLUS"}


def test_masking_survives_an_unexpected_shape():
    assert failures.mask_seller_info({"subscription": {}}) == {"subscription": {}}
    assert failures.mask_seller_info({"company": "не объект"}) == {"company": "не объект"}


# ── F2: журнал действий ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_action_is_recorded_with_both_values(db):
    """Прежнее значение — то, ради чего журнал заводится заранее."""
    await series.record_action(
        db, at=tz.now_msk_iso(), shop_id="main", object_kind="campaign",
        object_id="42708950", field="daily_budget", value_before="1000",
        value_after="1500", actor="assistant", result=series.ACTION_OK)

    async with db.execute(
        "SELECT object_kind, object_id, field, value_before, value_after, actor, result "
        "FROM action_log") as cur:
        assert tuple(await cur.fetchone()) == (
            "campaign", "42708950", "daily_budget", "1000", "1500", "assistant", "ok")


@pytest.mark.asyncio
async def test_a_blocked_attempt_is_recorded_too(db):
    """🔴 Несостоявшееся действие — тоже данные к воротам этапа 2.

    Без этой записи на вопрос «что агент делал бы, будь ему позволено» пришлось бы
    отвечать по памяти.
    """
    await series.record_action(
        db, at=tz.now_msk_iso(), shop_id="main", object_kind="product",
        object_id="1418194093", field="ozon_product_delete",
        value_after='{"product_id": [1418194093]}', actor="assistant",
        result=series.ACTION_BLOCKED_READONLY)

    async with db.execute("SELECT result, field FROM action_log") as cur:
        assert tuple(await cur.fetchone()) == ("blocked_readonly", "ozon_product_delete")


@pytest.mark.asyncio
async def test_records_accumulate_in_order(db):
    for field in ("bid", "budget", "state"):
        await series.record_action(
            db, at=tz.now_msk_iso(), shop_id="main", object_kind="campaign",
            object_id="1", field=field, actor="assistant", result=series.ACTION_OK)
    async with db.execute("SELECT field FROM action_log ORDER BY id") as cur:
        assert [row[0] for row in await cur.fetchall()] == ["bid", "budget", "state"]


@pytest.mark.asyncio
async def test_a_naive_timestamp_is_refused(db):
    """Тот же контракт, что у всего ряда: метка без пояса не пишется."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await series.record_action(
            db, at="2026-09-21 12:00:00", shop_id="main", object_kind="campaign",
            object_id="1", field="bid", actor="assistant", result=series.ACTION_OK)


# ── Что именно попадёт в журнал при отказе ───────────────────────────────────


@pytest.mark.parametrize("tool,kind", [
    ("ozon_product_delete", "product"),
    ("ozon_ad_campaign_bids", "campaign"),
    ("ozon_set_prices", "price"),
    ("ozon_chat_send", "chat"),
    ("ozon_returns_fbs_approve", "return"),
    ("ozon_seller_action_toggle", "seller_action"),
])
def test_object_kind_is_derived_from_the_tool(tool, kind):
    assert readonly.object_kind(tool) == kind


def test_an_unknown_tool_is_not_guessed():
    assert readonly.object_kind("ozon_нечто_совсем_новое") == "tool"


@pytest.mark.parametrize("arguments,expected", [
    ({"campaign_id": 42708950}, "42708950"),
    ({"product_id": [1, 2]}, "1, 2"),
    ({"skus": [1, 2, 3, 4, 5, 6, 7]}, "1, 2, 3, 4, 5…"),
    ({"posting_number": "0001-1"}, "0001-1"),
    ({}, ""),
])
def test_object_id_is_taken_from_the_arguments(arguments, expected):
    assert readonly.object_id(arguments) == expected


def test_an_unrecognised_object_is_empty_not_invented():
    """Пустая строка честнее выдуманного идентификатора."""
    assert readonly.object_id({"что_то": "значение"}) == ""


def test_the_attempted_change_keeps_what_the_model_asked_for():
    got = json.loads(readonly.attempted_change(
        {"campaign_id": 1, "bid": 2500, "shop_id": "main", "view": "compact"}))
    assert got == {"campaign_id": 1, "bid": 2500}
    assert "shop_id" not in got, "магазин подставляем мы, а не модель — в журнал не идёт"


def test_a_huge_argument_set_is_capped():
    """Журнал не должен раздуваться одной попыткой на тысячу товаров."""
    got = readonly.attempted_change({"skus": list(range(5000))})
    assert len(got) <= 1000


@pytest.mark.asyncio
async def test_a_blocked_call_actually_reaches_the_journal(tmp_path, monkeypatch):
    """🔴 Сторож против возврата проглатывания.

    ⚠️ Первая редакция обернула запись в `contextlib.suppress(Exception)` — и оно
    проглотило `NameError`: `shop_id` был локальной переменной другой функции. Журнал
    молча не писался, а приёмка показывала пустую таблицу без единой причины. Тот самый
    класс, против которого построен весь проект, в моём же коде.

    Поэтому проверяется не вызов `record_action`, а СОДЕРЖИМОЕ таблицы после
    настоящего отказа.
    """
    from ozon_mcp import server

    monkeypatch.setenv("OZON_READONLY", "1")
    await series.init_db(tmp_path)
    try:
        blocks = await server._call_tool_impl(
            "ozon_product_delete", {"shop_id": "main", "product_id": [1418194093]})
        assert "только для чтения" in blocks[0].text

        conn = series.connection()
        async with conn.execute(
            "SELECT shop_id, object_kind, object_id, field, value_after, result, at "
            "FROM action_log") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
    finally:
        await series.close_db()

    assert len(rows) == 1, "попытка не дошла до журнала"
    shop, kind, obj, field, attempt, result, at = rows[0]
    assert (shop, kind, obj, field, result) == (
        "main", "product", "1418194093", "ozon_product_delete", "blocked_readonly")
    assert json.loads(attempt) == {"product_id": [1418194093]}
    assert at.endswith("+03:00"), "метка времени без московского пояса"


@pytest.mark.asyncio
async def test_a_read_tool_leaves_no_trace(tmp_path, monkeypatch):
    """Журнал ДЕЙСТВИЙ, а не вызовов: чтение в него попадать не должно."""
    from ozon_mcp import server

    monkeypatch.setenv("OZON_READONLY", "1")
    await series.init_db(tmp_path)
    try:
        with pytest.raises(Exception):
            await server._call_tool_impl("ozon_ad_campaigns", {"shop_id": "нет такого"})
        conn = series.connection()
        async with conn.execute("SELECT count(*) FROM action_log") as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await series.close_db()


@pytest.mark.asyncio
async def test_a_journal_failure_is_loud_not_silent(tmp_path, monkeypatch, capsys):
    """Отказ журнала не отменяет отказ инструмента, но обязан быть слышен."""
    from ozon_mcp import server

    monkeypatch.setenv("OZON_READONLY", "1")
    await series.init_db(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("таблица недоступна")

    monkeypatch.setattr(series, "record_action", boom)
    try:
        blocks = await server._call_tool_impl(
            "ozon_product_delete", {"shop_id": "main", "product_id": [1]})
    finally:
        await series.close_db()

    assert "только для чтения" in blocks[0].text, "отказ инструмента пропал"
    assert "ЖУРНАЛ ДЕЙСТВИЙ НЕ ЗАПИСАН" in capsys.readouterr().out
