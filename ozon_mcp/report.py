"""Сборка ежедневного отчёта: ДРР по товарам, доля рекламных заказов, аномалии.

🔴 **Правило ДРР живёт здесь, а не в промпте модели.** Промпт можно переформулировать
незаметно, и проверить его нечем; правило в коде проверяется тестом.

**Что входит в ДРР.** Весь **управляемый** рекламный расход: CPC (трафареты) и CPO
(«оплата за заказ» по выбранным товарам). Исключается ровно одна сущность — режим
«Оплата за заказ: **все** товары»: там тариф 5 %, ставка одна на кабинет, по товарам не
управляется, и рычагом агента она не является. Её расход показывается **отдельной
строкой**, а не выбрасывается.

Обоснование, почему граница именно здесь. Сложив тариф с управляемой рекламой, агент
получит завышенный ДРР и начнёт резать работающие кампании. Обратная ошибка так же
дорога: выбросив из ДРР управляемый CPO, агент на этапе 2 двигает ставку, эффект которой
не измеряет.

⚠️ **Классификация кампаний обязана прийти снаружи.** Если её нет — отчёт не считает ДРР
и говорит об этом. Молчаливое «посчитали по всему расходу» дало бы правдоподобное и
завышенное число: в кабинете 1702 кампании, и какие из них тариф, по самому ряду не видно.

**Замер 21.09.2026, полный обход кабинета — 1702 кампании:**

===================  ========================  ======
`advObjectType`      `PaymentType`             сколько
===================  ========================  ======
REF_VK               CAMPAIGN_TYPE_INVALID       1284
SKU                  CPC                          250
REF_BLOGGER          CAMPAIGN_TYPE_INVALID        161
SEARCH_PROMO         CPO                            4
BANNER               CPM / CPC                      2
ALL_SKU_PROMO        CPO                            1
===================  ========================  ======

Отсюда и опора на `advObjectType`. ⚠️ Первая редакция различала режим «все товары» по
словам в заголовке — и совпала случайно: настоящее название «Оплата за заказ **-** все
товары», через дефис. У Ozon для него есть отдельный тип, и эвристика не нужна.

Реферальные кампании (их 85 % кабинета) приходят с `PaymentType = CAMPAIGN_TYPE_INVALID`,
то есть по оплате не различаются вовсе. Их расход в ДРР по товарам не входит: это другой
продукт, а не рычаг ставки. Показывается отдельной строкой, как и медийный.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Виды расхода. Первые два — управляемые по товарам, остальные в ДРР не входят.
MANAGED_CPC = "cpc"                  # трафареты, advObjectType = SKU
MANAGED_CPO = "cpo_selected"         # оплата за заказ по выбранным, SEARCH_PROMO
EXCLUDED_ALL_SKU = "cpo_all_sku"     # тариф 5%, ALL_SKU_PROMO
EXCLUDED_REFERRAL = "referral"       # REF_VK и REF_BLOGGER
EXCLUDED_BANNER = "banner"           # медийная реклама, не по товарам
UNKNOWN_KIND = "unknown"

MANAGED = frozenset({MANAGED_CPC, MANAGED_CPO})

#: Вид кампании берётся из `advObjectType` — СТРУКТУРНОГО поля, а не из заголовка.
#:
#: ⚠️ Первая редакция различала режим «все товары» по словам в названии. Замер 21.09.2026
#: показал, что у Ozon есть отдельный тип `ALL_SKU_PROMO`, а название у этой кампании —
#: «Оплата за заказ - все товары», через дефис. Эвристика совпала случайно и развалилась
#: бы от смены знака препинания.
#:
#: Полный замер кабинета (1702 кампании): REF_VK 1284, SKU 250, REF_BLOGGER 161,
#: SEARCH_PROMO 4, BANNER 2, ALL_SKU_PROMO 1.
BY_OBJECT_TYPE: dict[str, str] = {
    "SKU": MANAGED_CPC,
    "SEARCH_PROMO": MANAGED_CPO,
    "ALL_SKU_PROMO": EXCLUDED_ALL_SKU,
    "REF_VK": EXCLUDED_REFERRAL,
    "REF_BLOGGER": EXCLUDED_REFERRAL,
    "BANNER": EXCLUDED_BANNER,
    "VIDEO_BANNER": EXCLUDED_BANNER,
}

#: Как называется каждый вид в отчёте. Строка в отчёте — тоже интерфейс.
KIND_TITLES: dict[str, str] = {
    EXCLUDED_ALL_SKU: "расход «оплата за заказ: все товары»",
    EXCLUDED_REFERRAL: "расход реферальный (блогеры, ВК)",
    EXCLUDED_BANNER: "расход медийный (баннеры)",
    UNKNOWN_KIND: "расход неклассифицированный",
}


def classify_campaign(campaign: dict) -> str:
    """Определить вид расхода кампании по её карточке.

    Опора — `advObjectType`. Реферальные кампании приходят с
    `PaymentType = CAMPAIGN_TYPE_INVALID`, то есть по оплате их не различить вовсе:
    структурное поле здесь единственный надёжный признак.
    """
    object_type = str(campaign.get("advObjectType") or "").upper()
    kind = BY_OBJECT_TYPE.get(object_type)
    if kind is not None:
        return kind
    # Незнакомый тип не угадывается по оплате: отнести его к управляемым значит
    # рискнуть завышенным ДРР, к исключаемым — заниженным. Оба риска тихие.
    return UNKNOWN_KIND


def campaign_kinds(campaigns: list[dict]) -> dict[int, str]:
    """`id кампании → вид расхода` для всего кабинета."""
    out: dict[int, str] = {}
    for campaign in campaigns:
        raw = campaign.get("id")
        if raw in (None, ""):
            continue
        out[int(raw)] = classify_campaign(campaign)
    return out


@dataclass
class Report:
    """Отчёт за период. Числа и причины, а не текст."""

    period: dict[str, str]
    coverage: dict[str, Any]
    drr: float | None = None
    managed_expense: float = 0.0
    sales: float = 0.0
    #: Расход по каждому НЕуправляемому виду. Словарь, а не пара счётчиков: видов
    #: четыре, и сваливать их в один «прочий» значило бы снова терять причину.
    excluded: dict[str, float] = field(default_factory=dict)
    ad_orders: int = 0
    total_orders: int | None = None
    ad_orders_share: float | None = None
    by_sku: list[dict[str, Any]] = field(default_factory=list)
    anomalies: dict[str, list[Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "период": self.period,
            "coverage": self.coverage,
            "ДРР": self.drr,
            "управляемый расход": round(self.managed_expense, 2),
            "выручка": round(self.sales, 2),
            # Все известные виды печатаются ВСЕГДА, включая нулевые: отсутствие
            # строки «тариф» неотличимо от «тариф рассмотрели, и он ноль», а это
            # разные утверждения.
            **{title: round(self.excluded.get(kind, 0.0), 2)
               for kind, title in KIND_TITLES.items()},
            "рекламных заказов": self.ad_orders,
            "всего заказов": self.total_orders,
            "доля рекламных заказов": self.ad_orders_share,
            "по товарам": self.by_sku,
            "аномалии": self.anomalies,
            "замечания": self.notes,
        }


def build(
    *,
    period: dict[str, str],
    coverage: dict[str, Any],
    ad_rows: list[dict[str, Any]],
    kinds: dict[int, str] | None,
    total_orders: dict[tuple[int, str], int] | None = None,
    stock: dict[int, int] | None = None,
    names: dict[int, str] | None = None,
    top: int = 20,
) -> Report:
    """Собрать отчёт из накопленного ряда.

    `ad_rows` — строки `ad_daily` в разрезе `sku × день × кампания`. `kinds` —
    классификация кампаний; `None` означает «неизвестна», и тогда ДРР не считается.

    `names` — названия товаров по `sku`. `None` значит «не запрашивали», и тогда поля
    «товар» в отчёте не будет вовсе: пустое поле читалось бы как «у товара нет имени».
    Переданный словарь, в котором части `sku` не хватает, — другое утверждение, и оно
    попадает в замечания: каталог отстал от рекламы либо товар рекламируется без
    карточки. Список из одних чисел обсуждать с тем, кто ведёт ассортимент, нельзя.
    """
    report = Report(period=period, coverage=coverage)

    if kinds is None:
        report.notes.append(
            "Классификация кампаний недоступна — ДРР не посчитан. Считать его по всему "
            "расходу нельзя: в него вошёл бы тариф «оплата за заказ: все товары», и "
            "ДРР оказался бы завышенным, оставаясь правдоподобным."
        )

    per_sku: dict[int, dict[str, float]] = {}
    for row in ad_rows:
        campaign = row.get("campaign_id")
        kind = (kinds or {}).get(int(campaign)) if campaign is not None else None
        if kinds is not None and kind is None:
            kind = UNKNOWN_KIND
        expense = float(row.get("expense") or 0.0)
        orders = int(row.get("orders") or 0) + int(row.get("model_orders") or 0)
        sales = float(row.get("sales") or 0.0)

        if kind not in MANAGED:
            # Всё, что не управляется по товарам, складывается ОТДЕЛЬНО по своему виду:
            # тариф, реферальные, медийные, неопознанные. Ни один из них не входит в
            # ДРР и ни один не выбрасывается — иначе сумма частей перестанет сходиться
            # с расходом ряда, и заметить это будет нечем.
            report.excluded[kind or UNKNOWN_KIND] = (
                report.excluded.get(kind or UNKNOWN_KIND, 0.0) + expense)
            continue

        report.managed_expense += expense
        report.sales += sales
        report.ad_orders += orders
        sku = row.get("sku")
        if sku is None:
            continue
        bucket = per_sku.setdefault(int(sku), {"expense": 0.0, "sales": 0.0, "orders": 0})
        bucket["expense"] += expense
        bucket["sales"] += sales
        bucket["orders"] += orders

    unknown = report.excluded.get(UNKNOWN_KIND, 0.0)
    if unknown:
        report.notes.append(
            f"Расход {round(unknown, 2)} ₽ пришёлся на кампании, тип которых нам "
            "незнаком. Он НЕ включён в ДРР и не выброшен — показан отдельно, потому "
            "что отнести его наугад значит сдвинуть ДРР в неизвестную сторону."
        )

    if kinds is not None and report.sales > 0:
        report.drr = round(report.managed_expense / report.sales, 4)

    report.by_sku = sorted(
        (
            {
                "sku": sku,
                **_named(sku, names),
                "expense": round(values["expense"], 2),
                "sales": round(values["sales"], 2),
                "orders": int(values["orders"]),
                "ДРР": (round(values["expense"] / values["sales"], 4)
                        if values["sales"] > 0 else None),
            }
            for sku, values in per_sku.items()
        ),
        key=lambda item: item["expense"], reverse=True,
    )[:top]

    if names is not None:
        unnamed = sorted(sku for sku in per_sku if sku not in names)
        if unnamed:
            report.notes.append(
                f"Названия не нашлись у {len(unnamed)} товаров с расходом "
                f"({', '.join(str(s) for s in unnamed[:10])}"
                f"{'…' if len(unnamed) > 10 else ''}). Это значит либо что каталог не "
                "пересобран после появления товара, либо что sku рекламируется без "
                "карточки. В отчёте у них стоит null, а не прочерк: прочерк выглядел "
                "бы как товар без имени."
            )

    if total_orders is not None:
        report.total_orders = sum(total_orders.values())
        if report.total_orders > 0:
            report.ad_orders_share = round(report.ad_orders / report.total_orders, 4)
        else:
            report.notes.append(
                "Общих заказов за период ноль — доля рекламных не считается. Ноль в "
                "знаменателе это не «ноль процентов», а «не из чего считать»."
            )

    report.anomalies = _anomalies(per_sku, coverage, stock, names)
    return report


def _named(sku: int, names: dict[int, str] | None) -> dict[str, Any]:
    """Поле «товар» — или его отсутствие, если названий не запрашивали.

    Три разных утверждения не сводятся к одному: «не спрашивали» (ключа нет),
    «спросили, не нашли» (`null`) и «вот название». Прочерк вместо `null` стёр бы
    границу между вторым и третьим.
    """
    if names is None:
        return {}
    return {"товар": names.get(int(sku))}


def _anomalies(
    per_sku: dict[int, dict[str, float]],
    coverage: dict[str, Any],
    stock: dict[int, int] | None,
    names: dict[int, str] | None = None,
) -> dict[str, list[Any]]:
    """Три аномалии, названные в спецификации."""
    spend_without_orders = sorted(
        ({"sku": sku, **_named(sku, names), "expense": round(v["expense"], 2)}
         for sku, v in per_sku.items() if v["expense"] > 0 and v["orders"] == 0),
        key=lambda item: item["expense"], reverse=True,
    )

    if stock is None:
        orders_without_stock: list[Any] = []
        stock_note = ["остатки не переданы — проверка не выполнена"]
    else:
        orders_without_stock = sorted(
            (
                {"sku": sku, **_named(sku, names), "orders": int(values["orders"])}
                for sku, values in per_sku.items()
                if values["orders"] > 0 and not stock.get(sku)
            ),
            key=lambda item: item["orders"], reverse=True,
        )
        stock_note = []

    broken = list(coverage.get("сбора не было") or []) + \
        list(coverage.get("сбор провалился") or []) + \
        list(coverage.get("сбор не завершён") or [])

    out: dict[str, list[Any]] = {
        "расход без заказов": spend_without_orders,
        "заказы без остатка": orders_without_stock,
        "обрыв ряда": sorted(broken),
    }
    if stock_note:
        out["заказы без остатка — НЕ ПРОВЕРЕНО"] = stock_note
    return out
