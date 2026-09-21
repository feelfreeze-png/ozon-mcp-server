"""E3: сборка отчёта. ДРР по управляемому расходу, доля рекламных заказов, аномалии.

🔴 **Правило ДРР живёт в коде, а не в промпте модели.** Промпт можно переформулировать
незаметно и проверить нечем; правило в коде проверяется тестом — вот этим.

**Полный обход кабинета 21.09.2026 — 1702 кампании:** REF_VK 1284, SKU 250,
REF_BLOGGER 161, SEARCH_PROMO 4, BANNER 2, ALL_SKU_PROMO 1.

Отсюда классификация по СТРУКТУРНОМУ полю `advObjectType`. ⚠️ Первая редакция читала
заголовок и совпала случайно: настоящее название тарифной кампании — «Оплата за заказ
**-** все товары», через дефис, а у Ozon для неё есть отдельный тип. Реферальные
кампании — 85 % кабинета — приходят с `PaymentType = CAMPAIGN_TYPE_INVALID`, то есть по
оплате не различаются вовсе.

Обоснование границы: сложив тариф с управляемой рекламой, агент получит завышенный ДРР и
начнёт резать работающие кампании. Выбросив управляемый CPO — будет двигать ставку,
эффект которой не измеряет. Обе ошибки дорогие и обе тихие.
"""

import pytest

from ozon_mcp import report

CPC = {"id": "42708950", "advObjectType": "SKU", "PaymentType": "CPC", "title": "ростов"}
CPO_SELECTED = {"id": "11665082", "advObjectType": "SEARCH_PROMO",
                "PaymentType": "CPO", "title": "Оплата за заказ: выбранные товары"}
# 🔴 Настоящая карточка из кабинета: тип отдельный, а в названии ДЕФИС, не двоеточие.
CPO_ALL = {"id": "27440418", "advObjectType": "ALL_SKU_PROMO", "PaymentType": "CPO",
           "title": "Оплата за заказ - все товары"}
REFERRAL = {"id": "42297392", "advObjectType": "REF_BLOGGER",
            "PaymentType": "CAMPAIGN_TYPE_INVALID", "title": ""}
BANNER = {"id": "33333333", "advObjectType": "BANNER", "PaymentType": "CPM",
          "title": "баннер"}
CPO_STRANGE = {"id": "77777777", "advObjectType": "СОВСЕМ_НОВЫЙ_ТИП",
               "PaymentType": "CPO", "title": "Что-то новое"}

PERIOD = {"с": "2026-09-14", "по": "2026-09-20", "пояс": "МСК"}
FULL = {"дней в периоде": 7, "собрано": 7, "сбор провалился": [],
        "сбор не завершён": [], "сбора не было": [], "ряд полон": True}


def _row(sku, campaign, expense, orders=0, sales=0.0, model_orders=0):
    return {"sku": sku, "campaign_id": campaign, "expense": expense,
            "orders": orders, "model_orders": model_orders, "sales": sales}


# ── Классификация кампаний ───────────────────────────────────────────────────


def test_cpc_is_managed():
    """Замерено: трафареты приходят типом SKU, их 250 в кабинете."""
    assert report.classify_campaign(CPC) == report.MANAGED_CPC


def test_cpo_on_selected_products_is_managed():
    """Ставка по выбранным товарам — рычаг агента, из ДРР её выбрасывать нельзя."""
    assert report.classify_campaign(CPO_SELECTED) == report.MANAGED_CPO


def test_cpo_on_all_products_is_excluded():
    """🔴 Единственная исключаемая сущность: тариф 5%, ставка одна на кабинет."""
    assert report.classify_campaign(CPO_ALL) == report.EXCLUDED_ALL_SKU


def test_an_unfamiliar_type_goes_to_unknown_not_to_managed():
    """Незнакомый тип не угадывается по оплате.

    Отнести такую кампанию к управляемым значит рискнуть завышенным ДРР, к
    исключаемым — заниженным. Оба риска тихие, поэтому выбирается третий ответ:
    «неизвестно», и он виден отдельной строкой.
    """
    assert report.classify_campaign(CPO_STRANGE) == report.UNKNOWN_KIND


def test_referral_campaigns_are_excluded_from_per_sku_drr():
    """85 % кабинета — реферальные. По оплате их не различить вовсе.

    Замерено: они приходят с PaymentType = CAMPAIGN_TYPE_INVALID, то есть признака
    оплаты у них нет. Структурное поле здесь единственный надёжный сигнал.
    """
    assert report.classify_campaign(REFERRAL) == report.EXCLUDED_REFERRAL


def test_banners_are_excluded_too():
    """Медийная реклама не управляется по товарам, значит в ДРР по товарам не входит."""
    assert report.classify_campaign(BANNER) == report.EXCLUDED_BANNER


def test_classification_does_not_depend_on_the_title():
    """⚠️ Первая редакция читала заголовок и совпала случайно.

    Настоящее название — «Оплата за заказ - все товары», через дефис. Тип решает.
    """
    renamed = {**CPO_ALL, "title": "как угодно переименованная"}
    assert report.classify_campaign(renamed) == report.EXCLUDED_ALL_SKU
    mislabelled = {**CPC, "title": "Оплата за заказ: все товары"}
    assert report.classify_campaign(mislabelled) == report.MANAGED_CPC


def test_kinds_map_is_keyed_by_int():
    kinds = report.campaign_kinds([CPC, CPO_SELECTED, CPO_ALL, REFERRAL, BANNER])
    assert kinds == {42708950: report.MANAGED_CPC,
                     11665082: report.MANAGED_CPO,
                     27440418: report.EXCLUDED_ALL_SKU,
                     42297392: report.EXCLUDED_REFERRAL,
                     33333333: report.EXCLUDED_BANNER}


# ── ДРР ──────────────────────────────────────────────────────────────────────


def test_drr_counts_cpc_and_selected_cpo_together():
    """Управляемый расход — это CPC И CPO по выбранным товарам, а не один из них."""
    built = report.build(
        period=PERIOD, coverage=FULL,
        ad_rows=[_row(1, 42708950, 100.0, orders=2, sales=1000.0),
                 _row(1, 11665082, 50.0, orders=1, sales=500.0)],
        kinds=report.campaign_kinds([CPC, CPO_SELECTED]))
    assert built.managed_expense == pytest.approx(150.0)
    assert built.sales == pytest.approx(1500.0)
    assert built.drr == pytest.approx(0.1)


def test_the_tariff_is_excluded_from_drr_and_shown_separately():
    """🔴 Главная проверка правила: тариф не входит в ДРР и не выбрасывается."""
    built = report.build(
        period=PERIOD, coverage=FULL,
        ad_rows=[_row(1, 42708950, 100.0, orders=2, sales=1000.0),
                 _row(1, 27440418, 900.0, orders=5, sales=9000.0)],
        kinds=report.campaign_kinds([CPC, CPO_ALL]))
    assert built.managed_expense == pytest.approx(100.0)
    assert built.excluded[report.EXCLUDED_ALL_SKU] == pytest.approx(900.0)
    assert built.drr == pytest.approx(0.1), "тариф попал в ДРР и завысил его вдесятеро"
    assert built.as_dict()["расход «оплата за заказ: все товары»"] == 900.0


def test_without_classification_drr_is_not_computed():
    """⚠️ Считать по всему расходу нельзя: в кабинете 1702 кампании.

    Какие из них тариф, по самому ряду не видно, и молчаливое «посчитали по всему»
    дало бы правдоподобное завышенное число.
    """
    built = report.build(period=PERIOD, coverage=FULL,
                         ad_rows=[_row(1, 42708950, 100.0, sales=1000.0)], kinds=None)
    assert built.drr is None
    assert any("не посчитан" in note for note in built.notes)


def test_unknown_campaigns_are_set_aside_not_guessed():
    built = report.build(
        period=PERIOD, coverage=FULL,
        ad_rows=[_row(1, 42708950, 100.0, sales=1000.0),
                 _row(1, 77777777, 700.0, sales=7000.0)],
        kinds=report.campaign_kinds([CPC, CPO_STRANGE]))
    assert built.managed_expense == pytest.approx(100.0)
    assert built.excluded[report.UNKNOWN_KIND] == pytest.approx(700.0)
    assert built.drr == pytest.approx(0.1)
    assert any("незнаком" in note for note in built.notes)


def test_a_campaign_missing_from_the_map_is_unknown_not_managed():
    """Кампания, которой нет в классификации, — тоже неизвестность."""
    built = report.build(period=PERIOD, coverage=FULL,
                         ad_rows=[_row(1, 12345, 500.0, sales=5000.0)], kinds={})
    assert built.excluded[report.UNKNOWN_KIND] == pytest.approx(500.0)
    assert built.managed_expense == 0.0


def test_zero_sales_gives_no_drr_rather_than_infinity():
    built = report.build(period=PERIOD, coverage=FULL,
                         ad_rows=[_row(1, 42708950, 100.0, sales=0.0)],
                         kinds=report.campaign_kinds([CPC]))
    assert built.drr is None


# ── Доля рекламных заказов ───────────────────────────────────────────────────


def test_ad_order_share_uses_the_denominator():
    built = report.build(
        period=PERIOD, coverage=FULL,
        ad_rows=[_row(1, 42708950, 100.0, orders=3, model_orders=1, sales=1000.0)],
        kinds=report.campaign_kinds([CPC]),
        total_orders={(1, "2026-09-20"): 10, (2, "2026-09-20"): 10})
    assert built.ad_orders == 4, "модельные заказы обязаны входить — замерено в C1"
    assert built.total_orders == 20
    assert built.ad_orders_share == pytest.approx(0.2)


def test_without_a_denominator_the_share_is_absent_not_zero():
    built = report.build(period=PERIOD, coverage=FULL,
                         ad_rows=[_row(1, 42708950, 100.0, orders=3, sales=1000.0)],
                         kinds=report.campaign_kinds([CPC]))
    assert built.total_orders is None and built.ad_orders_share is None


def test_zero_total_orders_is_named_not_divided():
    """Ноль в знаменателе — это «не из чего считать», а не «ноль процентов»."""
    built = report.build(period=PERIOD, coverage=FULL,
                         ad_rows=[_row(1, 42708950, 100.0, sales=1000.0)],
                         kinds=report.campaign_kinds([CPC]), total_orders={})
    assert built.ad_orders_share is None
    assert any("не из чего считать" in note for note in built.notes)


# ── Аномалии ─────────────────────────────────────────────────────────────────


def test_spend_without_orders_is_flagged():
    built = report.build(
        period=PERIOD, coverage=FULL,
        ad_rows=[_row(1, 42708950, 500.0, orders=0, sales=0.0),
                 _row(2, 42708950, 10.0, orders=5, sales=900.0)],
        kinds=report.campaign_kinds([CPC]), stock={1: 5, 2: 5})
    assert built.anomalies["расход без заказов"] == [{"sku": 1, "expense": 500.0}]


def test_orders_without_stock_are_flagged():
    built = report.build(
        period=PERIOD, coverage=FULL,
        ad_rows=[_row(1, 42708950, 10.0, orders=3, sales=900.0)],
        kinds=report.campaign_kinds([CPC]), stock={1: 0})
    assert built.anomalies["заказы без остатка"] == [{"sku": 1, "orders": 3}]


def test_without_a_stock_snapshot_the_check_says_not_performed(monkeypatch):
    """🔴 «Не проверяли» и «проверили, остатка нет» — разные ответы.

    Первое не должно обвинять: снимка за день могло просто не быть.
    """
    built = report.build(period=PERIOD, coverage=FULL,
                         ad_rows=[_row(1, 42708950, 10.0, orders=3, sales=900.0)],
                         kinds=report.campaign_kinds([CPC]), stock=None)
    assert built.anomalies["заказы без остатка"] == []
    assert "заказы без остатка — НЕ ПРОВЕРЕНО" in built.anomalies


def test_a_broken_series_is_an_anomaly():
    coverage = {**FULL, "собрано": 5, "сбора не было": ["2026-09-16"],
                "сбор провалился": ["2026-09-17"], "ряд полон": False}
    built = report.build(period=PERIOD, coverage=coverage,
                         ad_rows=[], kinds={})
    assert built.anomalies["обрыв ряда"] == ["2026-09-16", "2026-09-17"]


def test_a_full_series_has_no_gap_anomaly():
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=[], kinds={})
    assert built.anomalies["обрыв ряда"] == []


# ── Форма отчёта ─────────────────────────────────────────────────────────────


def test_top_products_are_sorted_by_spend_and_capped():
    rows = [_row(sku, 42708950, float(sku), orders=1, sales=100.0)
            for sku in range(1, 11)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), top=3)
    assert [item["sku"] for item in built.by_sku] == [10, 9, 8]
    assert built.by_sku[0]["ДРР"] == pytest.approx(0.1)


def test_the_report_carries_coverage_next_to_the_numbers():
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=[], kinds={})
    payload = built.as_dict()
    assert payload["coverage"] is FULL
    assert payload["период"] == PERIOD


# ── Названия товаров ─────────────────────────────────────────────────────────

NAMES = {101: "Пищевое ведро 8 л с крышкой, нержавейка",
         102: "Сковорода 34 см, тройное дно"}


def test_a_named_product_carries_its_name_everywhere_it_appears():
    """Отчёт из одних чисел нельзя обсуждать с тем, кто ведёт ассортимент."""
    rows = [_row(101, 42708950, 1464.99), _row(102, 42708950, 315.0, orders=2, sales=900.0)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), names=NAMES)

    by_sku = {item["sku"]: item["товар"] for item in built.by_sku}
    assert by_sku == NAMES
    # Та же строка в аномалии: расход есть, заказов нет.
    spend = built.anomalies["расход без заказов"]
    assert [(item["sku"], item["товар"]) for item in spend] == [(101, NAMES[101])]


def test_orders_without_stock_are_named_too():
    rows = [_row(101, 42708950, 10.0, orders=3, sales=500.0)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), stock={}, names=NAMES)
    assert built.anomalies["заказы без остатка"] == [
        {"sku": 101, "товар": NAMES[101], "orders": 3}]


def test_not_asking_for_names_leaves_no_empty_field():
    """🔴 «Не спрашивали» и «спросили, не нашли» — разные утверждения.

    Пустое поле «товар» читалось бы как «у товара нет имени», а это третье, неверное.
    """
    rows = [_row(101, 42708950, 10.0)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]))
    assert "товар" not in built.by_sku[0]
    assert "товар" not in built.anomalies["расход без заказов"][0]


def test_a_missing_name_is_null_and_is_reported():
    """Ненайденное название — сигнал: каталог отстал либо карточки нет вовсе."""
    rows = [_row(101, 42708950, 10.0), _row(999, 42708950, 5.0)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), names=NAMES)

    by_sku = {item["sku"]: item["товар"] for item in built.by_sku}
    assert by_sku[999] is None, "прочерк выглядел бы как товар без имени"
    assert any("999" in note and "Названия не нашлись" in note for note in built.notes)


def test_all_names_found_says_nothing():
    rows = [_row(101, 42708950, 10.0)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), names=NAMES)
    assert not [note for note in built.notes if "Названия не нашлись" in note]


# ── Счётчики аномалий ────────────────────────────────────────────────────────


def test_anomaly_counts_come_ready_made():
    """🔴 Сторож против ошибки, случившейся живьём 21.09.2026.

    Отчёт отдал 111 товаров с расходом и без заказов, а в сводку ушло 113: считать
    длину списка пришлось тому, кто пишет текст. Число обязано быть в ответе.
    """
    rows = [_row(sku, 42708950, 10.0) for sku in range(1, 112)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), stock={1: 5})
    counts = built.as_dict()["аномалий, штук"]
    assert counts["расход без заказов"] == 111
    assert counts["расход без заказов"] == len(built.anomalies["расход без заказов"])


def test_an_unchecked_anomaly_counts_as_none_not_zero():
    """Ноль сказал бы «проверили, чисто». Проверки не было."""
    rows = [_row(1, 42708950, 10.0, orders=2, sales=100.0)]
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=rows,
                         kinds=report.campaign_kinds([CPC]), stock=None)
    counts = built.as_dict()["аномалий, штук"]
    assert counts["заказы без остатка"] is None
    assert counts["расход без заказов"] == 0, "эта проверка выполнялась — тут честный ноль"


def test_counts_cover_every_anomaly_that_has_a_list():
    built = report.build(period=PERIOD, coverage=FULL, ad_rows=[], kinds={}, stock={})
    counts = built.as_dict()["аномалий, штук"]
    assert set(counts) == {"расход без заказов", "заказы без остатка", "обрыв ряда"}
    assert all(value == 0 for value in counts.values())
