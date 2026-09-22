"""D4: один прогон отчёта размещения — создать, дождаться, скачать, СОХРАНИТЬ файл.

🔴 **Прогон расходуется безвозвратно: 5 в сутки на кабинет.** Поэтому здесь нет ни
строчки разбора: сначала байты на диск, потом сколько угодно попыток их прочитать
(`backfill_placement.py`). Ошибка парсера не должна съедать прогон — 20.09.2026 ровно
так и вышло: файла не осталось, остались многоточия в спецификации.

    docker cp scripts/fetch_placement_report.py ozon-mcp-server:/tmp/
    docker exec -e PYTHONPATH=/app -w /app ozon-mcp-server \
        python /tmp/fetch_placement_report.py --from 2026-08-22 --to 2026-09-21

⚠️ Ссылка на файл живёт **три часа** (замерено 22.09.2026), перевыпуска нет: `report_info`
по вчерашнему коду отдаёт ту же протухшую ссылку и `403` телом XML. Скачивать сразу.

🟢 Кабинет создаёт такой отчёт **сам** ежедневно в 00:01 МСК. Если успеть в трёхчасовое
окно, файл берётся из `report_list` бесплатно — своего прогона не нужно вовсе.
"""

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ozon_mcp import reports  # noqa: E402
from ozon_mcp.server import get_seller_for_shop  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--out", default="/data/artifacts")
    parser.add_argument("--shop", default="main")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seller = get_seller_for_shop(args.shop)
    try:
        # Период сверяется внутри клиента ДО отправки: отвергнутый нами запрос прогона
        # не тратит, отвергнутый Ozon — тратит.
        created = await seller.report_placement_create(args.date_from, args.date_to)
        print("CREATE:", json.dumps(created, ensure_ascii=False)[:500], flush=True)
        # Ответ на 22.09.2026 — плоский {"code": …}; result.code оставлен на случай,
        # если Ozon вернётся к общей обёртке отчётов.
        code = (created.get("code")
                or (created.get("result") or {}).get("code") or "")
        if not code:
            raise SystemExit(
                "кода отчёта в ответе нет. Прогон потрачен, файла не будет — "
                f"ответ целиком: {created!r}")
        (out / "last_report_code.txt").write_text(code, encoding="utf-8")

        ready = await reports.wait_for_report(seller, code)
        print("READY:", ready.code, "| ссылка живёт до", ready.expires_at, flush=True)
        (out / "last_report_record.json").write_text(
            json.dumps(ready.raw or {}, ensure_ascii=False, indent=1), encoding="utf-8")
        body = await reports.download_report(ready)
    finally:
        await seller.close()

    path = out / f"placement_{args.date_from}_{args.date_to}.xlsx"
    path.write_bytes(body)
    print(f"SAVED: {path} | {len(body)} байт | sha256 "
          f"{hashlib.sha256(body).hexdigest()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
