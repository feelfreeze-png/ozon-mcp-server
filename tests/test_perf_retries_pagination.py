"""C2 и C3: ретраи Performance-клиента, очередь выгрузок, обход списка кампаний.

**C2.** У Seller-клиента ретраи были, у Performance — нет. Цена разная: один 429 здесь
это потерянный день рекламной статистики, переснять который нечем. Плюс очередь: Ozon
держит ровно одну активную выгрузку на аккаунт и отвергает вторую мгновенно.

**C3.** Прежний код брал ровно первую страницу из ста при 248 SKU-кампаниях в кабинете —
замерено живым вызовом 21.09.2026. Половина кабинета не существовала для агента, и ни
один признак на это не указывал. 🔴 Поэтому главный тест здесь отрицательный: оборванный
обход обязан быть пойман сверкой с `total`, а не пройти молча.
"""

import asyncio

import httpx
import pytest

from ozon_mcp.client import OzonPerformanceClient


def _client() -> OzonPerformanceClient:
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)
    client._token = "не-секрет-тестовый"
    client._token_at = float("inf")
    client._report_slot = asyncio.Lock()
    return client


class _Transport:
    """Отвечает заранее заданной очередью ответов и считает запросы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, path, params=None, json=None):
        self.requests.append((method, path, params, json))
        status, payload, headers = self.responses.pop(0)
        return httpx.Response(
            status, json=payload, headers=headers or {},
            request=httpx.Request(method, "https://api-performance.ozon.ru" + path),
        )


async def _no_wait(_seconds=0):
    """Заглушка паузы. НЕ звать asyncio.sleep — подменённый, он позовёт сам себя."""
    return None


def _wire(client, responses):
    transport = _Transport(responses)
    client._http = transport

    async def no_token():
        return None

    client._ensure_token = no_token
    return transport


# ── C2: ретраи ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_429_is_retried_not_surfaced(monkeypatch):
    """Один 429 — это потерянный день, а наверху он выглядел бы строкой «Ошибка: …»."""
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    client = _client()
    transport = _wire(client, [
        (429, {"error": "too many"}, None),
        (200, {"ok": True}, None),
    ])
    assert await client._get("/api/client/campaign") == {"ok": True}
    assert len(transport.requests) == 2, "повтора не было"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_every_retryable_status_is_retried(monkeypatch, status):
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    client = _client()
    transport = _wire(client, [(status, {}, None), (200, {"ok": 1}, None)])
    assert await client._get("/x") == {"ok": 1}
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_client_error_is_not_retried(monkeypatch):
    """400 повторять бессмысленно: ответ не изменится, а запросы сожгут лимит."""
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    client = _client()
    transport = _wire(client, [(400, {"error": "empty campaigns"}, None)])
    with pytest.raises(httpx.HTTPStatusError):
        await client._get("/x")
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_retries_are_bounded_and_then_the_failure_is_loud(monkeypatch):
    """Бесконечный повтор — это подвисание, неотличимое от работы."""
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    client = _client()
    transport = _wire(client, [(429, {}, None)] * 10)
    with pytest.raises(httpx.HTTPStatusError):
        await client._get("/x")
    assert len(transport.requests) == client._RETRY_MAX + 1


@pytest.mark.asyncio
async def test_pause_grows_and_is_capped(monkeypatch):
    """⚠️ Performance API не отдаёт ни Retry-After, ни X-RateLimit-* — паузу берём сами."""
    waits = []

    async def capture(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", capture)
    client = _client()
    _wire(client, [(429, {}, None)] * 3 + [(200, {"ok": 1}, None)])
    await client._get("/x")
    assert waits == [1.0, 2.0, 4.0], f"пауза не растёт: {waits}"
    assert all(w <= client._RETRY_CAP_S for w in waits)


@pytest.mark.asyncio
async def test_retry_after_header_is_honoured_when_present(monkeypatch):
    waits = []

    async def capture(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", capture)
    client = _client()
    _wire(client, [(429, {}, {"Retry-After": "7"}), (200, {"ok": 1}, None)])
    await client._get("/x")
    assert waits == [7.0]


# ── C2: очередь выгрузок ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_report_waits_for_the_first():
    """🔴 Ozon держит ровно ОДНУ активную выгрузку и отвергает вторую мгновенно.

    Без очереди второй вызов терял бы свой отчёт, а выглядело бы это обычной ошибкой.
    """
    client = _client()
    order = []
    running = 0
    peak = 0

    async def fake(body, _aio):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        order.append(("начал", body["dateFrom"]))
        await asyncio.sleep(0.02)
        order.append(("кончил", body["dateFrom"]))
        running -= 1
        return {"day": body["dateFrom"]}

    client._statistics_locked = fake
    results = await asyncio.gather(
        client.statistics([1], "2026-09-19", "2026-09-19"),
        client.statistics([2], "2026-09-20", "2026-09-20"),
    )

    assert peak == 1, f"две выгрузки шли одновременно: {order}"
    assert order[0][0] == "начал" and order[1][0] == "кончил", order
    assert {r["day"] for r in results} == {"2026-09-19", "2026-09-20"}


@pytest.mark.asyncio
async def test_a_failing_report_frees_the_slot():
    """Отказ не должен запирать очередь навсегда — иначе сбор встанет молча."""
    client = _client()

    async def boom(body, _aio):
        raise RuntimeError("отчёт не собрался")

    client._statistics_locked = boom
    with pytest.raises(RuntimeError):
        await client.statistics([1], "2026-09-19", "2026-09-19")
    assert not client._report_slot.locked(), "слот остался занятым после отказа"


# ── C3: пагинация ────────────────────────────────────────────────────────────


def _page(items, total, start):
    return (200, {"list": [{"id": str(i)} for i in range(start, start + items)],
                  "total": total}, None)


@pytest.mark.asyncio
async def test_full_walk_collects_exactly_total():
    """Оракул берётся из самого ответа: абсолютные числа устареют на первой кампании."""
    client = _client()
    transport = _wire(client, [_page(100, 248, 0), _page(100, 248, 100), _page(48, 248, 200)])
    result = await client.campaigns_all(adv_object_type="SKU", pause_s=0)

    assert result["total"] == 248
    assert len(result["list"]) == 248
    assert result["pages"] == 3, "страниц не ceil(total / page_size)"
    assert len(transport.requests) == 3
    assert [r[2]["page"] for r in transport.requests] == [1, 2, 3]


@pytest.mark.asyncio
async def test_truncated_walk_is_caught_by_total_not_passed_silently():
    """🔴 Главный тест C3: недобор обязан ронять вызов.

    Молчаливый недобор — это дефект, ради которого пакет заведён: кабинет выглядит
    меньшим, чем он есть, и отличить это от «мало кампаний» нечем.
    """
    client = _client()
    _wire(client, [_page(100, 248, 0), (200, {"list": [], "total": 248}, None)])
    with pytest.raises(ValueError, match="из 248"):
        await client.campaigns_all(pause_s=0)


@pytest.mark.asyncio
async def test_missing_total_is_refused():
    """Без `total` полноту обхода сверять не с чем — это «не проверено», а не «всё»."""
    client = _client()
    _wire(client, [(200, {"list": [{"id": "1"}]}, None)])
    with pytest.raises(ValueError, match="total"):
        await client.campaigns_all(pause_s=0)


@pytest.mark.asyncio
async def test_page_size_is_passed_through_and_paces_the_walk():
    client = _client()
    transport = _wire(client, [_page(50, 120, 0), _page(50, 120, 50), _page(20, 120, 100)])
    result = await client.campaigns_all(page_size=50, pause_s=0)
    assert result["pages"] == 3 and result["pageSize"] == 50
    assert {r[2]["pageSize"] for r in transport.requests} == {50}


@pytest.mark.asyncio
async def test_single_page_cabinet_needs_no_second_request():
    client = _client()
    transport = _wire(client, [_page(7, 7, 0)])
    result = await client.campaigns_all(pause_s=0)
    assert result["pages"] == 1 and len(transport.requests) == 1


@pytest.mark.asyncio
async def test_walk_pauses_between_pages(monkeypatch):
    """⚠️ Замерено: второй запрос подряд даёт 429, 9 секунд между страницами проходят."""
    waits = []

    async def capture(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", capture)
    client = _client()
    _wire(client, [_page(100, 150, 0), _page(50, 150, 100)])
    await client.campaigns_all()
    assert waits == [client.CAMPAIGNS_PAGE_PAUSE_S], f"обход не выдержал паузу: {waits}"


@pytest.mark.asyncio
async def test_single_page_call_still_honours_page_arguments():
    """Постраничный вызов остаётся: полный обход стоит минуты и нужен не всегда."""
    client = _client()
    transport = _wire(client, [_page(10, 248, 0)])
    await client.campaigns_list(page=3, page_size=25)
    assert transport.requests[0][2]["page"] == 3
    assert transport.requests[0][2]["pageSize"] == 25


# ── Кампании по списку id: отчёту не нужен весь кабинет ──────────────────────


class ByIdsSeller:
    """Кабинет, отвечающий ровно на спрошенные идентификаторы."""

    def __init__(self, known: set[int] | None = None):
        self.known = known
        self.calls: list[list[int]] = []

    async def campaigns_list(self, campaign_ids=None, page_size=100, **kwargs):
        ids = [int(c) for c in (campaign_ids or [])]
        self.calls.append(ids)
        found = [i for i in ids if self.known is None or i in self.known]
        return {"list": [{"id": str(i), "advObjectType": "SKU"} for i in found],
                "total": len(found)}


def _client_with(seller):
    from ozon_mcp.client import OzonPerformanceClient

    client = OzonPerformanceClient.__new__(OzonPerformanceClient)
    client.campaigns_list = seller.campaigns_list
    return client


@pytest.mark.asyncio
async def test_only_the_named_campaigns_are_asked_for():
    """🔴 Замер 22.09.2026: полный обход — 158,6 с, нужные 52 кампании — 0,4 с."""
    seller = ByIdsSeller()
    got = await _client_with(seller).campaigns_by_ids([42708950, 41668952], pause_s=0)

    assert seller.calls == [[41668952, 42708950]], "спрошено не то, что просили"
    assert got["asked"] == 2 and got["missing"] == []
    assert {c["id"] for c in got["list"]} == {"41668952", "42708950"}


@pytest.mark.asyncio
async def test_duplicates_are_asked_once():
    seller = ByIdsSeller()
    await _client_with(seller).campaigns_by_ids([7, 7, 7, 9], pause_s=0)
    assert seller.calls == [[7, 9]]


@pytest.mark.asyncio
async def test_more_than_a_page_is_split_into_chunks():
    """Порция — измеренный лимит страницы, а не круглое число из головы."""
    seller = ByIdsSeller()
    ids = list(range(1, 251))
    got = await _client_with(seller).campaigns_by_ids(ids, pause_s=0)

    assert got["requests"] == 3, "250 идентификаторов при лимите 100 — это три запроса"
    assert [len(c) for c in seller.calls] == [100, 100, 50]
    assert len(got["list"]) == 250


@pytest.mark.asyncio
async def test_a_campaign_ozon_did_not_return_is_named():
    """Пропавшая кампания обязана быть названа: её расход уйдёт в «неизвестные»."""
    seller = ByIdsSeller(known={1, 3})
    got = await _client_with(seller).campaigns_by_ids([1, 2, 3], pause_s=0)

    assert got["missing"] == [2]
    assert got["asked"] == 3


@pytest.mark.asyncio
async def test_an_empty_request_asks_ozon_nothing():
    seller = ByIdsSeller()
    got = await _client_with(seller).campaigns_by_ids([], pause_s=0)
    assert seller.calls == [] and got["requests"] == 0


@pytest.mark.asyncio
async def test_a_second_page_of_results_is_refused_loudly():
    """Если фильтр перестанет умещаться в страницу, молча брать первую нельзя."""
    class Truncating(ByIdsSeller):
        async def campaigns_list(self, campaign_ids=None, page_size=100, **kwargs):
            self.calls.append(list(campaign_ids or []))
            return {"list": [{"id": "1", "advObjectType": "SKU"}], "total": 42}

    with pytest.raises(ValueError, match="не уместился на одну страницу"):
        await _client_with(Truncating()).campaigns_by_ids([1, 2], pause_s=0)


@pytest.mark.asyncio
async def test_the_pause_is_kept_between_chunks_but_not_before_the_first():
    """Пауза 9 с замерена: второй запрос подряд даёт 429. Перед первым она — трата."""
    import asyncio as _asyncio

    seller = ByIdsSeller()
    slept: list[float] = []

    async def _record(seconds):
        slept.append(seconds)

    real_sleep = _asyncio.sleep
    _asyncio.sleep = _record
    try:
        await _client_with(seller).campaigns_by_ids(list(range(1, 151)), pause_s=9.0)
    finally:
        _asyncio.sleep = real_sleep

    assert slept == [9.0], "пауза нужна между порциями, а не перед первой"
