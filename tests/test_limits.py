"""C6: чанкинг и проверка чисел справочника.

**Приёмка пакета:** запрос на границе проходит, на границе+1 отвергается **нами**, а не
Ozon. Разница не косметическая: ответ Ozon сверх лимита бывает неотличим от пустого
результата — `products/sku` на неверное имя поля отвечает `empty campaigns`, то есть
утверждает, что кампаний нет, хотя их передали.

**Второе требование:** у каждого числа записано происхождение. Справочник базы знаний уже
один раз разошёлся с замером, на единицах бюджета. Число без пометки — это число, о
котором неизвестно, проверяли его или нет, и такая неизвестность здесь запрещена.
"""

import asyncio

import pytest

from ozon_mcp import limits
from ozon_mcp.client import OzonPerformanceClient


def test_every_limit_declares_its_origin():
    """Число без происхождения — это «где-то написано», выдающее себя за «проверено»."""
    for key, value in limits.LIMITS.items():
        assert value.origin in (limits.MEASURED, limits.HANDBOOK), key
        assert value.what, f"{key}: не сказано, что именно ограничено"
        assert value.evidence, f"{key}: не сказано, чем подтверждено"
        assert value.value > 0, key


def test_unverified_limits_are_listed_not_hidden():
    """Список «на что опираемся, не замерив» — отчёт, а не украшение.

    Он намеренно непустой: девять чисел из одиннадцати взяты из справочника. Скрыть это
    значило бы выдать справочник за замер.
    """
    unverified = limits.unverified()
    assert unverified, "все лимиты вдруг замерены — проверьте, не потерялась ли пометка"
    assert "statistics.campaigns" in unverified
    assert "products_sku.campaigns" not in unverified, "эта порция замерена живым вызовом"
    assert "campaigns.page_size" not in unverified


def test_measured_limits_name_the_measurement():
    for key in ("products_sku.campaigns", "campaigns.page_size"):
        assert "2026" in limits.limit(key).evidence, f"{key}: замер без даты"


def test_unknown_limit_is_a_loud_error():
    """Опечатка в ключе не должна тихо снимать проверку."""
    with pytest.raises(KeyError, match="не заведён"):
        limits.limit("нет.такого")


# ── Граница ──────────────────────────────────────────────────────────────────


def test_boundary_passes_and_boundary_plus_one_is_refused():
    """Ровно та приёмка, что названа в плане."""
    size = limits.limit("products_sku.campaigns").value
    limits.check("products_sku.campaigns", list(range(size)))
    with pytest.raises(limits.LimitExceeded):
        limits.check("products_sku.campaigns", list(range(size + 1)))


def test_refusal_names_the_number_and_where_it_came_from():
    """Отказ без происхождения числа отправляет читателя искать его по коду."""
    with pytest.raises(limits.LimitExceeded) as caught:
        limits.check("statistics.campaigns", list(range(11)))
    message = str(caught.value)
    assert "11 > 10" in message
    assert limits.HANDBOOK in message, "не сказано, что число из справочника"


@pytest.mark.asyncio
async def test_over_limit_request_never_leaves_the_process():
    """🔴 Главное свойство: отвергаем МЫ, до отправки.

    Ответ Ozon сверх лимита бывает неотличим от пустого результата, и тогда день
    «собран» пустым.
    """
    from ozon_mcp import timezones as tz

    client = OzonPerformanceClient.__new__(OzonPerformanceClient)

    async def must_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("запрос сверх лимита ушёл в Ozon")

    client._post = must_not_be_called
    day = tz.yesterday_msk()
    with pytest.raises(limits.LimitExceeded):
        await client.statistics_products_sku(list(range(31)), day, day)


@pytest.mark.asyncio
async def test_async_report_over_ten_campaigns_is_refused():
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)
    client._report_slot = asyncio.Lock()

    async def must_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("выгрузка сверх лимита ушла в Ozon")

    client._post = must_not_be_called
    with pytest.raises(limits.LimitExceeded):
        await client.statistics(list(range(11)), "2026-09-19", "2026-09-19")
    assert not client._report_slot.locked(), "слот занят отказом"


# ── Чанкинг ──────────────────────────────────────────────────────────────────


def test_chunks_split_by_the_limit():
    got = list(limits.chunks("products_sku.campaigns", list(range(248))))
    assert [len(c) for c in got] == [30] * 8 + [8]
    assert sum(len(c) for c in got) == 248
    assert [item for chunk in got for item in chunk] == list(range(248))


def test_empty_input_gives_no_chunks():
    """Ноль порций и одна пустая порция — разные вещи: вторая шлёт пустой запрос."""
    assert list(limits.chunks("products_sku.campaigns", [])) == []


@pytest.mark.asyncio
async def test_full_walk_chunks_and_never_exceeds_the_limit():
    """248 кампаний из живого кабинета проходят порциями, и каждая в пределах лимита."""
    from ozon_mcp import timezones as tz

    client = OzonPerformanceClient.__new__(OzonPerformanceClient)
    sent = []

    async def capture(path, body=None):
        sent.append(body["campaignIds"])
        start = int(body["campaignIds"][0])
        return {"rows": [{"sku": start, "campaignId": start,
                          "date": body["date_from"], "expense": "1.00"}]}

    client._post = capture
    day = tz.yesterday_msk()
    rows = await client.statistics_products_sku_all(list(range(248)), day, pause_s=0)

    size = limits.limit("products_sku.campaigns").value
    assert len(sent) == 9
    assert all(len(batch) <= size for batch in sent), "порция превысила лимит"
    assert sum(len(batch) for batch in sent) == 248
    assert len(rows) == 9 and rows[0]["expense"] == 1.0


@pytest.mark.asyncio
async def test_walk_pauses_between_chunks(monkeypatch):
    from ozon_mcp import timezones as tz

    waits = []

    async def capture_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)

    async def reply(path, body=None):
        return {"rows": []}

    client._post = reply
    await client.statistics_products_sku_all(list(range(90)), tz.yesterday_msk())
    assert waits == [client.PRODUCTS_SKU_CHUNK_PAUSE_S] * 2, waits


@pytest.mark.asyncio
async def test_a_failing_chunk_is_not_swallowed_into_a_short_answer():
    """Отказ порции обязан ронять обход: иначе день соберётся неполным и молча."""
    from ozon_mcp import timezones as tz

    client = OzonPerformanceClient.__new__(OzonPerformanceClient)
    calls = {"n": 0}

    async def flaky(path, body=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("порция не собралась")
        return {"rows": []}

    client._post = flaky
    with pytest.raises(RuntimeError):
        await client.statistics_products_sku_all(list(range(90)), tz.yesterday_msk(), pause_s=0)


# ── Единицы бюджета: замер отложен до этапа 2 ────────────────────────────────


def test_budget_conversion_is_refused_until_measured(monkeypatch):
    """🔴 Решение владельца 21.09.2026 — отложить замер до этапа 2.

    Отложить замер и оставить молчаливое `* 1_000_000` значило бы отложить не вопрос,
    а его последствие: ошибка в единице бюджета в тысячу раз неотличима от верной
    настройки ничем, кроме счёта в конце недели.
    """
    monkeypatch.delenv(limits.BUDGET_UNIT_ENV, raising=False)
    with pytest.raises(limits.UnmeasuredUnitError) as exc:
        limits.budget_to_api(1234, field="weeklyBudget")
    assert "weeklyBudget" in str(exc.value)


def test_the_refusal_names_the_experiment_that_settles_it():
    """Отказ обязан говорить, ЧТО сделать, иначе он читается как «нельзя никогда»."""
    with pytest.raises(limits.UnmeasuredUnitError) as exc:
        limits.budget_to_api(1234, field="dailyBudget")
    text = str(exc.value)
    assert "1234000000" in text and "1234000000000" in text
    assert "прочитать обратно" in text


def test_a_measured_unit_converts(monkeypatch):
    monkeypatch.setenv(limits.BUDGET_UNIT_ENV, "1000000")
    assert limits.budget_to_api(1234, field="weeklyBudget") == "1234000000"
    monkeypatch.setenv(limits.BUDGET_UNIT_ENV, "1000000000")
    assert limits.budget_to_api(1234, field="weeklyBudget") == "1234000000000"


@pytest.mark.parametrize("value", ["микрорубли", "", "  ", "1000", "1e6", "10000000"])
def test_anything_but_the_two_candidates_is_refused(monkeypatch, value):
    """Замер обязан дать один из двух. Другое значение — опечатка ценой в порядок."""
    monkeypatch.setenv(limits.BUDGET_UNIT_ENV, value)
    with pytest.raises(limits.UnmeasuredUnitError):
        limits.budget_to_api(1234, field="weeklyBudget")


@pytest.mark.asyncio
async def test_campaign_create_cannot_slip_a_budget_through(monkeypatch):
    """Сторож против возврата: оба метода обязаны ходить через один отказ."""
    from ozon_mcp.client import OzonPerformanceClient

    monkeypatch.delenv(limits.BUDGET_UNIT_ENV, raising=False)
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("запрос ушёл в Ozon с непроверенной единицей бюджета")

    client._post = must_not_be_called
    with pytest.raises(limits.UnmeasuredUnitError):
        await client.campaign_create("проба", weekly_budget_rub=1234)


@pytest.mark.asyncio
async def test_campaign_update_cannot_slip_a_budget_through(monkeypatch):
    """⚠️ Второе место. Документация утверждала, что оно одно."""
    from ozon_mcp.client import OzonPerformanceClient

    monkeypatch.delenv(limits.BUDGET_UNIT_ENV, raising=False)
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("запрос ушёл в Ozon с непроверенной единицей бюджета")

    client._ensure_token = must_not_be_called
    with pytest.raises(limits.UnmeasuredUnitError):
        await client.campaign_update(42708950, daily_budget_rub=1234)


def test_no_bare_budget_conversion_is_left_in_the_client():
    """Сторож по исходнику: молчаливое умножение бюджета не должно вернуться."""
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1] / "ozon_mcp" / "client.py"
    text = source.read_text(encoding="utf-8")
    offenders = [line.strip() for line in text.splitlines()
                 if re.search(r"[Bb]udget.*1_000_000|1_000_000.*[Bb]udget", line)]
    assert not offenders, f"бюджет снова преобразуется напрямую: {offenders}"
