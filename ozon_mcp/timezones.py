"""Часовые пояса: московские сутки, классификация полей даты, запрет `from`/`to`.

**Почему пакет идёт раньше сбора.** Пояс входит в ключ накопленной строки
(`ad_daily.date_msk`), и задним числом он не чинится: `products/sku` отдаёт только
сегодня и вчера, переснять неверно нарезанный день через неделю будет нечем.

**Что замерено** (спецификация § 3.3, оба способа независимы):

* рекламные сутки Ozon — **московские**, UTC+3. Подтверждено тегом `Statistics`
  официальной спеки и границами `from=21:00:00Z` / `to=20:59:59Z` в самих ответах API;
* ⚠️ измерение `day` в `/v1/analytics/data` **уже нарезано по Москве**. Механический
  перевод «всё, что из Seller, — UTC» сдвинет такой агрегат ещё на три часа, и заметить
  это будет нечем: день останется днём, просто не тем.

**Правило, из которого всё следует.** Переводим ТОЛЬКО то, что явно названо меткой
времени. Неклассифицированное поле не переводится **никогда** — оно помечается как
неизвестное и остаётся как есть. Догадка здесь стоит трёх часов, а выглядит как данные:
ровно тот класс, что в проекте описан как «отказ неотличим от нормы».

🔴 **Запрет `from`/`to`.** У методов статистики Performance API поля `from`/`to`
(RFC3339) выбирают дни по UTC, а имя файла в том же ответе рендерит их в МСК: два ответа
на один запрос, оба правдоподобные. Разрешены только `dateFrom`/`dateTo` простыми датами.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

#: Пояс рекламных суток Ozon. Перехода на летнее время в России нет с 2014 года,
#: поэтому смещение постоянное и зона задаётся числом, а не именем из базы tzdata:
#: от наличия tzdata на хосте результат зависеть не должен.
MSK = timezone(timedelta(hours=3), "MSK")

#: Границы московских суток в UTC — те самые, что видны в ответах API.
MSK_DAY_START_UTC = "21:00:00Z"
MSK_DAY_END_UTC = "20:59:59Z"


class TimezoneContractError(ValueError):
    """Нарушение контракта дат: неверный формат либо запрещённое поле периода."""


# ─────────────────────────────────────────────────────────────────────────────
# Реестр полей даты
# ─────────────────────────────────────────────────────────────────────────────

TIMESTAMP = "timestamp_utc"
"""Метка времени в UTC. Переводим в МСК."""

DAY_MSK = "day_msk"
"""Суточный агрегат, уже нарезанный по Москве. НЕ трогаем."""

UNKNOWN = "unknown"
"""Поле не классифицировано. Не трогаем и говорим об этом вслух."""

#: Явный реестр. Ведётся поимённо и только по полям, которые мы действительно читаем.
#: Догадываться по имени запрещено: `delivery_date` у одного метода метка времени,
#: у другого — плановая дата без времени, и разница в три часа между ними невидима.
FIELD_KINDS: dict[str, str] = {
    # Суточные агрегаты — уже московские
    "day": DAY_MSK,            # /v1/analytics/data, измерение day
    "date": DAY_MSK,           # Performance: statistics/products/sku, daily/json
    "date_msk": DAY_MSK,       # наше собственное поле в series.db
    # Метки времени Seller API — приходят в UTC
    "created_at": TIMESTAMP,
    "updated_at": TIMESTAMP,
    "processed_at": TIMESTAMP,
    "cancelled_at": TIMESTAMP,
    "in_process_at": TIMESTAMP,
    "expires_at": TIMESTAMP,   # /v1/roles, срок жизни Seller-ключа
    "moment": TIMESTAMP,
    # Наше поле — пишется уже с поясом
    "fetched_at": TIMESTAMP,
}

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOOKS_LIKE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}|$)")


def classify(field: str) -> str:
    """Чем является поле: меткой времени, суточным агрегатом или неизвестностью."""
    return FIELD_KINDS.get(field, UNKNOWN)


# ─────────────────────────────────────────────────────────────────────────────
# Московские сутки
# ─────────────────────────────────────────────────────────────────────────────


def parse_utc(value: str | datetime) -> datetime:
    """Разобрать метку времени. Без пояса — отказ, а не «подразумеваем UTC».

    Наивная метка — это вопрос «в каком поясе?», на который нет ответа. Подставить
    UTC молча значит ошибиться на три часа там, где строка была московской.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TimezoneContractError(
                f"не метка времени ISO-8601: {value!r}"
            ) from exc
    if moment.tzinfo is None:
        raise TimezoneContractError(
            f"метка времени без пояса: {value!r}. Подставлять пояс молча нельзя — "
            "ошибка в три часа неотличима от нормы."
        )
    return moment


def to_msk(value: str | datetime) -> datetime:
    """Метка времени в московском поясе."""
    return parse_utc(value).astimezone(MSK)


def msk_day(value: str | datetime) -> str:
    """Московские сутки, в которые попала метка времени.

    Жёсткая пара приёмки: `2026-09-19T21:30:00Z` → `2026-09-20`.
    """
    return to_msk(value).date().isoformat()


def now_msk() -> datetime:
    """Текущий момент в МСК. Не зависит от пояса хоста."""
    return datetime.now(timezone.utc).astimezone(MSK)


def now_msk_iso() -> str:
    """Метка времени для записи в ряд: ISO-8601 с поясом, как требует схема series.db."""
    return now_msk().replace(microsecond=0).isoformat()


def today_msk() -> str:
    """Сегодняшние московские сутки."""
    return now_msk().date().isoformat()


def yesterday_msk() -> str:
    """Вчерашние московские сутки — окно съёма `products/sku`."""
    return (now_msk().date() - timedelta(days=1)).isoformat()


def msk_day_start_utc(day: str | date | None = None) -> datetime:
    """Начало московских суток, выраженное в UTC.

    Нужно там, где хранятся метки в UTC, а спрашивают про московский день: сравнивать
    московскую дату с UTC-метками напрямую значит сдвинуть границу на три часа.
    """
    if day is None:
        day = now_msk().date()
    elif isinstance(day, str):
        require_plain_day(day, "day")
        day = date.fromisoformat(day)
    return datetime.combine(day, datetime.min.time(), tzinfo=MSK).astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Контракт периода у методов статистики
# ─────────────────────────────────────────────────────────────────────────────

FORBIDDEN_PERIOD_FIELDS = ("from", "to")
"""🔴 Поля периода в RFC3339. Выбирают дни по UTC, а имя файла в том же ответе — по МСК."""


def require_plain_day(value: str, field: str = "date") -> str:
    """Дата обязана быть простой: `YYYY-MM-DD`, без времени и без пояса.

    Метка времени в `dateFrom` возвращает нас к той же развилке: Ozon нарежет дни по
    одному правилу, а подпишет по другому.
    """
    if not isinstance(value, str) or not _ISO_DAY.match(value):
        raise TimezoneContractError(
            f"{field}={value!r}: у методов статистики период задаётся простой датой "
            "YYYY-MM-DD. Метка времени выбирает дни по UTC, а подписывает их по МСК."
        )
    return value


def check_statistics_period(params: dict[str, Any]) -> None:
    """Сторож периода: запрещённых полей нет, разрешённые — простыми датами."""
    present = [f for f in FORBIDDEN_PERIOD_FIELDS if f in params]
    if present:
        raise TimezoneContractError(
            f"поля {present} у методов статистики запрещены: они выбирают дни по UTC, "
            "а имя файла в том же ответе рендерит их в МСК — два ответа на один запрос. "
            "Использовать dateFrom/dateTo простыми датами."
        )
    for field in ("dateFrom", "dateTo", "date_from", "date_to"):
        if field in params:
            require_plain_day(params[field], field)


# ─────────────────────────────────────────────────────────────────────────────
# Подпись пояса в ответе
# ─────────────────────────────────────────────────────────────────────────────

_MARKS = {
    TIMESTAMP: "МСК, переведено из UTC",
    DAY_MSK: "МСК, суточный агрегат — не переводится",
    UNKNOWN: "НЕИЗВЕСТНО — поле не классифицировано, значение оставлено как есть",
}


def annotate(payload: Any) -> tuple[Any, dict[str, str]]:
    """Перевести метки времени в МСК и вернуть карту поясов по полям.

    Возвращает `(payload, marks)`. Значения суточных агрегатов не трогаются. Поля, не
    попавшие в реестр, **не переводятся** и помечаются как неизвестные: молчаливое
    «наверное, UTC» здесь и есть тот самый сдвиг на три часа.

    Карта возвращается отдельно, а не подмешивается в данные, чтобы не менять форму
    ответа: подпись нужна читателю, а ломать разбор ради неё нельзя.
    """
    marks: dict[str, str] = {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if isinstance(value, str) and _LOOKS_LIKE_DATE.match(value.strip()):
                    kind = classify(key)
                    marks.setdefault(key, _MARKS[kind])
                    if kind == TIMESTAMP:
                        try:
                            out[key] = to_msk(value).isoformat()
                            continue
                        except TimezoneContractError:
                            # Поле названо меткой времени, а пояса в значении нет.
                            # Переводить нечего — говорим об этом вместо догадки.
                            marks[key] = (
                                "НЕИЗВЕСТНО — поле объявлено меткой времени, но значение "
                                "пришло без пояса; не переведено"
                            )
                    out[key] = walk(value)
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(payload), marks
