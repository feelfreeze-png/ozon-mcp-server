"""A3: схема `series.db` и механизм миграций.

Проверяется не только «схема создалась», но и то, ради чего пакет затевался: **отказ
миграции обязан быть виден**. Прежний механизм (`stats.py`) оборачивал `ALTER TABLE` в
`try/except` с пустой веткой и проглатывал любой отказ, включая «database is locked» —
сервер стартовал, схема оставалась прежней, и узнать об этом было неоткуда.

Отрицательных тестов здесь больше, чем положительных, и это соразмерно: положительный
доказывает, что код работает сегодня, отрицательный — что поломка завтра будет громкой.
"""

import ast
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from ozon_mcp import series

TABLES = {"ad_daily", "stock_daily", "product", "product_sku", "action_log"}

AD_ROW = (
    "2026-09-19", 123456, 777, "shop1",
    1.5, 100, 10, 3, 2, 1, 250.0, 120.0, 990.0,
    "products_sku", "2026-09-20T03:15:00+03:00",
)
AD_COLUMNS = (
    "date_msk, sku, campaign_id, shop_id, expense, views, clicks, to_cart, "
    "orders, model_orders, sales, model_sales, price, source, fetched_at"
)
AD_INSERT = f"INSERT INTO ad_daily ({AD_COLUMNS}) VALUES ({', '.join('?' * 15)})"

STOCK_INSERT = (
    "INSERT INTO stock_daily (date_msk, sku, warehouse, shop_id, qty, source, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
STOCK_ROW = ("2026-09-19", 123456, "Хоругвино", "shop1", 7, "snapshot", "2026-09-20T03:15:00+03:00")


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await series.open_db(tmp_path)
    yield conn
    await conn.close()


async def _version(conn):
    async with conn.execute("SELECT version FROM schema_version") as cur:
        return (await cur.fetchone())[0]


async def _tables(conn):
    async with conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
        return {row[0] for row in await cur.fetchall()}


# ── миграция с нуля ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_migration_creates_the_whole_schema(db):
    assert TABLES <= await _tables(db)
    assert await _version(db) == series.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_series_lives_in_its_own_file(tmp_path):
    conn = await series.open_db(tmp_path)
    await conn.close()
    assert (tmp_path / "series.db").exists()
    # Телеметрия с ротацией и деловой ряд не должны делить файл.
    assert not (tmp_path / "stats.db").exists()


# ── идемпотентность ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_run_changes_nothing_and_keeps_data(tmp_path):
    first = await series.open_db(tmp_path)
    await first.execute(AD_INSERT, AD_ROW)
    await first.close()

    second = await series.open_db(tmp_path)
    try:
        assert await _version(second) == series.SCHEMA_VERSION
        async with second.execute("SELECT count(*) FROM schema_version") as cur:
            assert (await cur.fetchone())[0] == 1, "версия обязана быть ровно одной строкой"
        async with second.execute("SELECT count(*) FROM ad_daily") as cur:
            assert (await cur.fetchone())[0] == 1, "повторная миграция снесла данные"
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_migrate_on_a_current_database_executes_nothing(db, monkeypatch):
    """Идемпотентность не «повторный CREATE прошёл», а «ни одного оператора».

    Проверяется подменой `execute`: миграция на доведённой базе обязана только прочитать
    версию. Без этого теста в миграции можно было бы держать `CREATE TABLE IF NOT EXISTS`
    и считать повторный прогон идемпотентным, хотя он молча чинил бы расхождение схемы.
    """
    executed = []
    original = db.execute

    def spy(sql, *args, **kwargs):
        executed.append(" ".join(str(sql).split()))
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(db, "execute", spy)
    assert await series.migrate(db) == series.SCHEMA_VERSION

    assert not [sql for sql in executed if sql.upper().startswith(("BEGIN", "CREATE", "INSERT"))], (
        f"миграция на доведённой базе что-то выполнила: {executed}"
    )


# ── отрицательные: отказ обязан быть громким ─────────────────────────────────


@pytest.mark.asyncio
async def test_broken_migration_raises_and_rolls_back_whole(tmp_path, monkeypatch):
    """Главный тест пакета: сломанная миграция падает и не оставляет половины.

    Версия обязана остаться прежней — иначе повторный запуск «продолжит» с середины и
    доведёт схему до состояния, которого нет ни в одной версии.
    """
    monkeypatch.setattr(
        series,
        "MIGRATIONS",
        [
            *series.MIGRATIONS,
            (
                2,
                (
                    "CREATE TABLE half_applied (x INTEGER)",
                    "CREATE TABLE ВОТ ЗДЕСЬ СЛОМАНО (",
                ),
            ),
        ],
    )

    with pytest.raises(sqlite3.OperationalError):
        await series.open_db(tmp_path)

    # Открываем тем же кодом, но уже с исправным списком миграций.
    # Короткий таймаут здесь не для скорости, а ради внятности отказа: если проглатывание
    # когда-нибудь вернётся, первое открытие оставит транзакцию незакрытой, и это
    # обернётся падением через секунду, а не тридцатисекундным ожиданием блокировки.
    monkeypatch.undo()
    conn = await series.open_db(tmp_path, busy_timeout_s=1.0)
    try:
        assert await _version(conn) == 1, "версия сдвинулась несмотря на отказ"
        assert "half_applied" not in await _tables(conn), "откат не снял начатое"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_locked_database_is_reported_not_swallowed(tmp_path, monkeypatch):
    """Тот самый случай, который проглатывал `try/except` в `stats.py`.

    База занята чужой транзакцией. Правильный исход — исключение. Неправильный —
    «сервер стартовал, схема старая», и об этом никто не узнаёт.
    """
    first = await series.open_db(tmp_path)
    await first.close()

    monkeypatch.setattr(
        series,
        "MIGRATIONS",
        [*series.MIGRATIONS, (2, ("CREATE TABLE later (x INTEGER)",))],
    )

    blocker = sqlite3.connect(tmp_path / series.DB_NAME, isolation_level=None, timeout=1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await series.open_db(tmp_path, busy_timeout_s=0.1)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    monkeypatch.undo()
    conn = await series.open_db(tmp_path)
    try:
        assert await _version(conn) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_version_from_the_future_is_refused(tmp_path):
    """База новее кода: работать с чужой схемой вслепую нельзя."""
    conn = await series.open_db(tmp_path)
    await conn.execute("DELETE FROM schema_version")
    await conn.execute("INSERT INTO schema_version (version) VALUES (99)")
    await conn.close()

    with pytest.raises(series.SeriesSchemaError, match="99"):
        await series.open_db(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [0, 2])
async def test_damaged_version_table_is_not_read_as_a_fresh_database(tmp_path, rows):
    """Ни одной строки и две строки — повреждение, а не «схемы ещё нет».

    Свернуть это в «чистая база» значило бы запустить миграцию с нуля поверх
    накопленного ряда — и упереться в уже существующие таблицы уже после того,
    как отказ стал бы выглядеть штатным.
    """
    conn = await series.open_db(tmp_path)
    await conn.execute("DELETE FROM schema_version")
    for _ in range(rows):
        await conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    await conn.close()

    with pytest.raises(series.SeriesSchemaError, match="schema_version"):
        await series.open_db(tmp_path)


@pytest.mark.asyncio
async def test_existing_tables_without_version_are_refused(tmp_path):
    """Таблицы есть, версии нет — расхождение, а не повод дописать недостающее.

    Именно здесь `CREATE TABLE IF NOT EXISTS` был бы вреден: он подогнал бы базу
    неизвестного происхождения под текущую схему, ничего не сказав.
    """
    raw = sqlite3.connect(tmp_path / series.DB_NAME)
    raw.execute("CREATE TABLE ad_daily (whatever TEXT)")
    raw.commit()
    raw.close()

    with pytest.raises(sqlite3.OperationalError, match="ad_daily"):
        await series.open_db(tmp_path)


def test_no_swallowing_branches_in_the_module():
    """Сторож против возврата прежней привычки: каждый `except` обязан бросать дальше.

    ⚠️ Первая редакция искала подстроку `"except Exception:\\n        pass"` — то есть
    ловила проглатывание ровно при восьмипробельном отступе. Мутация с другим отступом
    сторожу была не видна. Поэтому здесь разбор синтаксиса, а не поиск по тексту: у
    формы отступа не должно быть права решать, сработает проверка или нет.

    Отдельно сторожится `contextlib.suppress`: он разрешён ровно внутри обработчика,
    который бросает исключение дальше (у нас — подавление отказа самого отката). Любой
    другой `suppress` — это проглатывание, просто записанное иначе.
    """
    tree = ast.parse(Path(series.__file__).read_text(encoding="utf-8"))

    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    swallowing = [h.lineno for h in handlers if not any(isinstance(x, ast.Raise) for x in ast.walk(h))]
    assert not swallowing, f"в series.py обработчик без повторного броска, строки: {swallowing}"

    reraising = [
        (h.lineno, h.end_lineno)
        for h in handlers
        if any(isinstance(x, ast.Raise) for x in ast.walk(h))
    ]
    stray = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            if name != "suppress":
                continue
            if not any(start <= node.lineno <= end for start, end in reraising):
                stray.append(node.lineno)
    assert not stray, f"contextlib.suppress вне обработчика, бросающего дальше: строки {stray}"


@pytest.mark.asyncio
async def test_duplicate_migration_version_is_refused(tmp_path, monkeypatch):
    """Дубль номера версии — самый тихий из возможных отказов.

    Цикл пропускает всё, что `<= version`, поэтому вторая миграция с тем же номером не
    применится НИКОГДА: её таблиц в базе не будет, версия при этом «правильная», и ни
    одна проверка не заметит. Так кончается слияние двух веток, каждая из которых
    добавила «миграцию 2».
    """
    monkeypatch.setattr(
        series,
        "MIGRATIONS",
        [
            *series.MIGRATIONS,
            (2, ("CREATE TABLE from_branch_a (x INTEGER) STRICT",)),
            (2, ("CREATE TABLE from_branch_b (x INTEGER) STRICT",)),
        ],
    )
    with pytest.raises(series.SeriesSchemaError, match="не применится никогда"):
        await series.open_db(tmp_path)


@pytest.mark.asyncio
async def test_migration_applied_by_another_process_is_not_repeated(tmp_path, monkeypatch):
    """Версия перечитывается внутри транзакции — иначе второй процесс падает зря.

    Сервер MCP и сборщик стартуют независимо: на пустой базе оба прочитают версию 0,
    и пока один мигрирует, второй ждёт блокировку с устаревшим числом на руках. Здесь
    это состояние воспроизведено точно: внешнее чтение отдаёт 0, внутреннее — правду.
    """
    ready = await series.open_db(tmp_path)
    await ready.close()

    real = series.current_version
    seen = []

    async def stale_outside_transaction(db):
        seen.append(1)
        return 0 if len(seen) == 1 else await real(db)

    monkeypatch.setattr(series, "current_version", stale_outside_transaction)

    db = await aiosqlite.connect(tmp_path / series.DB_NAME, isolation_level=None)
    try:
        assert await series.migrate(db) == series.SCHEMA_VERSION
        assert len(seen) >= 2, "версия внутри транзакции не перечитывалась"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_too_old_sqlite_is_refused_by_name(tmp_path, monkeypatch):
    """Старый SQLite не умеет STRICT. Отказ обязан назвать причину, а не сыпать синтаксисом."""
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.36.0")
    with pytest.raises(series.SeriesSchemaError, match="STRICT"):
        await series.open_db(tmp_path)


# ── схема держит то, что обещает ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comma_decimal_never_reaches_a_money_column(db):
    """🔴 Ozon отдаёт числа текстом, и в половине методов с запятой: `'2255,19'`.

    Замерено на таблице БЕЗ STRICT (SQLite 3.50.4): такая строка ложится в колонку REAL
    текстом, а `SUM()` читает из неё числовой префикс — 2255. Не ноль, а «почти верно»:
    копейки пропадают построчно, итог выглядит правдоподобно, и поймать это нечем.
    Со STRICT строка отвергается при записи — отказ становится виден сразу.
    """
    with pytest.raises(sqlite3.IntegrityError, match="REAL column"):
        await db.execute(AD_INSERT, (*AD_ROW[:4], "2255,19", *AD_ROW[5:]))

    async with db.execute("SELECT count(*) FROM ad_daily") as cur:
        assert (await cur.fetchone())[0] == 0, "строка с запятой всё-таки записалась"


@pytest.mark.asyncio
async def test_numeric_text_is_converted_not_stored_as_text(db):
    """Точечная форма (`'1256.36'`) — законное число, и хранится числом.

    Это не поблажка, а измеренное свойство STRICT: преобразование допускается ровно
    тогда, когда оно без потерь и обратимо. Проверяем `typeof`, а не факт вставки:
    без STRICT вставка прошла бы тоже, только значение осталось бы текстом.
    """
    await db.execute(AD_INSERT, (*AD_ROW[:4], "1256.36", *AD_ROW[5:]))
    async with db.execute("SELECT typeof(expense), expense FROM ad_daily") as cur:
        kind, value = await cur.fetchone()
    assert kind == "real", f"expense хранится как {kind}, а не числом"
    assert value == 1256.36


@pytest.mark.asyncio
async def test_non_numeric_text_is_rejected_in_key_columns(db):
    """Мусор в числовой колонке отвергается; число, записанное текстом, — нет."""
    with pytest.raises(sqlite3.IntegrityError, match="INTEGER column"):
        await db.execute(AD_INSERT, (AD_ROW[0], "не число", *AD_ROW[2:]))
    with pytest.raises(sqlite3.IntegrityError, match="INTEGER column"):
        await db.execute(AD_INSERT, (AD_ROW[0], "12.5", *AD_ROW[2:]))


@pytest.mark.asyncio
async def test_every_table_is_strict(db):
    """Сторож: новая таблица без STRICT вернёт прежнюю дыру.

    `sqlite_sequence` заводит сам SQLite под AUTOINCREMENT — его STRICT не касается.
    """
    async with db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
        "AND name != 'schema_version' AND name NOT LIKE 'sqlite_%'"
    ) as cur:
        rows = await cur.fetchall()
    lax = [name for name, sql in rows if "STRICT" not in (sql or "").upper()]
    assert not lax, f"таблицы без STRICT: {lax}"
    assert {name for name, _ in rows} == TABLES


@pytest.mark.asyncio
async def test_source_is_mandatory_in_both_series_tables(db):
    """🔴 Снимок и бэкфилл смешивать нельзя — значит `source` не может быть пустым."""
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO ad_daily (date_msk, sku, campaign_id, shop_id, fetched_at) "
            "VALUES ('2026-09-19', 1, 1, 's', '2026-09-20T03:15:00+03:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO stock_daily (date_msk, sku, warehouse, shop_id, fetched_at) "
            "VALUES ('2026-09-19', 1, 'w', 's', '2026-09-20T03:15:00+03:00')"
        )


@pytest.mark.asyncio
async def test_unknown_source_is_rejected(db):
    """Опечатка в имени источника не должна заводить третий, ничей ряд."""
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(AD_INSERT, (*AD_ROW[:13], "products-sku", AD_ROW[14]))
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(STOCK_INSERT, (*STOCK_ROW[:5], "placement", STOCK_ROW[6]))


@pytest.mark.asyncio
async def test_timestamp_without_a_timezone_is_rejected(db):
    """Наивная метка времени — источник сдвига на три часа, который никто не заметит."""
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(AD_INSERT, (*AD_ROW[:14], "2026-09-20T03:15:00"))
    await db.execute(AD_INSERT, (*AD_ROW[:14], "2026-09-20T00:15:00Z"))


@pytest.mark.asyncio
async def test_day_must_be_a_calendar_day_not_a_timestamp(db):
    """`date_msk` — сутки. Метка времени здесь развалила бы первичный ключ на дубли."""
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(AD_INSERT, ("2026-09-19T00:00:00+03:00", *AD_ROW[1:]))


@pytest.mark.asyncio
async def test_repeat_of_the_same_day_collides_on_the_primary_key(db):
    """Основание приёмки C4: повторный сбор перезаписывает, а не задваивает."""
    await db.execute(AD_INSERT, AD_ROW)
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(AD_INSERT, AD_ROW)
    await db.execute(AD_INSERT.replace("INSERT INTO", "INSERT OR REPLACE INTO"), AD_ROW)
    async with db.execute("SELECT count(*) FROM ad_daily") as cur:
        assert (await cur.fetchone())[0] == 1

    await db.execute(STOCK_INSERT, STOCK_ROW)
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(STOCK_INSERT, STOCK_ROW)


@pytest.mark.asyncio
async def test_same_sku_in_two_shops_is_two_rows(db):
    """Арендаторы не должны затирать друг друга: `shop_id` входит в ключ обеих таблиц."""
    await db.execute(AD_INSERT, AD_ROW)
    await db.execute(AD_INSERT, (*AD_ROW[:3], "shop2", *AD_ROW[4:]))
    async with db.execute("SELECT count(*) FROM ad_daily") as cur:
        assert (await cur.fetchone())[0] == 2


# ── соединение процесса ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writer_gets_an_error_not_none_when_storage_is_closed(tmp_path):
    """`connection()` не возвращает `None` — иначе писатель напишет `if db:` и промолчит."""
    assert not series.is_enabled()
    with pytest.raises(series.SeriesUnavailableError):
        series.connection()

    await series.init_db(tmp_path)
    try:
        assert series.is_enabled()
        assert series.connection() is not None
    finally:
        await series.close_db()

    assert not series.is_enabled()
    with pytest.raises(series.SeriesUnavailableError):
        series.connection()
