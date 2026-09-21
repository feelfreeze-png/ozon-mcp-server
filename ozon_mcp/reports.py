"""Получение готовых отчётов Ozon: ожидание, скачивание, проверка содержимого.

**Почему отдельный модуль.** Отчёт — это три разных отказа, и каждый выглядит иначе:
задание не создалось; задание создалось, но не досчиталось; файл досчитался, но ссылка
протухла. Свалить их в один `try` значит получить «отчёт пуст» вместо причины.

🔴 **Ссылка на файл предподписанная и протухает.** Замерено 21.09.2026: отчёт от 19.09 с
`expires_at: 2026-09-20T22:20:46Z` отдал по своей ссылке `403 AccessDenied: validate
error: signature is too old`, причём телом **XML, а не XLSX**. Парсер, обёрнутый в
`try/except`, показал бы пустой отчёт вместо протухшей ссылки — и бэкфилл записал бы ноль
строк как законный результат.

Поэтому здесь три проверки подряд, и каждая называет свою причину: срок ссылки до
запроса, код ответа, и сигнатура ZIP в первых двух байтах. XLSX — это zip-архив, и файл,
не начинающийся с `PK`, отчётом не является, каким бы правдоподобным ни был его размер.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from . import timezones

#: Пауза между опросами готовности. Отчёт размещения считается минутами.
POLL_PAUSE_S = 10.0

#: Сколько ждать готовности, прежде чем признать, что отчёт не досчитался.
POLL_TIMEOUT_S = 900.0

_ZIP_MAGIC = b"PK"


class ReportError(RuntimeError):
    """Отказ на пути к файлу отчёта. Причина названа в тексте."""


class ReportExpired(ReportError):
    """Ссылка на файл протухла. Прогон потрачен, файла нет."""


@dataclass
class ReadyReport:
    """Готовый отчёт: код, ссылка и срок её жизни."""

    code: str
    url: str
    expires_at: str | None = None
    report_type: str | None = None
    raw: dict[str, Any] | None = None


def _record(payload: dict) -> dict:
    result = payload.get("result", payload)
    return result.get("report", result) if isinstance(result, dict) else {}


async def wait_for_report(
    seller: Any, code: str, *,
    timeout_s: float = POLL_TIMEOUT_S, poll_s: float = POLL_PAUSE_S,
) -> ReadyReport:
    """Дождаться готовности отчёта по коду.

    Три исхода, и все названы: готов; отказался считаться; не успел за отведённое время.
    Последний **не** равен первым двум: отчёт может дойти позже, и прогон при этом уже
    потрачен — значит и сообщение должно предлагать вернуться за ним по коду, а не
    запускать новый.
    """
    deadline = timezones.now_msk().timestamp() + timeout_s
    last_status = "неизвестен"
    while True:
        record = _record(await seller.report_info(code))
        last_status = str(record.get("status") or "неизвестен")
        if last_status == "success":
            url = record.get("file") or ""
            if not url:
                raise ReportError(
                    f"отчёт {code} отмечен готовым, но ссылки на файл нет. "
                    "Считать это пустым отчётом нельзя: файл мог быть и не пуст."
                )
            return ReadyReport(
                code=code, url=url, expires_at=record.get("expires_at"),
                report_type=record.get("report_type"), raw=record)
        if last_status in ("failed", "error"):
            raise ReportError(
                f"отчёт {code} не собрался: статус {last_status}, "
                f"ошибка {record.get('error')!r}. Прогон потрачен."
            )
        if timezones.now_msk().timestamp() >= deadline:
            raise ReportError(
                f"отчёт {code} не досчитался за {int(timeout_s)} с, последний статус "
                f"{last_status!r}. Прогон уже потрачен — возвращаться за ним по коду, "
                "а не запускать новый."
            )
        await asyncio.sleep(poll_s)


def check_link_is_fresh(report: ReadyReport) -> None:
    """Проверить срок ссылки ДО запроса.

    Протухшая ссылка отдаёт XML с кодом 403, а не ошибку сети. Узнать об этом после
    скачивания тоже можно, но тогда причина уедет в разбор содержимого, где выглядит
    как «файл не того формата».
    """
    if not report.expires_at:
        return
    try:
        expires = timezones.parse_utc(report.expires_at)
    except timezones.TimezoneContractError:
        return
    if expires <= timezones.now_msk():
        raise ReportExpired(
            f"ссылка на отчёт {report.code} истекла {report.expires_at}. "
            "Предподписанные ссылки Ozon живут недолго; скачивать надо сразу после "
            "готовности. Прогон потрачен, файла уже не получить."
        )


async def download_report(
    report: ReadyReport, *, client: httpx.AsyncClient | None = None,
) -> bytes:
    """Скачать файл отчёта, убедившись, что это действительно архив.

    XLSX — это zip. Файл, не начинающийся с `PK`, отчётом не является, каким бы
    правдоподобным ни был его размер: протухшая ссылка отдаёт 95 байт XML.
    """
    check_link_is_fresh(report)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    try:
        response = await http.get(report.url)
    finally:
        if owned:
            await http.aclose()

    body = response.content
    if response.status_code != 200:
        raise ReportError(
            f"файл отчёта {report.code} не отдался: код {response.status_code}, "
            f"тело {body[:200]!r}. Это не пустой отчёт, а отказ хранилища."
        )
    if body[:2] != _ZIP_MAGIC:
        raise ReportError(
            f"файл отчёта {report.code} не архив: первые байты {body[:16]!r}, "
            f"байт всего {len(body)}. XLSX обязан начинаться с 'PK'. Разбирать такое "
            "как таблицу значит получить пустой результат вместо названной причины."
        )
    return body
