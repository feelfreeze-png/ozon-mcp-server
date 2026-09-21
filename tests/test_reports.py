"""D4, часть первая: путь к файлу отчёта и три его отказа.

🔴 **Замер 21.09.2026, определивший конструкцию.** Ссылка на файл отчёта —
предподписанная и протухает. Отчёт от 19.09 с `expires_at: 2026-09-20T22:20:46Z` отдал
по своей ссылке `403 AccessDenied: validate error: signature is too old`, причём телом
**XML в 95 байт, а не XLSX**. Парсер, обёрнутый в `try/except`, показал бы пустой отчёт
вместо протухшей ссылки — и бэкфилл записал бы ноль строк как законный результат, потратив
один из пяти суточных прогонов.

Поэтому здесь проверяется не «скачалось», а то, что каждый из трёх отказов назван своим
именем: задание не досчиталось; ссылка протухла; файл пришёл, но это не архив.
"""

import httpx
import pytest

from ozon_mcp import limits, reports, timezones as tz
from ozon_mcp.client import OzonSellerClient


class FakeSeller:
    """Отдаёт заранее заданную последовательность статусов отчёта."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    async def report_info(self, code):
        self.calls += 1
        record = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        return {"result": {"report": {"code": code, **record}}}


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def instant(_seconds=0):
        return None

    monkeypatch.setattr(reports.asyncio, "sleep", instant)


# ── Период отчёта сверяется ДО отправки ──────────────────────────────────────


@pytest.mark.asyncio
async def test_period_longer_than_the_window_never_leaves_the_process():
    """🔴 Прогон расходуется безвозвратно: 5 в сутки, и потраченный не возвращается."""
    client = OzonSellerClient.__new__(OzonSellerClient)

    async def must_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("прогон потрачен на заведомо неверный период")

    client._post = must_not_be_called
    window = limits.limit("placement_report.days").value
    start = "2026-01-01"
    too_far = f"2026-02-{window - 29:02d}"  # окно + 1 день
    with pytest.raises(ValueError, match="безвозвратно"):
        await client.report_placement_create(start, too_far)


@pytest.mark.asyncio
async def test_reversed_period_is_refused():
    client = OzonSellerClient.__new__(OzonSellerClient)
    client._post = None
    with pytest.raises(ValueError, match="перевёрнут"):
        await client.report_placement_create("2026-09-10", "2026-09-01")


@pytest.mark.asyncio
async def test_timestamp_instead_of_a_day_is_refused():
    client = OzonSellerClient.__new__(OzonSellerClient)
    client._post = None
    with pytest.raises(tz.TimezoneContractError):
        await client.report_placement_create("2026-09-01T00:00:00Z", "2026-09-05")


@pytest.mark.asyncio
async def test_a_valid_period_is_sent_as_plain_days():
    client = OzonSellerClient.__new__(OzonSellerClient)
    seen = {}

    async def capture(path, body=None, **kwargs):
        seen["path"], seen["body"] = path, body
        return {"result": {"code": "REPORT_1"}}

    client._post = capture
    await client.report_placement_create("2026-09-01", "2026-09-30")
    assert seen["path"] == "/v1/report/placement/by-products/create"
    assert seen["body"]["date_from"] == "2026-09-01"
    assert seen["body"]["date_to"] == "2026-09-30"


# ── Ожидание готовности ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_waiting_returns_the_link_when_ready():
    seller = FakeSeller([{"status": "processing"}, {"status": "processing"},
                         {"status": "success", "file": "https://ir.ozone.ru/x.xlsx",
                          "expires_at": "2030-01-01T00:00:00Z"}])
    ready = await reports.wait_for_report(seller, "REPORT_1", poll_s=0)
    assert ready.url.endswith("x.xlsx")
    assert seller.calls == 3


@pytest.mark.asyncio
async def test_a_failed_report_is_named_not_treated_as_empty():
    seller = FakeSeller([{"status": "failed", "error": "нет данных за период"}])
    with pytest.raises(reports.ReportError, match="не собрался"):
        await reports.wait_for_report(seller, "REPORT_1", poll_s=0)


@pytest.mark.asyncio
async def test_ready_without_a_link_is_an_error_not_an_empty_report():
    """«Готов, но файла нет» — не пустой отчёт: файл мог быть и не пуст."""
    seller = FakeSeller([{"status": "success", "file": ""}])
    with pytest.raises(reports.ReportError, match="ссылки на файл нет"):
        await reports.wait_for_report(seller, "REPORT_1", poll_s=0)


@pytest.mark.asyncio
async def test_timeout_tells_you_to_come_back_by_code_not_to_rerun():
    """Прогон уже потрачен. Запустить новый — потратить второй из пяти."""
    seller = FakeSeller([{"status": "processing"}])
    with pytest.raises(reports.ReportError, match="по коду"):
        await reports.wait_for_report(seller, "REPORT_1", timeout_s=0, poll_s=0)


# ── Протухшая ссылка ─────────────────────────────────────────────────────────


def test_an_expired_link_is_caught_before_the_request():
    """Замер: истёкшая ссылка отдаёт 403 XML, а не ошибку сети."""
    report = reports.ReadyReport(code="R", url="https://ir.ozone.ru/x.xlsx",
                                 expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(reports.ReportExpired, match="истекла"):
        reports.check_link_is_fresh(report)


def test_a_fresh_link_passes():
    reports.check_link_is_fresh(reports.ReadyReport(
        code="R", url="https://x", expires_at="2030-01-01T00:00:00Z"))


def test_a_link_without_an_expiry_is_not_blocked():
    """Отсутствие срока — не повод отказывать: проверку сделает сам ответ."""
    reports.check_link_is_fresh(reports.ReadyReport(code="R", url="https://x"))


# ── Содержимое файла ─────────────────────────────────────────────────────────


def _http(status, body, url="https://ir.ozone.ru/x.xlsx"):
    class Fake:
        async def get(self, _url):
            return httpx.Response(status, content=body,
                                  request=httpx.Request("GET", url))

        async def aclose(self):
            return None

    return Fake()


@pytest.mark.asyncio
async def test_a_zip_is_accepted():
    body = b"PK\x03\x04" + b"\x00" * 100
    got = await reports.download_report(
        reports.ReadyReport(code="R", url="https://x", expires_at="2030-01-01T00:00:00Z"),
        client=_http(200, body))
    assert got == body


@pytest.mark.asyncio
async def test_the_measured_403_xml_is_named_not_parsed():
    """🔴 Ровно то, что пришло живьём: 95 байт XML под видом файла отчёта."""
    body = (b"<Error><Code>AccessDenied</Code><Message>validate error: "
            b"signature is too old</Message></Error>")
    with pytest.raises(reports.ReportError, match="отказ хранилища"):
        await reports.download_report(
            reports.ReadyReport(code="R", url="https://x",
                                expires_at="2030-01-01T00:00:00Z"),
            client=_http(403, body))


@pytest.mark.asyncio
async def test_a_200_that_is_not_an_archive_is_refused():
    """Код 200 не доказывает, что это отчёт. XLSX обязан начинаться с 'PK'."""
    with pytest.raises(reports.ReportError, match="не архив"):
        await reports.download_report(
            reports.ReadyReport(code="R", url="https://x",
                                expires_at="2030-01-01T00:00:00Z"),
            client=_http(200, b"<html>Service Unavailable</html>"))


@pytest.mark.asyncio
async def test_an_empty_body_with_code_200_is_refused():
    """Ноль байт — не пустая таблица, а отсутствие файла."""
    with pytest.raises(reports.ReportError, match="не архив"):
        await reports.download_report(
            reports.ReadyReport(code="R", url="https://x"), client=_http(200, b""))
