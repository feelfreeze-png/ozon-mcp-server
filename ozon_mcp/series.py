"""Хранилище накопленного ряда: отдельная база `/data/series.db`.

**Почему отдельный файл, а не `stats.db`.** В `stats.db` лежит телеметрия с ротацией:
`health_checks` чистится `DELETE … LIMIT 1000`, то есть база устроена так, что старые
строки из неё исчезают по замыслу. Деловой ряд восстановить неоткуда — `products/sku`
отдаёт только сегодня и вчера, и день, не снятый вовремя, потерян навсегда. Держать то
и другое в одном файле значит однажды применить ротацию не к тому ряду.

**Почему миграции устроены именно так.** В `stats.py` миграция выглядит вот так::

    try:
        await _db.execute("SELECT shop_id FROM tool_calls LIMIT 1")
    except Exception:
        await _db.execute("ALTER TABLE tool_calls ADD COLUMN shop_id TEXT DEFAULT ''")

Ветка `except` ловит **любой** отказ, а не только «колонки нет»: «database is locked»,
«disk I/O error», «no such table» приводят сюда же. Сервер после этого стартует как ни
в чём не бывало, схема остаётся прежней, и узнать о случившемся можно только по
отсутствию данных через неделю. Это тот самый класс, ради которого в проекте записано
правило про пустой результат проверки: **отказ неотличим от нормы**.

Здесь наоборот, и каждое свойство проверяется тестом в `tests/test_series_schema.py`:

* версия схемы хранится явно (таблица `schema_version`), а не угадывается по наличию
  колонки;
* каждая миграция выполняется **одной транзакцией вместе с записью новой версии** —
  оборванная миграция откатывается целиком, полувыполненного состояния не бывает;
* `CREATE TABLE IF NOT EXISTS` не используется **намеренно**: если таблица уже есть, а
  версия говорит, что её быть не должно, это расхождение, и его надо увидеть, а не
  замести под `IF NOT EXISTS`;
* версия из будущего (база новее кода) — отказ, а не молчаливая работа с чужой схемой;
* повреждённая `schema_version` (не ровно одна строка) — отказ, а не «считаем, что базы нет»;
* повторный номер версии в списке миграций — отказ: иначе вторая миграция с тем же
  номером не применится **никогда**, и об этом никто не узнает;
* версия читается второй раз уже внутри транзакции: сервер и сборщик стартуют
  независимо, и на пустой базе оба видят версию 0;
* таблицы объявлены `STRICT`. Ozon отдаёт числа текстом, в половине методов с запятой
  (`'2255,19'`); без `STRICT` такая строка легла бы в колонку `REAL` текстом, а `SUM()`
  прочитал бы из неё числовой префикс — 2255. Не ноль, а «почти верно»: копейки
  пропадают построчно, итог выглядит правдоподобно, и поймать это нечем;
* любое исключение поднимается наверх. Проглоченных нет ни одного.

🔴 **WAL и бэкап.** Этот модуль включает журнал WAL, и это меняет требование к бэкапу.
Замерено: при `wal_autocheckpoint=0` после 200 вставок основной файл остаётся пустым
(4 КБ), а все данные лежат в `series.db-wal` (28 КБ). Снимок через `sqlite3 .backup`
отдаёт все 200 строк и проходит `quick_check`; **обычное копирование основного файла
даёт «no such table»**. Бэкап в `src/infra/scripts/backup.sh` соседнего репозитория
снимает базы именно через `.backup` и исключает спутников `-wal`/`-shm` поимённо —
то есть уже верен. Но если его когда-нибудь «упростят» до `cp`, ряд будет
архивироваться пустым, и отказ снова станет неотличим от нормы.

🔴 **Изоляция арендаторов — на читателе.** База одна на все магазины, разделение идёт
колонкой `shop_id`. Привязка «токен → магазин» живёт в `tenancy.py` и действует на
пути `call_tool`; сборщик и отчёты ходят мимо него. Значит **каждый запрос к этим
таблицам обязан фильтровать по `shop_id`** — забытое условие покажет арендатору чужой
расход. Первичные ключи обоих рядов включают `shop_id`, поэтому записи не затирают
друг друга, но от чтения ключ не защищает.

**Чего здесь нет.** Записи в таблицы: первым писателем будет сборщик (`collector.py`,
пакет C4), он же включит хранилище в жизненный цикл приложения. Пока модуль никем не
вызывается — это осознанно: пустая база, заведённая на старте сервера, добавила бы ещё
одну причину падать всем арендаторам сразу, ничего при этом не собирая.
"""

import contextlib
import sqlite3
from pathlib import Path

import aiosqlite

DB_NAME = "series.db"

# STRICT-таблицы появились в SQLite 3.37 (2021). Без них колонка REAL принимает строку.
# Замерено (SQLite 3.50.4, таблица без STRICT): '2255,19' ложится в REAL как TEXT, а
# SUM() читает из неё числовой префикс — 2255. Копейки исчезают, сумма остаётся похожей
# на правду, и заметить это нечем. Со STRICT та же строка отвергается при записи.
# Поэтому старый SQLite отвергается по имени, а не обходится без STRICT.
MIN_SQLITE_VERSION = (3, 37, 0)


class SeriesSchemaError(RuntimeError):
    """Схема базы в состоянии, о котором нельзя молча догадываться."""


class SeriesUnavailableError(RuntimeError):
    """Хранилище не открыто. Поднимается вместо возврата `None`.

    Так писатель не может случайно написать `if db:` и тихо пропустить сутки сбора:
    «не собирали» и «не смогли записать» обязаны различаться в журнале.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Миграции. Список только растёт: правка уже выпущенной миграции изменит схему
# на новых установках и не тронет старые, то есть разведёт их молча.
# ─────────────────────────────────────────────────────────────────────────────

_M1_AD_DAILY = """
    CREATE TABLE ad_daily (
        date_msk     TEXT    NOT NULL,          -- YYYY-MM-DD, московские сутки
        sku          INTEGER NOT NULL,
        campaign_id  INTEGER NOT NULL,
        shop_id      TEXT    NOT NULL,
        expense      REAL,
        views        INTEGER,
        clicks       INTEGER,
        to_cart      INTEGER,
        orders       INTEGER,
        model_orders INTEGER,
        sales        REAL,
        model_sales  REAL,
        price        REAL,
        source       TEXT    NOT NULL,          -- products_sku | backfill_report
        fetched_at   TEXT    NOT NULL,          -- ISO-8601 обязательно с поясом
        PRIMARY KEY (date_msk, sku, campaign_id, shop_id),
        CHECK (date_msk GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        CHECK (source IN ('products_sku', 'backfill_report')),
        CHECK (fetched_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR fetched_at GLOB '*Z')
    ) STRICT
"""

_M1_STOCK_DAILY = """
    CREATE TABLE stock_daily (
        date_msk   TEXT    NOT NULL,
        sku        INTEGER NOT NULL,
        warehouse  TEXT    NOT NULL,
        shop_id    TEXT    NOT NULL,
        qty        INTEGER,
        source     TEXT    NOT NULL,            -- snapshot | placement_report
        fetched_at TEXT    NOT NULL,
        PRIMARY KEY (date_msk, sku, warehouse, shop_id),
        CHECK (date_msk GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        CHECK (source IN ('snapshot', 'placement_report')),
        CHECK (fetched_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR fetched_at GLOB '*Z')
    ) STRICT
"""

_M1_PRODUCT = """
    CREATE TABLE product (
        product_id INTEGER NOT NULL,
        shop_id    TEXT    NOT NULL,
        offer_id   TEXT,
        name       TEXT,
        archived   INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT    NOT NULL,
        PRIMARY KEY (product_id, shop_id),
        CHECK (archived IN (0, 1)),
        CHECK (updated_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR updated_at GLOB '*Z')
    ) STRICT
"""

_M1_PRODUCT_SKU = """
    CREATE TABLE product_sku (
        product_id INTEGER NOT NULL,
        shop_id    TEXT    NOT NULL,
        sku        INTEGER NOT NULL,
        source     TEXT,                        -- sds | fbo | fbs
        PRIMARY KEY (sku, shop_id),
        CHECK (source IS NULL OR source IN ('sds', 'fbo', 'fbs'))
    ) STRICT
"""

_M1_ACTION_LOG = """
    CREATE TABLE action_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        at           TEXT NOT NULL,
        shop_id      TEXT NOT NULL,
        object_kind  TEXT NOT NULL,
        object_id    TEXT NOT NULL,
        field        TEXT NOT NULL,
        value_before TEXT,
        value_after  TEXT,
        actor        TEXT NOT NULL,
        result       TEXT NOT NULL,
        error        TEXT,
        CHECK (at GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR at GLOB '*Z')
    ) STRICT
"""

MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (
        1,
        (
            _M1_AD_DAILY,
            "CREATE INDEX ad_daily_by_shop_day ON ad_daily (shop_id, date_msk)",
            _M1_STOCK_DAILY,
            "CREATE INDEX stock_daily_by_shop_day ON stock_daily (shop_id, date_msk)",
            _M1_PRODUCT,
            _M1_PRODUCT_SKU,
            "CREATE INDEX product_sku_by_product ON product_sku (product_id, shop_id)",
            _M1_ACTION_LOG,
            "CREATE INDEX action_log_by_shop_time ON action_log (shop_id, at)",
        ),
    ),
]


def latest_version() -> int:
    """Версия, до которой доводит текущий код. Попутно сверяет сам список.

    Дубль номера версии опаснее, чем кажется: цикл миграции пропускает всё, что
    `<= version`, поэтому вторая миграция с тем же номером не применится **никогда** —
    её таблиц в базе просто не будет, и ни одна проверка об этом не скажет. Слияние
    двух веток, каждая из которых добавила «миграцию 2», приводит ровно к этому.
    """
    versions = [version for version, _ in MIGRATIONS]
    if not versions:
        raise SeriesSchemaError("список миграций пуст — версия схемы неопределена")
    if versions[0] != 1 or versions != sorted(set(versions)):
        raise SeriesSchemaError(
            f"версии миграций обязаны возрастать начиная с единицы, получено {versions}. "
            "Повторный номер означает, что вторая миграция не применится никогда."
        )
    return versions[-1]


SCHEMA_VERSION = latest_version()


# ─────────────────────────────────────────────────────────────────────────────
# Механизм
# ─────────────────────────────────────────────────────────────────────────────


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ) as cur:
        return await cur.fetchone() is not None


async def current_version(db: aiosqlite.Connection) -> int:
    """Версия схемы в базе. 0 — базы ещё нет.

    «Таблицы `schema_version` нет» и «она есть, но пуста» — разные состояния, и второе
    нельзя сворачивать в первое: первое означает чистую базу, второе — повреждение.
    """
    if not await _table_exists(db, "schema_version"):
        return 0
    async with db.execute("SELECT version FROM schema_version") as cur:
        rows = await cur.fetchall()
    if len(rows) != 1:
        raise SeriesSchemaError(
            f"в schema_version {len(rows)} строк вместо одной — состояние схемы неизвестно. "
            "Считать это чистой базой нельзя: миграция пойдёт с нуля поверх существующих данных."
        )
    return int(rows[0][0])


async def migrate(db: aiosqlite.Connection) -> int:
    """Довести схему до текущей версии. Возвращает версию после прогона.

    Идемпотентна: на доведённой базе не выполняет ни одного оператора. Любой отказ
    поднимается наверх, а начатая миграция откатывается — версия при этом остаётся
    прежней, то есть повторный запуск начнёт её заново, а не продолжит с середины.
    """
    version = await current_version(db)
    target_version = latest_version()
    if version > target_version:
        raise SeriesSchemaError(
            f"база {DB_NAME} имеет версию схемы {version}, а код знает только до "
            f"{target_version}. Работать с неизвестной схемой нельзя — обновите код "
            "или восстановите базу нужной версии."
        )

    for target, statements in MIGRATIONS:
        if target <= version:
            continue
        # DDL и запись версии — одной транзакцией. Иначе отказ на пятом операторе
        # оставил бы четыре применённых и версию от прошлой миграции.
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Версия перечитывается ВНУТРИ транзакции. Между чтением выше и захватом
            # блокировки другой процесс мог применить ровно эту миграцию: сервер MCP и
            # сборщик стартуют независимо и на пустой базе оба увидят версию 0. Без
            # перечитывания второй упёрся бы в «table ad_daily already exists» —
            # громко, но на ровном месте.
            applied = await current_version(db)
            if applied >= target:
                await db.execute("ROLLBACK")
                version = applied
                continue
            for sql in statements:
                await db.execute(sql)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            await db.execute("DELETE FROM schema_version")
            await db.execute("INSERT INTO schema_version (version) VALUES (?)", (target,))
            await db.execute("COMMIT")
        except BaseException:
            # Подавляется только отказ самого отката (например, SQLite уже откатил
            # транзакцию сам). Исходная ошибка поднимается следующей строкой —
            # проглоченных отказов здесь нет.
            with contextlib.suppress(Exception):
                await db.execute("ROLLBACK")
            raise
        version = target

    return version


async def open_db(
    data_dir: Path, *, busy_timeout_s: float = 30.0
) -> aiosqlite.Connection:
    """Открыть `series.db` и довести схему. При любом отказе соединение закрывается."""
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise SeriesSchemaError(
            "нужен SQLite не ниже "
            + ".".join(str(part) for part in MIN_SQLITE_VERSION)
            + f" (STRICT-таблицы), установлен {sqlite3.sqlite_version}. "
            "Обойтись без STRICT нельзя: строка '2255,19' легла бы в колонку REAL "
            "текстом, и SUM() считал бы её как 2255 — копейки пропадали бы молча."
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(
        data_dir / DB_NAME, isolation_level=None, timeout=busy_timeout_s
    )
    try:
        db.row_factory = aiosqlite.Row
        # WAL: сборщик пишет, отчёты читают, и читатель не должен ждать писателя.
        await db.execute("PRAGMA journal_mode=WAL")
        await migrate(db)
    except BaseException:
        await db.close()
        raise
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Единственное соединение процесса — как в stats.py
# ─────────────────────────────────────────────────────────────────────────────

_db: aiosqlite.Connection | None = None


async def init_db(data_dir: Path) -> None:
    global _db
    _db = await open_db(data_dir)


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def is_enabled() -> bool:
    """Открыто ли хранилище. Нужно, чтобы отличать «нечего показать» от «нечем смотреть»."""
    return _db is not None


def connection() -> aiosqlite.Connection:
    """Соединение для писателей. Не открыто — исключение, а не `None`."""
    if _db is None:
        raise SeriesUnavailableError(
            f"{DB_NAME} не открыта: init_db не вызывался или упал. "
            "Записывать некуда — пропуск сбора обязан попасть в журнал как отказ."
        )
    return _db
