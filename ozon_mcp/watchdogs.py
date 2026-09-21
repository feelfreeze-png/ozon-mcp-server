"""Сторожа накопленного ряда.

Три штуки, и каждый отвечает на свой вопрос:

1. **Непрерывность** — не пропал ли день. 🔴 Отличает «данных нет, потому что не
   собирали» от «данных нет, потому что расхода не было»: второе нормально и трогать
   его нельзя, первое означает потерянные навсегда сутки. Различие берётся из
   `collection_run`, а не из самого ряда — по ряду его не видно.
2. **Сверка** — не разошёлся ли накопленный день с переснятым. Проверяет допущение
   «цифры закрытого дня задним числом не меняются», которое **не замерено**.
3. **Сумма по SKU против `daily`** — не сменил ли Ozon семантику. Разовая приёмка C1
   доказала, что код верен сегодня; ежедневная доказывает, что он верен и завтра.

**Почему у каждого свой вывод, а не общий «всё хорошо».** Сторож, у которого «тихо» и
«не смог проверить» выглядят одинаково, хуже отсутствующего: он создаёт уверенность.
Поэтому каждый возвращает `Verdict` с полем `checked` — сколько дней он реально
посмотрел, — и `unknown` для того, о чём судить не смог.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import aiosqlite

from . import numbers, timezones


@dataclass
class Verdict:
    """Итог сторожа. `checked = 0` читается как «НЕ ПРОВЕРЕНО», а не как «чисто»."""

    name: str
    checked: int = 0
    alerts: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def quiet(self) -> bool:
        """Сторож молчит ПО ДЕЛУ: что-то проверил и возражений нет."""
        return self.checked > 0 and not self.alerts

    def __str__(self) -> str:
        if not self.checked:
            return f"{self.name}: НЕ ПРОВЕРЕНО (посмотреть было нечего)"
        head = f"{self.name}: проверено дней {self.checked}"
        if self.alerts:
            head += f", ТРЕВОГ {len(self.alerts)}"
        if self.unknown:
            head += f", неизвестно {len(self.unknown)}"
        return head


def _days(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)]


async def check_continuity(
    db: aiosqlite.Connection, *, shop_id: str, days: int = 14,
    until: str | None = None,
) -> Verdict:
    """Сторож непрерывности: дыра в ряде — тревога.

    🔴 **День из нулей трогать нельзя.** Кампании могли не крутиться, и тогда строк с
    нулевым расходом — или вовсе ни одной — это норма. Отличие несёт `collection_run`:
    прогон со статусом `ok` означает «мы смотрели, и там пусто». Отсутствие прогона —
    «не смотрели».
    """
    verdict = Verdict("непрерывность ряда")
    until = until or timezones.yesterday_msk()
    timezones.require_plain_day(until, "until")
    window = _days((date.fromisoformat(until) - timedelta(days=days - 1)).isoformat(), until)

    async with db.execute(
        "SELECT day_msk, status FROM collection_run "
        "WHERE shop_id = ? AND day_msk BETWEEN ? AND ?",
        (shop_id, window[0], window[-1]),
    ) as cur:
        runs: dict[str, set[str]] = {}
        for day, status in await cur.fetchall():
            runs.setdefault(day, set()).add(status)

    async with db.execute(
        "SELECT date_msk, count(*) FROM ad_daily "
        "WHERE shop_id = ? AND date_msk BETWEEN ? AND ? GROUP BY date_msk",
        (shop_id, window[0], window[-1]),
    ) as cur:
        rows = {day: count for day, count in await cur.fetchall()}

    # Раньше первого прогона судить не о чем: ряд просто не начинался.
    started = min(runs) if runs else None
    for day in window:
        if started is None or day < started:
            continue
        verdict.checked += 1
        statuses = runs.get(day, set())
        if not statuses:
            verdict.alerts.append(f"{day}: сбора не было вовсе — сутки потеряны")
        elif "ok" not in statuses:
            verdict.alerts.append(
                f"{day}: сбор был, но не завершился ({', '.join(sorted(statuses))})")
        elif not rows.get(day):
            # Это НЕ тревога: сбор прошёл и строк не нашёл — расхода не было.
            verdict.detail.setdefault("пустые дни", []).append(day)

    verdict.detail["дней со строками"] = len(rows)
    if started is None:
        verdict.unknown.append("прогонов в окне нет — ряд ещё не начинался")
    return verdict


async def check_against_resnapshot(
    db: aiosqlite.Connection, *, shop_id: str, day: str,
    fresh_rows: list[dict], tolerance: float = 0.01,
) -> Verdict:
    """Сторож сверки: накопленный день против переснятого.

    ⚠️ Это проверка допущения «цифры закрытого дня задним числом не меняются» — оно
    **не замерено**. Расхождение здесь не обязательно наша ошибка; оно означает, что
    допущение неверно, и это надо узнать до того, как на нём построят отчёт.
    """
    verdict = Verdict("сверка с переснятым днём")
    timezones.require_plain_day(day, "day")

    async with db.execute(
        "SELECT sku, campaign_id, expense, orders FROM ad_daily "
        "WHERE shop_id = ? AND date_msk = ?",
        (shop_id, day),
    ) as cur:
        stored = {(sku, campaign): (expense, orders)
                  for sku, campaign, expense, orders in await cur.fetchall()}

    if not stored and not fresh_rows:
        verdict.unknown.append(f"{day}: нет ни накопленного, ни переснятого — сверять нечего")
        return verdict

    fresh = {(row["sku"], row["campaign_id"]): (row.get("expense"), row.get("orders"))
             for row in fresh_rows}
    verdict.checked = 1

    for key in sorted(set(stored) | set(fresh), key=lambda k: (k[0] or 0, k[1] or 0)):
        was, now = stored.get(key), fresh.get(key)
        if was is None:
            verdict.alerts.append(f"{day} sku={key[0]} кампания={key[1]}: "
                                  "есть в переснятом, нет в накопленном")
            continue
        if now is None:
            verdict.alerts.append(f"{day} sku={key[0]} кампания={key[1]}: "
                                  "есть в накопленном, нет в переснятом")
            continue
        for index, what in ((0, "расход"), (1, "заказы")):
            old_value, new_value = was[index] or 0, now[index] or 0
            if abs(old_value - new_value) > tolerance:
                verdict.alerts.append(
                    f"{day} sku={key[0]} кампания={key[1]}: {what} "
                    f"накоплено {old_value}, переснято {new_value}")
    verdict.detail["строк накоплено"] = len(stored)
    verdict.detail["строк переснято"] = len(fresh)
    return verdict


async def check_sum_against_daily(
    db: aiosqlite.Connection, *, shop_id: str, day: str,
    daily_payload: Any, tolerance: float = 0.02,
) -> Verdict:
    """Сторож семантики: сумма по SKU против `daily/json` за тот же день.

    Спека называет эту сверку **ежедневным сторожем**, а не разовой приёмкой C1.
    Разовая доказывает, что код верен сегодня; ежедневная — что Ozon не сменил
    семантику. Замерено при приёмке C1: расхождение составило одну копейку от
    округления, а `daily.orders` равнялся сумме `orders` и `modelOrders`.
    """
    verdict = Verdict("сумма по SKU против daily")
    timezones.require_plain_day(day, "day")

    async with db.execute(
        "SELECT sum(expense), sum(orders), sum(model_orders), count(*) FROM ad_daily "
        "WHERE shop_id = ? AND date_msk = ?",
        (shop_id, day),
    ) as cur:
        expense, orders, model_orders, count = await cur.fetchone()

    rows = daily_payload
    if isinstance(daily_payload, dict):
        rows = daily_payload.get("rows") or daily_payload.get("result") or []
    if not count or not rows:
        verdict.unknown.append(
            f"{day}: сверять нечего (строк в ряде {count or 0}, строк в daily {len(rows or [])})")
        return verdict

    verdict.checked = 1
    daily_expense = sum(
        numbers.parse_number(row.get("moneySpent") or row.get("expense") or 0) or 0.0
        for row in rows)
    daily_orders = sum(
        int(numbers.parse_number(row.get("orders") or 0) or 0) for row in rows)

    delta = round((expense or 0.0) - daily_expense, 2)
    if abs(delta) > tolerance:
        verdict.alerts.append(
            f"{day}: расход по SKU {round(expense or 0.0, 2)}, по daily "
            f"{round(daily_expense, 2)}, расхождение {delta}")

    combined = (orders or 0) + (model_orders or 0)
    if combined != daily_orders:
        verdict.alerts.append(
            f"{day}: daily.orders={daily_orders}, а orders+modelOrders={combined} — "
            "семантика заказов изменилась")

    verdict.detail.update({
        "расход по SKU": round(expense or 0.0, 2),
        "расход по daily": round(daily_expense, 2),
        "расхождение": delta,
        "orders+modelOrders": combined,
        "daily.orders": daily_orders,
    })
    return verdict
