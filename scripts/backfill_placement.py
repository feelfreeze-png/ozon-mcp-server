"""D4: загрузить сохранённый файл отчёта размещения в `stock_daily`.

Отчёт и его загрузка **разведены намеренно**. Прогон отчёта расходуется безвозвратно
(5 в сутки на кабинет), и ошибка разбора не должна его съедать: сначала байты на диск,
потом сколько угодно попыток их прочитать.

    docker cp scripts/backfill_placement.py ozon-mcp-server:/tmp/
    docker exec ozon-mcp-server python /tmp/backfill_placement.py \
        --file /data/artifacts/placement_2026-08-22_2026-09-21.xlsx \
        --from 2026-08-22 --to 2026-09-21 --dry-run

`--dry-run` печатает те же числа и ничего не пишет.
"""

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import aiosqlite  # noqa: E402

from ozon_mcp import placement, series  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="сохранённый XLSX отчёта")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--db", default="/data/series.db")
    parser.add_argument("--shop", default="main")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = pathlib.Path(args.file).read_bytes()
    parsed = placement.parse(
        data, shop_id=args.shop, date_from=args.date_from, date_to=args.date_to)

    print(f"разобрано строк: {len(parsed.rows)}")
    print(f"  дней: {len(parsed.days)} ({min(parsed.days, default='—')}…"
          f"{max(parsed.days, default='—')})")
    print(f"  SKU: {len(parsed.skus)} | складов: {len(parsed.warehouses)}")
    print(f"  отвергнуто: {len(parsed.rejected)} "
          f"(дублей {parsed.duplicates}, вне окна {parsed.out_of_window})")
    print(f"  дробное количество (qty=NULL): {parsed.fractional}")
    for item in parsed.rejected[:10]:
        print(f"    ⚠️ {item.reason}")

    if args.dry_run:
        print("--dry-run: не записано ничего")
        return 0

    db = await aiosqlite.connect(args.db)
    try:
        await series.migrate(db)
        written = await placement.load(db, parsed)
    finally:
        await db.close()
    print(f"записано строк: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
