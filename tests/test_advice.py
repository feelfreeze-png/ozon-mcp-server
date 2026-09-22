"""Рекомендации: предложенные действия, по которым владелец говорит «да» или «нет».

Ворота 1 → 2 в прежней редакции требовали «владелец согласен с рекомендациями» и были
невыполнимы: рекомендаций никто не порождал. Этот модуль закрывает разрыв, и главное в
нём — **когда он отказывается советовать**.

🔴 Замер, на котором стоит `MIN_DAYS`: 20.09.2026 sku 922567890 был самой дорогой строкой
без заказов (1 464,99 ₽), а 21.09 дал заказ на 6 479 ₽ с ДРР 8,24 %. Сутки перевернули
вывод по товару целиком.
"""

import pytest

from ozon_mcp import advice

FULL = {"дней в периоде": 7, "собрано": 7, "сбор провалился": [],
        "сбор не завершён": [], "сбора не было": [], "ряд полон": True}
WINDOW = {"с": "2026-09-20", "по": "2026-09-26", "пояс": "МСК"}


def _sku(expense, orders=0, sales=0.0):
    return {"expense": expense, "orders": orders, "sales": sales}


# ── Когда советовать нельзя ──────────────────────────────────────────────────


@pytest.mark.parametrize("collected", [0, 1, 2, 6])
def test_a_short_window_is_refused_not_hedged(collected):
    """🔴 Короткое окно даёт не осторожный совет, а неверный."""
    coverage = {**FULL, "собрано": collected}
    with pytest.raises(advice.AdviceRefused) as exc:
        advice.build(window=WINDOW, coverage=coverage,
                     per_sku={1: _sku(100.0)}, stock={1: 0})
    assert "922567890" in str(exc.value), "отказ обязан называть замер, а не правило"


def test_exactly_the_minimum_is_enough():
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={1: _sku(100.0)}, stock={1: 0})
    assert len(got.items) == 1


@pytest.mark.parametrize("key", ["сбор провалился", "сбор не завершён", "сбора не было"])
def test_a_gap_in_the_window_is_refused(key):
    """Сумма расхода за период с пропуском меньше настоящей, и это не видно."""
    coverage = {**FULL, key: ["2026-09-23"]}
    with pytest.raises(advice.AdviceRefused) as exc:
        advice.build(window=WINDOW, coverage=coverage,
                     per_sku={1: _sku(100.0)}, stock={1: 0})
    assert "2026-09-23" in str(exc.value), "не названо, какой именно день пропущен"


def test_the_refusal_explains_both_directions_of_the_error():
    """Пропуск может и занизить расход, и спрятать заказ. Сказать надо про оба."""
    coverage = {**FULL, "сбора не было": ["2026-09-23"]}
    with pytest.raises(advice.AdviceRefused) as exc:
        advice.build(window=WINDOW, coverage=coverage, per_sku={}, stock={})
    text = str(exc.value)
    assert "потратить больше" in text and "заказ" in text


# ── Три вида рекомендаций ────────────────────────────────────────────────────


def test_spend_without_orders_and_without_stock_is_the_first_kind():
    """Самое дорогое из найденного: 62 % дневного бюджета 20.09 ушло на такие товары."""
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={922567890: _sku(1464.99)}, stock={922567890: 0},
                       names={922567890: "Пищевое ведро 8 л"})
    item = got.items[0]
    assert item.kind == advice.STOP_NO_STOCK
    assert item.product == "Пищевое ведро 8 л"
    assert item.grounds == {"расход": 1464.99, "заказов": 0, "выручка": 0.0,
                            "дней в окне": 7, "остаток": 0}


def test_stock_present_but_no_orders_is_a_different_advice():
    """Это вопрос карточки и цены, а не склада — и действие другое."""
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={5: _sku(700.0)}, stock={5: 3})
    assert got.items[0].kind == advice.FIX_CARD
    assert "карточку" in got.items[0].action


def test_orders_without_stock_asks_for_supply_not_for_a_bid():
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={7: _sku(50.0, orders=4, sales=900.0)}, stock={7: 0})
    assert got.items[0].kind == advice.RESTOCK
    assert "пополнить" in got.items[0].action


def test_a_working_product_gets_no_advice_and_it_is_counted():
    """Отсутствие рекомендации — тоже результат, и он должен быть виден числом."""
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={9: _sku(100.0, orders=3, sales=2000.0)}, stock={9: 5})
    assert got.items == []
    assert got.skipped == {"заказы идут, остаток есть": 1}


# ── Остаток: «не смотрели» не равно «нет» ────────────────────────────────────


def test_without_a_snapshot_no_stock_advice_is_given_at_all():
    """🔴 «Остатка нет» и «мы не смотрели» — разные утверждения, и второе опаснее."""
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={1: _sku(500.0), 2: _sku(50.0, orders=2)}, stock=None)

    assert got.items == [], "без снимка рекомендация про остаток невозможна"
    assert got.skipped == {"остаток не проверен — снимка за день нет": 2}
    assert any("не смотрели" in note for note in got.notes)


def test_an_empty_snapshot_is_not_the_same_as_a_missing_one():
    """Пустой словарь значит «проверили, остатка нет нигде» — совет строится."""
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={1: _sku(500.0)}, stock={})
    assert got.items[0].kind == advice.STOP_NO_STOCK


# ── Порядок, пороги и полнота ────────────────────────────────────────────────


def test_the_most_expensive_comes_first_within_a_kind():
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={1: _sku(100.0), 2: _sku(900.0), 3: _sku(500.0)},
                       stock={1: 0, 2: 0, 3: 0})
    assert [item.sku for item in got.items] == [2, 3, 1]


def test_kinds_are_ordered_by_what_costs_most():
    got = advice.build(
        window=WINDOW, coverage=FULL,
        per_sku={1: _sku(10.0), 2: _sku(10.0, orders=1, sales=100.0), 3: _sku(900.0)},
        stock={1: 0, 2: 0, 3: 7})
    assert [item.kind for item in got.items] == [
        advice.STOP_NO_STOCK, advice.RESTOCK, advice.FIX_CARD]


def test_a_cheap_product_can_be_cut_off_and_the_cut_is_counted():
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={1: _sku(5.0), 2: _sku(500.0)}, stock={1: 0, 2: 0},
                       min_expense=100.0)
    assert [item.sku for item in got.items] == [2]
    assert got.skipped == {"расход ниже порога 100 ₽": 1}


def test_a_truncated_list_says_so():
    """Урезанный список, выданный за полный, — тот же класс, что молчаливое усечение."""
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={i: _sku(float(i)) for i in range(1, 60)},
                       stock={i: 0 for i in range(1, 60)}, top=10)
    assert len(got.items) == 10
    assert any("из 59" in note for note in got.notes)


def test_zero_spend_is_not_advised_on():
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={1: _sku(0.0)}, stock={1: 0})
    assert got.items == []
    assert got.skipped == {"расхода в окне не было": 1}


# ── Форма выдачи ─────────────────────────────────────────────────────────────


def test_every_kind_is_printed_even_at_zero():
    """Отсутствие строки «пополнить остаток» неотличимо от «мы это не считали»."""
    payload = advice.build(window=WINDOW, coverage=FULL,
                           per_sku={1: _sku(100.0)}, stock={1: 0}).as_dict()
    assert set(payload["по видам"]) == set(advice.KIND_ORDER)
    assert payload["по видам"][advice.RESTOCK] == 0


def test_an_advice_carries_object_action_and_grounds():
    """Рекомендация без основания — мнение, а с основанием — предмет спора."""
    item = advice.build(window=WINDOW, coverage=FULL,
                        per_sku={42: _sku(300.0)}, stock={42: 0},
                        names={42: "Половник"}).items[0].as_dict()
    assert set(item) == {"вид", "sku", "товар", "предлагается", "основание"}
    assert item["sku"] == 42 and item["товар"] == "Половник"
    assert item["основание"]["расход"] == 300.0


def test_a_nameless_product_is_null_not_a_dash():
    got = advice.build(window=WINDOW, coverage=FULL,
                       per_sku={42: _sku(300.0)}, stock={42: 0})
    assert got.items[0].as_dict()["товар"] is None
