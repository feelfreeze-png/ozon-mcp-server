"""B3: московские сутки, классификация полей даты, запрет `from`/`to`.

🔴 **Инвариантности мало.** Сравнение двух прогонов под разными `TZ` проходит на
равномерно неверном коде: если всё считается по UTC — ровно тот дефект, ради которого
пакет заведён, — оба прогона совпадут и тест позеленеет. Поэтому здесь три вида проверок,
и главный из них — **оракул**: жёстко заданная пара вход-выход, которую равномерно
неверный код пройти не может.

Прогонять под разными поясами:

    TZ=Asia/Tashkent .venv/bin/python -m pytest tests/test_timezones.py -q
    TZ=UTC           .venv/bin/python -m pytest tests/test_timezones.py -q
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from ozon_mcp import timezones as tz

# ── 1. Оракул: жёсткие пары вход → московские сутки ──────────────────────────

ORACLE = [
    # Граница московских суток — 21:00:00Z. Это не выдумка: те же значения видны
    # в ответах Performance API как from=21:00:00Z / to=20:59:59Z.
    ("2026-09-19T21:30:00Z", "2026-09-20"),   # вечер 19-го по UTC = уже 20-е по МСК
    ("2026-09-19T20:59:59Z", "2026-09-19"),   # последняя секунда московских суток
    ("2026-09-19T21:00:00Z", "2026-09-20"),   # первая секунда следующих
    ("2026-09-20T00:00:00Z", "2026-09-20"),   # полночь UTC — те же сутки
    ("2026-09-20T20:59:59Z", "2026-09-20"),
    ("2026-01-01T21:00:00Z", "2026-01-02"),   # переход через год
    ("2026-06-15T12:00:00+03:00", "2026-06-15"),  # уже московская метка
    ("2026-06-15T09:00:00+00:00", "2026-06-15"),
]


@pytest.mark.parametrize("moment,expected", ORACLE)
def test_msk_day_oracle(moment, expected):
    """Пара вход-выход задана жёстко: равномерно неверный код её не пройдёт."""
    assert tz.msk_day(moment) == expected


def test_msk_day_boundary_is_exactly_three_hours():
    """Смещение именно +03:00, а не «какое-то». Проверяется разностью, а не константой."""
    utc_midnight = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert tz.to_msk(utc_midnight).utcoffset() == timedelta(hours=3)
    assert tz.to_msk(utc_midnight).hour == 3


# ── 2. Не-перевод суточного агрегата ─────────────────────────────────────────


def test_day_aggregate_is_not_converted():
    """⚠️ `day` из /v1/analytics/data уже нарезан по Москве.

    Механический перевод «всё, что из Seller, — UTC» сдвинул бы его ещё на три часа, и
    заметить это было бы нечем: день остался бы днём, просто не тем.
    """
    payload = {"result": {"data": [{"dimensions": [{"id": "1", "name": "x"}],
                                    "day": "2026-09-20", "metrics": [5]}]}}
    out, marks = tz.annotate(payload)
    assert out["result"]["data"][0]["day"] == "2026-09-20", "суточный агрегат переведён"
    assert marks["day"] == "МСК, суточный агрегат — не переводится"


def test_advertising_date_is_not_converted():
    """У Performance поле `date` — тоже московские сутки, а не метка времени."""
    out, marks = tz.annotate({"rows": [{"date": "2026-09-19", "sku": 1, "expense": "1,5"}]})
    assert out["rows"][0]["date"] == "2026-09-19"
    assert "не переводится" in marks["date"]


def test_timestamp_is_converted_and_signed():
    """Метка времени переводится, и пояс остаётся в самой строке."""
    out, marks = tz.annotate({"created_at": "2026-09-19T21:30:00Z"})
    assert out["created_at"].startswith("2026-09-20T00:30:00")
    assert out["created_at"].endswith("+03:00"), "пояс обязан остаться в значении"
    assert marks["created_at"] == "МСК, переведено из UTC"


def test_unclassified_date_field_is_left_alone_and_named():
    """Неизвестное поле не переводится и помечается — догадка стоит три часа.

    Именно здесь живёт дефект, ради которого пакет заведён: молчаливое «наверное, UTC»
    выглядит как данные и не отличается от правильного ответа ничем.
    """
    out, marks = tz.annotate({"выдуманное_поле_даты": "2026-09-19T21:30:00Z"})
    assert out["выдуманное_поле_даты"] == "2026-09-19T21:30:00Z", "поле тронули"
    assert marks["выдуманное_поле_даты"].startswith("НЕИЗВЕСТНО")


def test_timestamp_without_zone_is_reported_not_guessed():
    """Поле объявлено меткой времени, а пояса в значении нет — это тоже «неизвестно»."""
    out, marks = tz.annotate({"created_at": "2026-09-19 21:30:00"})
    assert out["created_at"] == "2026-09-19 21:30:00"
    assert marks["created_at"].startswith("НЕИЗВЕСТНО")


def test_annotate_does_not_change_the_shape():
    """Подпись возвращается отдельно: ломать разбор ради неё нельзя."""
    payload = {"a": [1, 2], "b": {"c": "не дата"}, "created_at": "2026-09-19T21:30:00Z"}
    out, _ = tz.annotate(payload)
    assert set(out) == set(payload)
    assert out["a"] == [1, 2] and out["b"] == {"c": "не дата"}


# ── 3. Инвариантность к поясу хоста ──────────────────────────────────────────


def test_result_does_not_depend_on_host_timezone(monkeypatch):
    """Внутри процесса: подмена TZ не меняет ответ.

    Слабее прогона в отдельном процессе (tzset влияет не на всё), поэтому идёт
    дополнением к оракулу, а не вместо него.
    """
    import time as _time

    results = []
    for zone in ("Asia/Tashkent", "UTC", "America/Los_Angeles"):
        monkeypatch.setenv("TZ", zone)
        if hasattr(_time, "tzset"):
            _time.tzset()
        results.append((tz.msk_day("2026-09-19T21:30:00Z"), tz.to_msk("2026-09-19T21:30:00Z").isoformat()))
    monkeypatch.delenv("TZ", raising=False)
    if hasattr(_time, "tzset"):
        _time.tzset()
    assert len(set(results)) == 1, f"ответ зависит от пояса хоста: {results}"


@pytest.mark.parametrize("zone", ["Asia/Tashkent", "UTC"])
def test_oracle_holds_in_a_real_process_under_another_timezone(zone):
    """Тот же оракул, но в ОТДЕЛЬНОМ процессе с выставленным TZ.

    Здесь проверяется не совпадение двух прогонов между собой (оно проходит и на
    равномерно неверном коде), а совпадение каждого из них с жёстко заданной парой.
    """
    env = {**os.environ, "TZ": zone}
    script = (
        "from ozon_mcp import timezones as tz;"
        "print(tz.msk_day('2026-09-19T21:30:00Z'), tz.msk_day('2026-09-19T20:59:59Z'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["2026-09-20", "2026-09-19"]


def test_today_and_yesterday_are_one_day_apart():
    from datetime import date

    assert (date.fromisoformat(tz.today_msk())
            - date.fromisoformat(tz.yesterday_msk())) == timedelta(days=1)


def test_now_msk_iso_fits_the_series_schema():
    """Метка для `fetched_at` обязана проходить CHECK из series.db (пояс в строке)."""
    import sqlite3

    value = tz.now_msk_iso()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE t (v TEXT, "
        "CHECK (v GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR v GLOB '*Z')) STRICT"
    )
    conn.execute("INSERT INTO t VALUES (?)", (value,))  # не должно бросить
    conn.close()


# ── 4. Запрет `from`/`to` у методов статистики ───────────────────────────────


@pytest.mark.parametrize("field", ["from", "to"])
def test_rfc3339_period_fields_are_forbidden(field):
    """🔴 Они выбирают дни по UTC, а имя файла в том же ответе рендерит их в МСК."""
    with pytest.raises(tz.TimezoneContractError, match="запрещены"):
        tz.check_statistics_period({field: "2026-09-19T21:00:00Z"})


def test_plain_dates_pass():
    tz.check_statistics_period({"dateFrom": "2026-09-19", "dateTo": "2026-09-20"})


@pytest.mark.parametrize(
    "value", ["2026-09-19T00:00:00Z", "2026-09-19 00:00", "19.09.2026", "2026-9-19", ""]
)
def test_timestamp_in_datefrom_is_rejected(value):
    """Метка времени в `dateFrom` возвращает к той же развилке, что и `from`."""
    with pytest.raises(tz.TimezoneContractError):
        tz.check_statistics_period({"dateFrom": value, "dateTo": "2026-09-20"})


@pytest.mark.asyncio
async def test_statistics_methods_refuse_a_timestamp_period():
    """Сторож стоит в самих методах клиента, а не только в утилите.

    Иначе он проверяет сам себя: вызывающий обойдёт его, не заметив.
    """
    from ozon_mcp.client import OzonPerformanceClient

    client = OzonPerformanceClient.__new__(OzonPerformanceClient)

    async def must_not_be_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("запрос ушёл в Ozon с запрещённым периодом")

    client._post = must_not_be_called
    client._get = must_not_be_called

    for call in (
        lambda: client.statistics([1], "2026-09-19T00:00:00Z", "2026-09-20"),
        lambda: client.statistics_daily([1], "2026-09-19T00:00:00Z", "2026-09-20"),
        lambda: client.statistics_expenses([1], "2026-09-19T00:00:00Z", "2026-09-20"),
        lambda: client.statistics_products([1], "2026-09-19T00:00:00Z", "2026-09-20"),
    ):
        with pytest.raises(tz.TimezoneContractError):
            await call()


def test_registry_covers_the_fields_the_collector_reads():
    """Поля, на которые опирается сбор, обязаны быть классифицированы явно."""
    for field, kind in (("day", tz.DAY_MSK), ("date", tz.DAY_MSK),
                        ("date_msk", tz.DAY_MSK), ("fetched_at", tz.TIMESTAMP),
                        ("expires_at", tz.TIMESTAMP)):
        assert tz.classify(field) == kind, field
    assert tz.classify("нет такого поля") == tz.UNKNOWN


# ── 5. Две починенные точки ──────────────────────────────────────────────────


def test_diagnostics_dates_are_msk_and_honestly_signed(monkeypatch):
    """⚠️ Было: значение по локальному времени хоста, подпись `Z`. Подпись лгала.

    ⚠️ Первая редакция брала пояса `Asia/Tashkent` и `UTC` — они расходятся на пять
    часов, и в большинстве часов суток дают ОДНУ И ТУ ЖЕ дату. Тест зеленел на коде,
    берущем локальное время, просто потому, что прогон случился не ночью. Здесь взяты
    крайние пояса: UTC+14 и UTC−12 расходятся на 26 часов, то есть их календарные даты
    различаются ВСЕГДА, в любой момент времени.
    """
    import time as _time

    from ozon_mcp import diagnostics

    seen = []
    for zone in ("Pacific/Kiritimati", "Etc/GMT+12"):
        monkeypatch.setenv("TZ", zone)
        if hasattr(_time, "tzset"):
            _time.tzset()
        seen.append((diagnostics._today(), diagnostics._iso(7)))
    monkeypatch.delenv("TZ", raising=False)
    if hasattr(_time, "tzset"):
        _time.tzset()

    assert len(set(seen)) == 1, f"диагностика зависит от пояса хоста: {seen}"
    today, week_ago = seen[0]
    assert today == tz.today_msk()
    assert week_ago.endswith("+03:00"), "подпись пояса обязана соответствовать значению"
    assert not week_ago.endswith("Z"), "значение в МСК не может быть подписано как UTC"


@pytest.mark.asyncio
async def test_stats_counts_the_moscow_day_not_the_utc_day(tmp_path, monkeypatch):
    """Проверяется ПРИМЕНЕНИЕ границы в stats.py, а не сама утилита.

    ⚠️ Первая редакция звала `msk_day_start_utc` напрямую и зеленела даже тогда, когда
    `stats.py` считал «сегодня» по UTC: тест проверял инструмент, а не то, что им
    пользуются. Здесь две записи по разные стороны московской полуночи, и обе помечены
    ВЧЕРАШНЕЙ датой по UTC — код, считающий день по UTC, не насчитает ни одной.
    """
    from ozon_mcp import stats

    await stats.init_db(tmp_path)
    try:
        conn = stats._db
        # 20:00Z = 23:00 МСК 19-го; 22:00Z = 01:00 МСК уже 20-го.
        for moment in ("2026-09-19 20:00:00", "2026-09-19 22:00:00"):
            await conn.execute(
                "INSERT INTO tool_calls (tool_name, called_at, duration_ms, success, shop_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("ozon_ad_campaigns", moment, 1.0, True, "shop1"),
            )
        await conn.commit()

        monkeypatch.setattr(
            tz, "msk_day_start_utc",
            lambda *a, **kw: datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc),
        )
        summary = await stats.get_summary()
        assert summary["today"] == 1, (
            "сегодня по МСК — это ровно одна из двух записей; "
            f"получено {summary['today']}"
        )
    finally:
        await stats.close_db()


def test_stats_today_boundary_is_moscow_midnight_expressed_in_utc():
    """`called_at` хранится в UTC, а спрашивают про московский день.

    Сравнивать московскую дату с UTC-метками напрямую значит сдвинуть границу на три
    часа: с полуночи до трёх ночи по Москве счётчик показывал бы вчерашний день.
    """
    start = tz.msk_day_start_utc("2026-09-20")
    assert start.isoformat() == "2026-09-19T21:00:00+00:00"
    assert start.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-19 21:00:00"


# ── Сдвиг дня ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day,delta,expected", [
    ("2026-09-26", -6, "2026-09-20"),
    ("2026-10-02", -6, "2026-09-26"),   # через границу месяца
    ("2027-01-03", -6, "2026-12-28"),   # через границу года
    ("2028-03-01", -1, "2028-02-29"),   # високосный
    ("2026-09-20", 0, "2026-09-20"),
])
def test_the_window_is_built_by_date_arithmetic(day, delta, expected):
    """🔴 Окно считается по датам, а не вычитанием секунд.

    Секундная арифметика в поясе с переходами даёт «вчера» дважды или ни разу —
    ровно тот сдвиг, ради которого заведён весь пакет B3.
    """
    assert tz.shift_day(day, delta) == expected


def test_a_timestamp_is_not_a_day_and_is_refused():
    with pytest.raises(Exception):
        tz.shift_day("2026-09-20T00:00:00Z", -1)
