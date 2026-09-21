"""C1: `products/sku` — основной источник этапа 1, и единый парсер чисел.

Контракт выверен живым вызовом на боевом кабинете и записан в ТЗ. Здесь закрепляются
ровно те его свойства, нарушение которых не видно снаружи:

* поле кампаний — только `campaignIds`; привычное `campaigns` даёт `400 empty campaigns`,
  и наверху это читается как «у аккаунта нет кампаний»;
* тело в snake_case, в отличие от соседних методов;
* окно — только сегодня и вчера по МСК;
* разделитель дробной части здесь точка, в остальных методах запятая;
* `ctr` из ответа не читается: три метода под этим именем дают три конвенции.
"""

import pytest

from ozon_mcp import numbers, timezones as tz
from ozon_mcp.client import OzonPerformanceClient, normalize_sku_rows

# ── Единый парсер чисел ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1256.36", 1256.36),      # products/sku — точка
        ("2255,19", 2255.19),      # остальные методы — запятая
        ("1 256,36", 1256.36),     # пробел как разделитель тысяч
        ("1 256.36", 1256.36),
        ("1.256,36", 1256.36),     # обе формы: последняя — дробная часть
        ("1,256.36", 1256.36),
        ("0", 0.0), ("-21.75", -21.75), ("-21,75", -21.75),
        (5, 5.0), (5.5, 5.5), ("", None), (None, None),
    ],
)
def test_one_parser_takes_both_separators(raw, expected):
    assert numbers.parse_number(raw) == expected


def test_empty_is_none_not_zero():
    """«Поля нет» и «расхода не было» — разные вещи, и сливать их нельзя.

    Ноль в `expense` означает, что кампания работала без трат; `None` — что величины
    в ответе не было. Сторож непрерывности из C5 на этой разнице и стоит.
    """
    assert numbers.parse_number("") is None
    assert numbers.parse_number("0") == 0.0


@pytest.mark.parametrize("raw", ["1,234", "-1,234", "999,000"])
def test_ambiguous_thousands_is_refused_not_guessed(raw):
    """🔴 `1,234` — это 1.234 или 1234? Разница в тысячу раз, обе правдоподобны."""
    with pytest.raises(numbers.AmbiguousNumberError):
        numbers.parse_number(raw)


@pytest.mark.parametrize("raw", ["не число", "12,5,6", True, [], {}])
def test_garbage_is_refused(raw):
    with pytest.raises(ValueError):
        numbers.parse_number(raw)


def test_fractional_where_integer_expected_is_refused():
    """Отбрасывать хвост молча нельзя: 3 клика и 3.7 клика — разные ответы."""
    assert numbers.parse_int("12") == 12
    with pytest.raises(ValueError):
        numbers.parse_int("12.5")


# ── Окно съёма ───────────────────────────────────────────────────────────────


def test_window_accepts_today_and_yesterday():
    OzonPerformanceClient.check_products_sku_window(tz.yesterday_msk(), tz.yesterday_msk())
    OzonPerformanceClient.check_products_sku_window(tz.today_msk(), tz.today_msk())


def test_window_refuses_anything_older_with_a_named_reason():
    """Свой отказ называет окно; ответ Ozon ушёл бы наверх строкой «Ошибка: …»."""
    with pytest.raises(ValueError, match="только сегодня и вчера"):
        OzonPerformanceClient.check_products_sku_window("2026-01-01", "2026-01-01")


# ── Форма запроса ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_uses_campaign_ids_and_snake_case_period():
    """🔴 `campaigns` вместо `campaignIds` даёт 400 «empty campaigns».

    Ответ утверждает, что кампаний не передали, хотя их передали. Наверху это читается
    как «у аккаунта нет кампаний» — то есть отказ выглядит как пустой результат.
    """
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)
    seen = {}

    async def capture(path, body=None):
        seen["path"], seen["body"] = path, body
        return {"rows": []}

    client._post = capture
    day = tz.yesterday_msk()
    await client.statistics_products_sku([777, 888], day, day)

    assert seen["path"] == "/api/client/statistics/products/sku"
    assert seen["body"]["campaignIds"] == ["777", "888"]
    assert "campaigns" not in seen["body"], "поле campaigns даёт 400 empty campaigns"
    assert set(seen["body"]) == {"date_from", "date_to", "campaignIds"}, (
        "тело здесь в snake_case, а не dateFrom/dateTo"
    )


@pytest.mark.asyncio
async def test_request_outside_the_window_never_leaves_the_process():
    client = OzonPerformanceClient.__new__(OzonPerformanceClient)

    async def must_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("запрос ушёл в Ozon за пределами окна")

    client._post = must_not_be_called
    with pytest.raises(ValueError):
        await client.statistics_products_sku([777], "2026-01-01", "2026-01-01")


# ── Нормализация строк ───────────────────────────────────────────────────────

LIVE_SHAPE = {
    "rows": [
        {"sku": 1234567, "date": "2026-09-19", "campaignId": 777,
         "expense": "1256.36", "orders": "3", "modelOrders": "1",
         "sales": "5400.00", "modelSales": "1800.00", "drr": "23.26",
         "ctr": "1.5", "avgCpc": "12.5", "views": "800", "clicks": "12",
         "toCart": "4", "price": "1800.00"},
    ]
}


def test_normalized_row_matches_the_series_schema():
    (row,) = normalize_sku_rows(LIVE_SHAPE)
    assert row["sku"] == 1234567 and row["campaign_id"] == 777
    assert row["date_msk"] == "2026-09-19"
    assert row["expense"] == 1256.36 and row["sales"] == 5400.0
    assert row["orders"] == 3 and row["model_orders"] == 1
    assert row["views"] == 800 and row["clicks"] == 12 and row["to_cart"] == 4


def test_ctr_is_computed_not_taken():
    """⚠️ Три метода отдают `ctr` по трём конвенциям, и какая здесь — не замерено.

    Число, посчитанное не по той конвенции, ничем не отличается от верного.
    """
    (row,) = normalize_sku_rows(LIVE_SHAPE)
    assert row["ctr"] == pytest.approx(12 / 800)
    assert row["ctr"] != 1.5, "ctr взят из ответа вместо расчёта"
    assert "drr" not in row, "drr из ответа не берём — считаем на этапе отчёта"


def test_zero_views_gives_none_not_division_by_zero():
    rows = normalize_sku_rows({"rows": [{"sku": 1, "campaignId": 2, "date": "2026-09-19",
                                         "views": "0", "clicks": "0"}]})
    assert rows[0]["ctr"] is None


def test_new_field_from_ozon_is_surfaced_not_dropped():
    """Изменение формы ответа обязано быть видно, а не раствориться."""
    rows = normalize_sku_rows({"rows": [{"sku": 1, "campaignId": 2, "date": "2026-09-19",
                                         "совершенно_новое_поле": 42}]})
    assert rows[0]["_unknown"] == {"совершенно_новое_поле": 42}


def test_missing_field_is_none_not_zero():
    rows = normalize_sku_rows({"rows": [{"sku": 1, "campaignId": 2, "date": "2026-09-19"}]})
    assert rows[0]["expense"] is None and rows[0]["orders"] is None


def test_date_is_a_plain_moscow_day():
    """`date` — суточный агрегат. Метка времени здесь развалила бы ключ ряда."""
    with pytest.raises(tz.TimezoneContractError):
        normalize_sku_rows({"rows": [{"sku": 1, "campaignId": 2,
                                      "date": "2026-09-19T00:00:00Z"}]})


def test_broken_payload_is_refused_loudly():
    for payload in ({"rows": "строка"}, {"rows": [42]}, 7):
        with pytest.raises(ValueError):
            normalize_sku_rows(payload)


def test_comma_form_also_parses_in_a_row():
    """Парсер один на все методы: если Ozon сменит разделитель, строка не сломается."""
    (row,) = normalize_sku_rows({"rows": [{"sku": 1, "campaignId": 2,
                                           "date": "2026-09-19", "expense": "2255,19"}]})
    assert row["expense"] == 2255.19
