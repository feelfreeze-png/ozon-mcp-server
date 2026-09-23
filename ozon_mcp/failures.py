"""Структурный отказ инструмента: четыре исхода, которые нельзя сливать.

**Почему не плоский текст.** Раньше отказ уходил строкой `Ошибка: ValueError: …`.
Читатель-программа на такой строке либо падает на разборе JSON, либо — что хуже —
записывает её как пустой результат. А «пусто» у Ozon означает минимум три разных вещи:
активности не было; запрос не попал в окно; фильтр отсёк всё.

**Четыре исхода, и различать их обязан вызывающий:**

* **данные** — Ozon ответил, строки есть;
* **пусто-подтверждённое** — Ozon ответил, строк нет. Это ответ: расхода не было;
* **пусто-неизвестное** — ответ обрезан, разбор не удался, обход упёрся в потолок.
  Строк нет, но и утверждать «их нет» нельзя;
* **отказ** — до данных не дошли вовсе.

Второе и третье выглядят одинаково — пустой список, — и именно поэтому сливать их
нельзя: первое означает «ноль», второе «неизвестно», и среднее между ними неверно.

**Род отказа** различается по источнику, а не по тексту:

* `ozon_http` — Ozon ответил кодом ошибки. Несёт `status`, и по нему видно, стоит ли
  повторять (429 и 5xx — да, 400 — нет);
* `ozon_business` — код 200, а в теле поле `error`. Отказ, выдающий себя за данные;
* `network` — до Ozon не дошли: таймаут, DNS, обрыв;
* `forbidden` — запрос отвергнут нашей же границей доступа, до Ozon не пошёл вовсе.
  Отдельный род от `our_bug`, потому что чинить тут нечего: это штатный ответ «не
  твоё». Слитый с `our_bug`, он читался бы как «сервер сломан» и уводил бы разбор
  в код вместо выданных прав;
* `our_bug` — нарушен наш собственный контракт: лимит, пояс, состав ответа. Повторять
  бессмысленно, чинить надо код.
"""

from __future__ import annotations

from typing import Any

import httpx

from ozon_mcp.tenancy import ForeignShopError

OZON_HTTP = "ozon_http"
OZON_BUSINESS = "ozon_business"
NETWORK = "network"
FORBIDDEN = "forbidden"
OUR_BUG = "our_bug"

#: Коды, на которых повтор осмыслен. Тот же набор, что у ретраев клиента.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Исходы вызова. Названы, чтобы их нельзя было перепутать местами.
DATA = "data"
EMPTY_CONFIRMED = "empty_confirmed"
EMPTY_UNKNOWN = "empty_unknown"
FAILED = "failed"


class OzonBusinessError(RuntimeError):
    """Ozon ответил кодом 200 и полем `error` в теле.

    Отдельный род, потому что отличить его от данных по коду ответа невозможно: он
    приходит по успешному пути и выглядит ответом.
    """

    def __init__(self, message: str, *, endpoint: str = "") -> None:
        super().__init__(message)
        self.endpoint = endpoint


class EndpointRetired(OzonBusinessError):
    """Метода нет в публичном API Ozon. Заглушка, которая раньше отдавала `error`."""


def classify(exc: BaseException) -> dict[str, Any]:
    """Разобрать исключение в структуру отказа."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return {
            "kind": OZON_HTTP,
            "status": status,
            "message": _http_message(exc),
            "retryable": status in RETRYABLE_STATUSES,
        }
    if isinstance(exc, OzonBusinessError):
        return {
            "kind": OZON_BUSINESS,
            "status": 200,
            "message": str(exc),
            "retryable": False,
            "endpoint": exc.endpoint or None,
        }
    if isinstance(exc, httpx.RequestError):
        return {
            "kind": NETWORK,
            "status": None,
            "message": f"{type(exc).__name__}: {exc}",
            "retryable": True,
        }
    if isinstance(exc, ForeignShopError):
        # Список доступного кладётся в сам отказ: без него модель знает только, что
        # «нельзя», и следующий её шаг — перебор. С ним она называет причину и
        # переспрашивает по делу.
        return {
            "kind": FORBIDDEN,
            "status": None,
            "message": str(exc),
            "retryable": False,
            "requested": exc.requested,
            "allowed": list(exc.allowed),
        }
    return {
        "kind": OUR_BUG,
        "status": None,
        "message": f"{type(exc).__name__}: {exc}",
        # Повторять бессмысленно: ответ не изменится, пока не изменится код.
        "retryable": False,
    }


def _http_message(exc: httpx.HTTPStatusError) -> str:
    """Текст отказа Ozon вместе с телом: без него причина 400 теряется."""
    body = ""
    try:
        body = exc.response.text[:400]
    except Exception:  # pragma: no cover — тело может быть недоступно
        raise
    head = f"HTTP {exc.response.status_code}"
    return f"{head}: {body}" if body else head


def envelope(exc: BaseException) -> dict[str, Any]:
    """Отказ в том виде, в котором он уходит наружу."""
    return {"_error": classify(exc)}


def human_text(error: dict[str, Any]) -> str:
    """Тот же отказ словами. Блок для человека сохраняется рядом со структурой."""
    detail = error.get("_error", error)
    kind = detail.get("kind")
    status = detail.get("status")
    where = {
        OZON_HTTP: "Ozon ответил ошибкой",
        OZON_BUSINESS: "Ozon ответил успехом, но в теле отказ",
        NETWORK: "до Ozon не дошли",
        FORBIDDEN: "запрос отвергнут границей доступа, в Ozon не отправлялся",
        OUR_BUG: "нарушен наш собственный контракт",
    }.get(kind, "отказ")
    tail = " Повтор осмыслен." if detail.get("retryable") else " Повторять бессмысленно."
    status_text = f" (код {status})" if status else ""
    return f"Ошибка — {where}{status_text}: {detail.get('message')}.{tail}"


def outcome(payload: Any) -> str:
    """Какой из четырёх исходов перед нами.

    🔴 Пустой список сам по себе не отвечает на этот вопрос. `EMPTY_CONFIRMED` и
    `EMPTY_UNKNOWN` выглядят одинаково, а значат противоположное: «ноль» против
    «неизвестно». Различает их признак усечения, вписанный в сам ответ (B1).
    """
    if isinstance(payload, dict) and "_error" in payload:
        return FAILED
    rows = payload
    if isinstance(payload, dict):
        if payload.get("_truncated") or payload.get("_maybe_incomplete"):
            return EMPTY_UNKNOWN if not _any_rows(payload) else DATA
        rows = _first_list(payload)
    if isinstance(rows, list):
        return DATA if rows else EMPTY_CONFIRMED
    return DATA if payload not in (None, {}, "") else EMPTY_CONFIRMED


def _first_list(payload: dict) -> Any:
    for value in payload.values():
        if isinstance(value, list):
            return value
    return None


def _any_rows(payload: dict) -> bool:
    found = _first_list(payload)
    return bool(found)


#: Поля ответа `/v1/seller/info`, которые нельзя показывать никому, кроме владельца
#: кабинета, и нельзя писать в лог вообще. Замерено: `legal_name` — ФИО предпринимателя.
SELLER_PII_FIELDS = ("legal_name", "inn", "ogrn")


def mask_seller_info(payload: dict) -> dict:
    """Скрыть персональные данные в ответе о продавце.

    🔴 Маскируется по ИМЕНИ поля, а не по содержимому: угадывать ИНН по форме — значит
    однажды не угадать. Поля не выбрасываются, а заменяются пометкой: исчезнувшее поле
    неотличимо от поля, которого Ozon не прислал.
    """
    company = payload.get("company")
    if not isinstance(company, dict):
        return payload
    masked = {
        key: ("СКРЫТО" if key in SELLER_PII_FIELDS and value else value)
        for key, value in company.items()
    }
    return {**payload, "company": masked}
