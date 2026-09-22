"""Рекомендации по рекламному кабинету: предложенные действия, а не наблюдения.

**Зачем отдельный модуль.** Отчёт описывает: «176 товаров потратили деньги и не дали
заказов». С наблюдением нельзя согласиться или не согласиться — его можно только
проверить. Рекомендация — другое: **объект, действие, величина и основание**, по которым
владелец говорит «да» или «нет». Ворота 1 → 2 в прежней редакции требовали «владелец
согласен с рекомендациями» и были невыполнимы ровно потому, что рекомендаций никто не
порождал.

🔴 **Одного дня не хватает, и это замерено, а не осторожность.** 20.09.2026 sku 922567890
(«Пищевое ведро 8 л») было самой дорогой пустой строкой дня: 1 464,99 ₽ расхода, ноль
заказов. 21.09 тот же товар дал заказ на 6 479 ₽ с ДРР 8,24 %. **Один заказ перевёл его
из худших в лучшие.** Рекомендация «снять с продвижения», выданная 21-го, была бы
ошибкой — поэтому окно короче `MIN_DAYS` даёт не слабую рекомендацию, а отказ.

🔴 **Неполное покрытие ряда запрещает рекомендацию.** Сумма расхода за период с
несобранным днём меньше настоящей, и по самому числу этого не видно. Товар, у которого
«за неделю 300 ₽ и ноль заказов», при двух несобранных днях мог потратить вдвое больше —
или получить заказ именно в пропущенный день. Поэтому дыра в покрытии — отказ, а не
сноска.

**Что модуль НЕ делает.** Не меняет ничего в Ozon и не знает, как это делается: он
возвращает предложения. Исполнение — этап 2, поштучно и с записью вердикта в `action_log`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Сколько дней ряда нужно, чтобы вообще что-то советовать.
#:
#: Неделя выбрана не круглостью: замер 20-21.09.2026 показал, что сутки переворачивают
#: вывод по отдельному товару целиком. Недельное окно сглаживает единичный заказ, но
#: остаётся достаточно коротким, чтобы реагировать на изменения ассортимента.
MIN_DAYS = 7

#: Виды рекомендаций. Строкой, а не перечислением: вид попадает в журнал и в текст
#: владельцу, и читаться он должен без словаря.
STOP_NO_STOCK = "снять с продвижения: нет остатка"
FIX_CARD = "разобрать карточку: остаток есть, заказов нет"
RESTOCK = "пополнить остаток: заказы есть, остатка нет"

#: Порядок важности. Первое — самое дорогое из найденного на этом кабинете: 62 %
#: дневного бюджета 20.09 ушло на товары, которых не было на складе.
KIND_ORDER = (STOP_NO_STOCK, RESTOCK, FIX_CARD)


class AdviceRefused(ValueError):
    """Рекомендации не строятся, и причина названа."""


@dataclass(frozen=True)
class Advice:
    """Одно предложение. Всё, по чему владелец принимает решение, — внутри."""

    kind: str
    sku: int
    product: str | None
    action: str
    #: Числа, на которых стоит предложение. Владелец спорит с ними, а не с тоном.
    grounds: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "вид": self.kind,
            "sku": self.sku,
            "товар": self.product,
            "предлагается": self.action,
            "основание": self.grounds,
        }


@dataclass
class AdviceSet:
    """Итог прогона: предложения и то, почему остальные не построены."""

    window: dict[str, Any]
    items: list[Advice] = field(default_factory=list)
    #: Почему по товару рекомендации нет. Пустота здесь читалась бы как «всё хорошо».
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "окно": self.window,
            "рекомендаций": len(self.items),
            "по видам": {kind: sum(1 for item in self.items if item.kind == kind)
                         for kind in KIND_ORDER},
            "список": [item.as_dict() for item in self.items],
            "не предложено": self.skipped,
            "замечания": self.notes,
        }


def _order(item: Advice) -> tuple:
    return (KIND_ORDER.index(item.kind), -float(item.grounds.get("расход", 0) or 0))


def build(
    *,
    window: dict[str, Any],
    coverage: dict[str, Any],
    per_sku: dict[int, dict[str, Any]],
    stock: dict[int, int] | None,
    names: dict[int, str] | None = None,
    min_expense: float = 0.0,
    top: int = 50,
) -> AdviceSet:
    """Построить рекомендации по окну ряда.

    `per_sku` — агрегат управляемого расхода за окно: `{sku: {expense, orders, sales}}`.
    Неуправляемые кампании (тариф, реферальные, медийные) сюда попадать не должны: ставку
    по ним агент не двигает, и советовать про них нечего.

    `stock` — остаток на последний день окна: `None` означает «снимка нет». Разница
    несущая: на `None` рекомендации про остаток не строятся вовсе, потому что «остатка
    нет» и «мы не смотрели» — разные утверждения, и второе выглядит как первое.

    `min_expense` — порог, ниже которого предложение не стоит внимания владельца.
    Ноль означает «показывать всё».
    """
    result = AdviceSet(window=dict(window))
    names = names or {}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    days = int(coverage.get("собрано") or 0)
    if days < MIN_DAYS:
        raise AdviceRefused(
            f"в окне собрано {days} дней из нужных {MIN_DAYS}. Рекомендации не строятся: "
            f"замерено, что сутки переворачивают вывод по товару целиком — 20.09.2026 "
            f"sku 922567890 был самой дорогой строкой без заказов, а 21.09 дал заказ на "
            f"6 479 ₽. Короткое окно даёт не осторожный совет, а неверный."
        )

    broken = (list(coverage.get("сбор провалился") or [])
              + list(coverage.get("сбор не завершён") or [])
              + list(coverage.get("сбора не было") or []))
    if broken:
        raise AdviceRefused(
            f"в окне {len(broken)} несобранных дней ({', '.join(sorted(broken)[:5])}"
            f"{'…' if len(broken) > 5 else ''}). Рекомендации не строятся: сумма расхода "
            f"за период с пропуском меньше настоящей, и по самому числу этого не видно — "
            f"товар мог и потратить больше, и получить заказ именно в пропущенный день."
        )

    if stock is None:
        result.notes.append(
            "Снимка остатков за последний день окна нет, поэтому рекомендации про "
            "остаток не строятся вовсе. Это не «остатка нет» — это «не смотрели»."
        )

    for sku, values in per_sku.items():
        expense = float(values.get("expense") or 0.0)
        orders = int(values.get("orders") or 0)
        if expense <= 0:
            skip("расхода в окне не было")
            continue
        if expense < min_expense:
            skip(f"расход ниже порога {min_expense:g} ₽")
            continue

        on_hand = None if stock is None else int(stock.get(sku, 0))
        grounds = {
            "расход": round(expense, 2),
            "заказов": orders,
            "выручка": round(float(values.get("sales") or 0.0), 2),
            "дней в окне": days,
        }
        if on_hand is not None:
            grounds["остаток"] = on_hand

        if orders == 0 and on_hand == 0:
            result.items.append(Advice(
                kind=STOP_NO_STOCK, sku=sku, product=names.get(sku),
                action=("снять с продвижения или пополнить склад — сейчас реклама "
                        "ведёт на товар, которого нельзя купить"),
                grounds=grounds))
        elif orders == 0 and on_hand:
            result.items.append(Advice(
                kind=FIX_CARD, sku=sku, product=names.get(sku),
                action=("разобрать карточку, цену или соответствие запросу — товар в "
                        "наличии, показы оплачены, заказов нет"),
                grounds=grounds))
        elif orders > 0 and on_hand == 0:
            result.items.append(Advice(
                kind=RESTOCK, sku=sku, product=names.get(sku),
                action="пополнить остаток — заказы идут, а на складе пусто",
                grounds=grounds))
        elif on_hand is None:
            skip("остаток не проверен — снимка за день нет")
        else:
            skip("заказы идут, остаток есть")

    result.items.sort(key=_order)
    if len(result.items) > top:
        result.notes.append(
            f"Показаны {top} предложений из {len(result.items)} — остальные дешевле. "
            f"Отсечка названа здесь, чтобы её не приняли за полный список."
        )
        result.items = result.items[:top]
    result.skipped = dict(sorted(skipped.items()))
    return result
