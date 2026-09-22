"""Приёмка D4: сверка бэкфилла со снимком по порогу из `docs/ACCEPTANCE-D4.md`.

Порог записан **до** первого прогона отчёта (коммит b28e04a) и здесь не пересматривается:
смысл был в том, чтобы назвать число заранее. Скрипт только считает и называет один из
трёх исходов — ПРОЙДЕНА, ПРОВАЛЕНА, НЕ ПРОВЕДЕНА.

    docker exec ozon-mcp-server python /tmp/reconcile_d4.py --day 2026-09-21

Третий исход законен и в первый не сворачивается: ядро меньше 50 SKU означает, что
результат определяют фильтры, а не данные.

🔴 **Имена складов сравниваются без учёта регистра, и это не косметика.** Замерено
22.09.2026: отчёт печатает `Казань_РФЦ_НОВЫЙ`, снимок — `КАЗАНЬ_РФЦ_НОВЫЙ`. На точном
сравнении 5 складов из 44 выглядели «несопоставившимися», а их SKU уезжали в корзину
«не проверено» — то есть отказ моста выглядел как отсутствие данных. Свёртка делается
в Python: `lower()` SQLite кириллицу не сворачивает и дала бы тот же ложный результат.
"""

import argparse
import collections
import json
import pathlib
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

#: Порог из docs/ACCEPTANCE-D4.md. Меняется только вместе с документом и только с
#: объяснением, почему прежнее число было названо неверно.
MIN_CORE = 50
MIN_SHARE = 0.98
MAX_MISMATCH = 3


def _fold(name: str | None) -> str:
    return (name or "").casefold()


def reconcile(db_path: str, day: str, shop_id: str = "main") -> dict:
    con = sqlite3.connect(db_path)
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    def rows(source: str, target_day: str | None = None):
        sql = ("SELECT sku, warehouse_name, qty FROM stock_daily "
               "WHERE source = ? AND shop_id = ?")
        args: list = [source, shop_id]
        if target_day:
            sql += " AND date_msk = ?"
            args.append(target_day)
        return list(con.execute(sql, args))

    # Белый список FBO-складов — множество имён из самого файла за всё окно.
    whitelist_raw = {name for _, name, _ in rows("placement_report") if name}
    whitelist = {_fold(name) for name in whitelist_raw}
    snap_names_raw = {name for _, name, _ in rows("snapshot") if name}
    snap_names = {_fold(name) for name in snap_names_raw}

    # Обязательный побочный замер. Печатается дважды: точным сравнением и свёрнутым по
    # регистру — разница между ними и есть цена вопроса «имя или идентификатор».
    unmatched_exact = sorted(whitelist_raw - snap_names_raw)
    unmatched_folded = sorted(n for n in whitelist_raw if _fold(n) not in snap_names)

    report = collections.defaultdict(int)
    report_pairs: dict[tuple[int, str], int] = {}
    report_unknown_qty: set[int] = set()
    for sku, name, qty in rows("placement_report", day):
        if qty is None:
            report_unknown_qty.add(sku)
            continue
        report[sku] += qty
        report_pairs[(sku, _fold(name))] = report_pairs.get((sku, _fold(name)), 0) + qty

    def snapshot_for(target_day: str):
        inside: dict[int, int] = collections.defaultdict(int)
        everything: dict[int, int] = collections.defaultdict(int)
        present: set[int] = set()
        pairs: dict[tuple[int, str], int] = {}
        for sku, name, qty in rows("snapshot", target_day):
            present.add(sku)
            value = qty or 0
            everything[sku] += value
            pairs[(sku, _fold(name))] = value
            if _fold(name) in whitelist:
                inside[sku] += value
        return inside, everything, present, pairs

    snap_d, snap_d_all, present_d, snap_pairs = snapshot_for(day)
    snap_n, _, present_n, _ = snapshot_for(nxt)

    source_of = dict(con.execute(
        "SELECT sku, source FROM product_sku WHERE shop_id = ?", (shop_id,)))
    fbo_twin = {
        sku for (sku,) in con.execute(
            "SELECT ps.sku FROM product_sku ps JOIN product_sku fb "
            "  ON fb.product_id = ps.product_id AND fb.shop_id = ps.shop_id "
            "WHERE ps.shop_id = ? AND fb.source = 'fbo'", (shop_id,))
    }
    con.close()

    core: list[int] = []
    core_relaxed: list[int] = []
    unverified: dict[str, list[int]] = collections.defaultdict(list)
    moved = 0
    for sku in sorted(report):
        if source_of.get(sku) is None:
            unverified["нет в product_sku (source IS NULL)"].append(sku)
            continue
        if sku in report_unknown_qty:
            unverified["дробное количество в отчёте (qty=NULL)"].append(sku)
            continue
        # Склад отчёта, которого нет в снимке ни под каким регистром: сравнивать
        # нечего, и это «не проверено», а не «не сошлось».
        if any(pair not in snap_pairs for pair in report_pairs if pair[0] == sku):
            unverified["склад отчёта отсутствует в снимке"].append(sku)
            continue
        if sku not in present_d or sku not in present_n:
            unverified[f"нет снимка за {day} или {nxt}"].append(sku)
            continue
        if snap_d.get(sku, 0) != snap_n.get(sku, 0):
            moved += 1        # товар двигался — вне ядра по условию «г»
            continue
        core_relaxed.append(sku)
        if source_of.get(sku) == "fbo":     # условие «б» дословно
            core.append(sku)

    deltas = {sku: report[sku] - snap_d.get(sku, 0) for sku in core_relaxed}
    matched = [sku for sku, delta in deltas.items() if delta == 0]
    mismatched = {sku: delta for sku, delta in deltas.items() if delta != 0}
    share = len(matched) / len(core_relaxed) if core_relaxed else 0.0

    # Классы назначаются предикатом, а не подбираются под результат. Класс 3 — по
    # умолчанию; первый и второй надо заслужить. На ядре класс 2 запрещён по
    # определению ядра, поэтому здесь его и нет.
    classes: collections.Counter = collections.Counter()
    detail: dict[str, list] = collections.defaultdict(list)
    for sku, delta in sorted(mismatched.items()):
        if snap_d_all.get(sku, 0) != snap_d.get(sku, 0) and report[sku] == snap_d.get(sku, 0):
            classes["1 — товар не FBO"] += 1
            detail["1 — товар не FBO"].append(sku)
        else:
            classes["3 — ошибка сборки"] += 1
            detail["3 — ошибка сборки"].append(
                {"sku": sku, "отчёт": report[sku], "снимок": snap_d.get(sku, 0),
                 "дельта": delta})

    absent_zero = [sku for sku in present_d
                   if sku not in report and snap_d.get(sku, 0) == 0]
    absent_nonzero = [sku for sku in present_d
                      if sku not in report and snap_d.get(sku, 0) != 0]

    # Разрез «sku × склад»: он точнее суммы по SKU и показывает, что именно расходится.
    pair_total = len(report_pairs)
    pair_present = sum(1 for pair in report_pairs if pair in snap_pairs)
    pair_equal = sum(1 for pair, value in report_pairs.items()
                     if snap_pairs.get(pair) == value)

    return {
        "day": day, "next_day": nxt,
        "whitelist": len(whitelist_raw),
        "unmatched_exact": unmatched_exact,
        "unmatched_folded": unmatched_folded,
        "report_skus": len(report),
        "snapshot_skus_D": len(present_d),
        "snapshot_skus_D1": len(present_n),
        "moved_between_snapshots": moved,
        "core_literal": len(core),
        "core_relaxed": len(core_relaxed),
        "matched": len(matched),
        "mismatched": len(mismatched),
        "share": share,
        "classes": dict(classes),
        "class_detail": {key: value[:10] for key, value in detail.items()},
        "unverified": {key: len(value) for key, value in unverified.items()},
        "absent_from_report_zero_in_W": len(absent_zero),
        "absent_from_report_nonzero_in_W": len(absent_nonzero),
        "source_mix_of_report_skus": dict(collections.Counter(
            source_of.get(sku, "НЕТ В product_sku") for sku in report)),
        "report_skus_with_fbo_twin": len([sku for sku in report if sku in fbo_twin]),
        "pairs_total": pair_total,
        "pairs_present_in_snapshot": pair_present,
        "pairs_equal": pair_equal,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/data/series.db")
    parser.add_argument("--day", required=True, help="день D; нужен снимок за D и D+1")
    parser.add_argument("--shop", default="main")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    out = reconcile(args.db, args.day, args.shop)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["core_literal"] >= MIN_CORE else 2

    print(f"День D: {out['day']}, D+1: {out['next_day']}")
    print(f"Белый список FBO-складов (из самого файла): {out['whitelist']}")
    print(f"Имён складов БЕЗ соответствия в снимке: точным сравнением "
          f"{len(out['unmatched_exact'])}, без учёта регистра "
          f"{len(out['unmatched_folded'])} {out['unmatched_folded']}")
    print(f"SKU в отчёте за D: {out['report_skus']} | в снимке за D: "
          f"{out['snapshot_skus_D']} | за D+1: {out['snapshot_skus_D1']}")
    print(f"Какой sku печатает отчёт: {out['source_mix_of_report_skus']} "
          f"(с легаси-FBO-близнецом: {out['report_skus_with_fbo_twin']})")
    print(f"Пар sku×склад в отчёте: {out['pairs_total']} | нашлось в снимке: "
          f"{out['pairs_present_in_snapshot']} | совпало значением: {out['pairs_equal']}")
    print()
    print(f"ЯДРО по условию «б» дословно (product_sku.source='fbo'): {out['core_literal']}")
    print(f"Ядро без условия «б» (а+в+г) — ДИАГНОСТИКА, не приёмка: {out['core_relaxed']}")
    print(f"  двигались между снимками (вне ядра по «г»): {out['moved_between_snapshots']}")
    print(f"  совпало с дельтой 0: {out['matched']} ({out['share'] * 100:.1f} %) | "
          f"не совпало: {out['mismatched']}")
    for name, count in sorted(out["classes"].items()):
        print(f"  класс {name}: {count}")
    for name, items in sorted(out["class_detail"].items()):
        for item in items:
            print(f"    {name}: {item}")
    print("  не проверено:", out["unverified"] or "—")
    print(f"  SKU снимка вне отчёта с нулём по белому списку (класс 1 подтверждён): "
          f"{out['absent_from_report_zero_in_W']}")
    print(f"  SKU снимка вне отчёта с НЕнулём по белому списку: "
          f"{out['absent_from_report_nonzero_in_W']}")
    print()

    if out["core_literal"] < MIN_CORE:
        print(f"ИТОГ: НЕ ПРОВЕДЕНА — ядро {out['core_literal']} SKU < {MIN_CORE}.")
        print("Сворачивать это в «ПРОЙДЕНА» запрещено: на таком ядре результат "
              "определяют фильтры, а не данные.")
        return 2

    ok = out["share"] >= MIN_SHARE and out["mismatched"] <= MAX_MISMATCH
    print(f"ИТОГ: {'ПРОЙДЕНА' if ok else 'ПРОВАЛЕНА'} — доля {out['share'] * 100:.1f} % "
          f"при пороге {MIN_SHARE * 100:.0f} %, несовпавших {out['mismatched']} "
          f"при пороге {MAX_MISMATCH}.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
