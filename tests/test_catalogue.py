"""D2: таблица `product_id ↔ offer_id ↔ sku`.

**Приёмка пакета:** число строк равно сумме активных и архивных; ни один `product_id`
не задвоен; для каждого `sku` из рекламной статистики находится ровно один `product_id`.

🔴 Два замера с боевого кабинета, на которых всё держится:
* `sku` **не уникален** — полный обход 21.09.2026: 887 карточек, 1011 строк `sku`,
  129 карточек несут по два (`sds` плюс `fbo` либо `fbs`);
* `visibility="ALL"` архивные **не отдаёт** — 856 против 31, пересечение нулевое.
  Имя поля обещает больше, чем отдаёт.
"""

import pytest
import pytest_asyncio

from ozon_mcp import catalogue, series, timezones as tz

AD_COLUMNS = ", ".join(series.AD_DAILY_COLUMNS)
AD_INSERT = (f"INSERT OR REPLACE INTO ad_daily ({AD_COLUMNS}) "
             f"VALUES ({', '.join('?' * len(series.AD_DAILY_COLUMNS))})")


class FakeSeller:
    """Кабинет с курсорной пагинацией: отдаёт страницы и считает вызовы."""

    def __init__(self, pages_by_visibility, sources=None):
        self.pages = {k: list(v) for k, v in pages_by_visibility.items()}
        self.sources = dict(sources or {})
        self.calls = []

    async def product_list(self, limit=100, last_id="", visibility="ALL"):
        self.calls.append((visibility, last_id, limit))
        queue = self.pages[visibility]
        index = 0 if not last_id else int(last_id)
        items, cursor, total = queue[index]
        return {"result": {"items": items, "last_id": cursor, "total": total}}

    async def product_info_list(self, product_id):
        """Второй источник: только он отдаёт `sources` со схемами."""
        self.calls.append(("info", tuple(product_id)))
        return {"items": [{"id": pid, "sources": self.sources.get(pid, [])}
                          for pid in product_id]}


def _product(product_id, offer, name="Товар", **skus):
    return {"product_id": product_id, "offer_id": offer, "name": name, **skus}


@pytest.fixture(autouse=True)
def _no_page_pause(monkeypatch):
    """Паузы обхода не должны навсегда обнуляться одним тестом для всех остальных."""
    monkeypatch.setattr(catalogue, "PAGE_PAUSE_S", 0)
    monkeypatch.setattr(catalogue, "INFO_PAUSE_S", 0)


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


async def _rows(conn, sql, *args):
    async with conn.execute(sql, args) as cur:
        return [tuple(r) for r in await cur.fetchall()]


# ── Обход выборок ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cursor_walk_collects_exactly_total():
    seller = FakeSeller({"ALL": [
        ([_product(i, f"A{i}") for i in range(1000)], "1", 1500),
        ([_product(i, f"A{i}") for i in range(1000, 1500)], "", 1500),
    ]})
    got = await catalogue.fetch_products(seller, visibility="ALL")
    assert len(got) == 1500
    assert len(seller.calls) == 2


@pytest.mark.asyncio
async def test_truncated_walk_is_caught_by_total():
    """🔴 Оборванный курсор выдал бы ассортимент меньшим, чем он есть."""
    seller = FakeSeller({"ALL": [([_product(1, "A1")], "", 856)]})
    with pytest.raises(ValueError, match="из 856"):
        await catalogue.fetch_products(seller, visibility="ALL")


@pytest.mark.asyncio
async def test_missing_total_is_refused():
    class NoTotal(FakeSeller):
        async def product_list(self, **kwargs):
            return {"result": {"items": [_product(1, "A1")], "last_id": ""}}

    with pytest.raises(ValueError, match="total"):
        await catalogue.fetch_products(NoTotal({}), visibility="ALL")


# ── Объединение двух выборок ─────────────────────────────────────────────────


def test_all_and_archived_are_merged_not_replaced():
    """⚠️ `ALL` означает «все неархивные». Полный ассортимент — объединение."""
    merged = catalogue.merge_visibilities(
        [_product(1, "A1"), _product(2, "A2")],
        [_product(3, "A3")],
    )
    assert len(merged) == 3
    assert {p["product_id"]: p["archived"] for p in merged} == {1: 0, 2: 0, 3: 1}


def test_unexpected_overlap_is_loud():
    """Пересечение замерено нулевым. Появилось — значит смысл visibility изменился."""
    with pytest.raises(ValueError, match="пересеклись"):
        catalogue.merge_visibilities([_product(1, "A1")], [_product(1, "A1")])


# ── sku: множественный и неуникальный ────────────────────────────────────────


def test_a_product_can_carry_several_skus():
    """🔴 Множественность живёт в `sources`, и только в нём.

    Замер полным обходом 21.09.2026: 129 карточек из 887 несут по два sku. По первой
    сотне доля не считается — она упорядочена не случайно и дала бы «все 100».
    """
    got = catalogue.extract_skus(_product(1, "A1", sources=[
        {"sku": 111, "source": "fbo"}, {"sku": 222, "source": "sds"}]))
    assert sorted(got) == [(111, "fbo"), (222, "sds")]


def test_sku_from_product_list_alone_is_a_fallback_not_the_source():
    """`/v3/product/list` отдаёт один `sku`. Брать только его — собрать неполное."""
    assert catalogue.extract_skus(_product(1, "A1", sku=101)) == [(101, None)]
    # sources перевешивает: там схемы, а здесь их нет.
    assert catalogue.extract_skus(_product(1, "A1", sku=101, sources=[
        {"sku": 111, "source": "sds"}])) == [(111, "sds")]


def test_zero_and_empty_sku_are_not_taken():
    """Ozon отдаёт 0 вместо отсутствия. Ноль — не идентификатор."""
    assert catalogue.extract_skus(_product(1, "A1", sku=0, sources=[
        {"sku": 0, "source": "fbo"}, {"sku": "", "source": "sds"}])) == []


def test_the_same_sku_twice_in_sources_is_one_row():
    assert catalogue.extract_skus(_product(1, "A1", sources=[
        {"sku": 5, "source": "fbo"}, {"sku": 5, "source": "fbo"}])) == [(5, "fbo")]


@pytest.mark.asyncio
async def test_rebuild_writes_products_and_all_their_skus(db):
    seller = FakeSeller({
        "ALL": [([_product(1, "A1"), _product(2, "A2")], "", 2)],
        "ARCHIVED": [([_product(3, "A3")], "", 1)],
    }, sources={1: [{"sku": 101, "source": "sds"}],
                2: [{"sku": 201, "source": "fbo"}, {"sku": 202, "source": "sds"}],
                3: [{"sku": 301, "source": "sds"}]})
    result = await catalogue.rebuild(seller, db, shop_id="main")

    assert result.products == 3 and result.active == 2 and result.archived == 1
    assert result.skus == 4
    assert result.multi_sku_products == 1

    assert await _rows(db, "SELECT count(*) FROM product") == [(3,)]
    assert await _rows(
        db, "SELECT sku, source FROM product_sku WHERE product_id = 2 ORDER BY sku"
    ) == [(201, "fbo"), (202, "sds")]


@pytest.mark.asyncio
async def test_rebuild_is_full_not_incremental(db):
    """Карточка, исчезнувшая из кабинета, обязана исчезнуть и здесь.

    Иначе реклама будет связываться с товаром, которого больше нет, и отчёт покажет
    расход по несуществующему SKU — правдоподобно и неверно.
    """
    first = FakeSeller({"ALL": [([_product(1, "A1", sku=101),
                                  _product(2, "A2", sku=102)], "", 2)],
                        "ARCHIVED": [([], "", 0)]})
    await catalogue.rebuild(first, db, shop_id="main")

    second = FakeSeller({"ALL": [([_product(1, "A1", sku=101)], "", 1)],
                         "ARCHIVED": [([], "", 0)]})
    await catalogue.rebuild(second, db, shop_id="main")

    assert await _rows(db, "SELECT product_id FROM product ORDER BY product_id") == [(1,)]
    assert await _rows(db, "SELECT sku FROM product_sku") == [(101,)]


@pytest.mark.asyncio
async def test_rebuild_does_not_touch_another_shop(db):
    seller = FakeSeller({"ALL": [([_product(1, "A1", sku=101)], "", 1)],
                         "ARCHIVED": [([], "", 0)]})
    await catalogue.rebuild(seller, db, shop_id="main")
    await catalogue.rebuild(seller, db, shop_id="сосед")
    assert await _rows(db, "SELECT count(*) FROM product") == [(2,)]

    again = FakeSeller({"ALL": [([], "", 0)], "ARCHIVED": [([], "", 0)]})
    await catalogue.rebuild(again, db, shop_id="main")
    assert await _rows(db, "SELECT shop_id FROM product") == [("сосед",)]


@pytest.mark.asyncio
async def test_no_product_id_is_duplicated(db):
    """Приёмка: `product_id` — ключ, и задвоиться он не может даже при двух sku."""
    seller = FakeSeller({"ALL": [([_product(1, "A1")], "", 1)],
                         "ARCHIVED": [([], "", 0)]},
                        sources={1: [{"sku": 1, "source": "sds"},
                                     {"sku": 2, "source": "fbo"}]})
    await catalogue.rebuild(seller, db, shop_id="main")
    assert await _rows(db, "SELECT count(*), count(DISTINCT product_id) FROM product") == [(1, 1)]


# ── Связь с рекламой ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_advertised_sku_resolves_to_a_product(db):
    """Приёмка: для каждого `sku` из рекламы находится ровно один `product_id`."""
    day = tz.yesterday_msk()
    seller = FakeSeller({"ALL": [([_product(1, "A1")], "", 1)],
                         "ARCHIVED": [([], "", 0)]},
                        sources={1: [{"sku": 101, "source": "sds"},
                                     {"sku": 102, "source": "fbo"}]})
    await catalogue.rebuild(seller, db, shop_id="main")

    for sku in (101, 102):
        await db.execute(AD_INSERT, (day, sku, 777, "main", 1.0, 10, 1, 0, 0, 0,
                                     0.0, 0.0, 0.0, "products_sku",
                                     "2026-09-20T03:15:00+03:00"))
    await db.commit()

    assert await catalogue.unresolved_skus(db, shop_id="main", day=day) == []
    assert await _rows(
        db,
        "SELECT DISTINCT p.product_id FROM ad_daily a JOIN product_sku p "
        "ON p.sku = a.sku AND p.shop_id = a.shop_id WHERE a.date_msk = ?", day,
    ) == [(1,)], "два sku одного товара обязаны вести к одному product_id"


@pytest.mark.asyncio
async def test_an_advertised_sku_without_a_product_is_reported(db):
    """🔴 Пустой список значит «сверено». Непустой — что таблица отстала от кабинета."""
    day = tz.yesterday_msk()
    await db.execute(AD_INSERT, (day, 999, 777, "main", 1.0, 10, 1, 0, 0, 0,
                                 0.0, 0.0, 0.0, "products_sku",
                                 "2026-09-20T03:15:00+03:00"))
    await db.commit()
    assert await catalogue.unresolved_skus(db, shop_id="main", day=day) == [999]


@pytest.mark.asyncio
async def test_a_failing_rebuild_leaves_the_old_table_intact(db):
    """Полное обновление идёт транзакцией: половина ассортимента хуже устаревшего."""
    good = FakeSeller({"ALL": [([_product(1, "A1", sku=101)], "", 1)],
                       "ARCHIVED": [([], "", 0)]})
    await catalogue.rebuild(good, db, shop_id="main")

    class Broken(FakeSeller):
        async def product_list(self, **kwargs):
            raise RuntimeError("кабинет не ответил")

    with pytest.raises(RuntimeError):
        await catalogue.rebuild(Broken({}), db, shop_id="main")
    assert await _rows(db, "SELECT product_id FROM product") == [(1,)]


@pytest.mark.asyncio
async def test_rebuild_reads_the_second_source(db):
    """🔴 Сторож против возврата первой редакции.

    Она обошлась одним `/v3/product/list` и насчитала ноль карточек с двумя `sku`,
    пройдя при этом структурную приёмку: суммы сходились, ключи были уникальны.
    Полноту такая приёмка не мерит — поэтому здесь проверяется сам вызов.
    """
    seller = FakeSeller({"ALL": [([_product(1, "A1"), _product(2, "A2")], "", 2)],
                         "ARCHIVED": [([], "", 0)]},
                        sources={1: [{"sku": 11, "source": "sds"},
                                     {"sku": 12, "source": "fbo"}],
                                 2: [{"sku": 21, "source": "sds"}]})
    result = await catalogue.rebuild(seller, db, shop_id="main")

    assert ("info", (1, 2)) in seller.calls, "второй источник не запрашивался"
    assert result.multi_sku_products == 1
    assert result.skus == 3


@pytest.mark.asyncio
async def test_a_card_the_second_source_skipped_is_named(db):
    """Молчаливая неполнота здесь неотличима от полноты — значит её надо назвать."""
    class Partial(FakeSeller):
        async def product_info_list(self, product_id):
            self.calls.append(("info", tuple(product_id)))
            return {"items": [{"id": product_id[0], "sources": [{"sku": 11, "source": "sds"}]}]}

    seller = Partial({"ALL": [([_product(1, "A1"), _product(2, "A2")], "", 2)],
                      "ARCHIVED": [([], "", 0)]})
    result = await catalogue.rebuild(seller, db, shop_id="main")
    assert result.detail["без второго источника"] == [2]
    assert result.detail["без второго источника, всего"] == 1


@pytest.mark.asyncio
async def test_an_unfamiliar_scheme_is_surfaced(db):
    """Схема решает, какие остатки и какая реклама относятся к этому sku."""
    seller = FakeSeller({"ALL": [([_product(1, "A1")], "", 1)], "ARCHIVED": [([], "", 0)]},
                        sources={1: [{"sku": 11, "source": "невиданная_схема"}]})
    result = await catalogue.rebuild(seller, db, shop_id="main")
    assert result.detail["незнакомые схемы"] == ["невиданная_схема"]
