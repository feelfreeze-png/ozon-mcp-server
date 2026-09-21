"""Режим только для чтения: запретить инструменты, меняющие рекламу.

Включается `OZON_READONLY=1`. Нужен там, где ассистенту дают боевой рекламный
кабинет, но ещё не готовы доверить ему распоряжаться деньгами: ошибка модели в
ставке или бюджете тратит их немедленно и откатывается только руками.

**Список явный, а не по шаблону имени.** Шаблон вида «`_set`/`_update`/`_create` —
значит запись» пропустил бы `ozon_ad_campaign_bids`: имя выглядит как геттер, а
инструмент обновляет ставки товаров в кампании. Одного такого промаха достаточно,
чтобы режим создавал ложное чувство безопасности, — поэтому каждое имя выписано
руками и закреплено тестом.

**Что режим НЕ запрещает: генерацию отчётов** (`ozon_report_*_create`). Она создаёт
задание на стороне Ozon, то есть формально пишет, но рекламного состояния не меняет
и денег не тратит, а без неё аналитика теряет половину смысла. Граница проведена по
«может ли это стоить денег», а не по HTTP-методу.
"""

from __future__ import annotations

import os

# Реклама. Эти девять закрывались первыми — режим заводился ради них.
_ADS = {
    "ozon_ad_campaign_activate",        # запуск кампании
    "ozon_ad_campaign_bids",            # ⚠️ ОБНОВЛЯЕТ ставки, хотя имя звучит как чтение
    "ozon_ad_campaign_budget_update",   # смена бюджета и периода
    "ozon_ad_campaign_create",          # создание кампании
    "ozon_ad_campaign_stop",            # остановка кампании
    "ozon_ad_products_add",             # добавление товаров в кампанию
    "ozon_ad_products_delete",          # удаление товаров из кампании
    "ozon_search_promo_disable",        # выключение «оплаты за заказ»
    "ozon_search_promo_enable",         # включение «оплаты за заказ»
}

# Карточки товаров. 🔴 Цена ошибки здесь ВЫШЕ, чем в ставке: ставку можно вернуть,
# удалённую карточку — нет. До этой правки не блокировался ни один из них.
_CATALOG = {
    "ozon_product_archive",             # в архив
    "ozon_product_unarchive",           # из архива
    "ozon_product_attributes_update",   # правка характеристик
    "ozon_product_delete",              # удаление карточки — необратимо
    "ozon_product_import",              # создание и обновление карточек
    "ozon_product_import_by_sku",       # заведение по чужому SKU
    "ozon_product_update_images",       # ⚠️ СТИРАЕТ всё, что не передано
    "ozon_product_update_offer_id",     # смена артикула
    "ozon_product_update_stocks",       # остатки; путь /v2/products/stocks без слова-признака
}

# Цены, акции и скидки — тратят деньги напрямую.
_PRICING = {
    "ozon_set_prices",                  # установка цен
    "ozon_min_price_timer_renew",       # продление таймера минимальной цены
    "ozon_pricing_strategy_create",
    "ozon_pricing_strategy_delete",
    "ozon_pricing_strategy_products",   # добавление и удаление товаров в стратегии
    "ozon_pricing_strategy_update",
    "ozon_actions_activate",            # участие в акции Ozon
    "ozon_actions_deactivate",
    "ozon_action_auto_add_delete",      # снятие автодобавления
    "ozon_discount_approve",            # согласие на скидку покупателю
    "ozon_discount_decline",            # путь /decline — слова-признака в шаблоне нет
    "ozon_seller_action_create",        # собственная акция продавца
    "ozon_seller_action_products_add",
    "ozon_seller_action_products_delete",
    "ozon_seller_action_toggle",        # /change-activity — тоже мимо шаблона
}

# Заказы, отгрузки и возвраты. Отменённый заказ и отгруженная посылка обращением
# к API не отыгрываются.
_ORDERS = {
    "ozon_order_fbs_act_create",        # формирование акта
    "ozon_order_fbs_cancel",            # отмена отправления
    "ozon_order_fbs_country_set",       # страна-изготовитель в отправлении
    "ozon_order_fbs_ship",              # отгрузка
    "ozon_cancellation_approve",        # согласие на отмену покупателем
    "ozon_cancellation_reject",
    "ozon_carriage_create",             # создание перевозки
    "ozon_carriage_approve",            # подтверждение перевозки
    "ozon_returns_fbs_approve",
    "ozon_returns_fbs_reject",
    "ozon_returns_rfbs_action",         # общий путь /v2/returns/rfbs/{action}
}

# Общение с покупателем. Отправленное сообщение и опубликованный ответ видны
# постороннему человеку немедленно, и отзыв возможен не всегда.
_COMMUNICATION = {
    "ozon_chat_start",                  # начать чат с покупателем
    "ozon_chat_send",                   # ⚠️ отправка сообщения; мимо шаблона имени
    "ozon_chat_send_file",              # ⚠️ отправка файла; мимо шаблона имени
    "ozon_chat_read",                   # отметка о прочтении — состояние на стороне Ozon
    "ozon_question_reply",              # публичный ответ на вопрос
    "ozon_review_reply",                # публичный ответ на отзыв
    "ozon_review_reply_delete",         # удаление ответа
}

WRITE_TOOLS: frozenset[str] = frozenset(
    _ADS | _CATALOG | _PRICING | _ORDERS | _COMMUNICATION
)

# Пути, которые выглядят пишущими по имени, но читают. Держатся списком, потому что
# сторож выводит пишущих из исходника и без этого списка ругался бы на них.
READ_PATHS_THAT_LOOK_LIKE_WRITES: frozenset[str] = frozenset({
    "ozon_order_fbs_cancel_reasons",    # /v2/posting/fbs/cancel-reason/list
    "ozon_product_import_info",         # /v1/product/import/info — статус задания
    "ozon_review_comments",             # /v1/review/comment/list
})

# Генерация отчётов. Формально пишет — создаёт задание на стороне Ozon, — но ни
# рекламного, ни товарного состояния не меняет и денег не тратит. Граница проведена
# по «может ли это стоить денег», а не по HTTP-методу.
REPORT_TOOLS: frozenset[str] = frozenset({
    "ozon_report_discounted_create",
    "ozon_report_products_create",
    "ozon_report_stocks_create",
    "ozon_returns_report",
})

_TRUE = frozenset({"1", "true", "yes", "on", "да"})


def is_enabled() -> bool:
    """Включён ли режим только для чтения."""
    return (os.environ.get("OZON_READONLY") or "").strip().lower() in _TRUE


def is_blocked(name: str) -> bool:
    """Запрещён ли инструмент в текущем режиме."""
    return is_enabled() and name in WRITE_TOOLS


def refusal_message(name: str) -> str:
    """Отказ должен называть причину, иначе модель решит, что возможности нет вовсе.

    Разница существенная: «выключено политикой» пользователь может попросить изменить,
    а «такого не умеем» закрывает разговор.
    """
    return (
        f"Инструмент {name} выключен: сервер работает в режиме только для чтения "
        f"(OZON_READONLY). Недоступно всё, что меняет состояние кабинета: ставки и "
        f"бюджеты, карточки товаров и остатки, цены и участие в акциях, отгрузки и "
        f"возвраты, сообщения покупателю. Чтение — статистика, ставки, остатки, "
        f"карточки, зоны размещения, отчёты — работает как обычно."
    )


#: Из чего выводится вид объекта для журнала действий. Первое совпадение выигрывает.
_OBJECT_KINDS: tuple[tuple[str, str], ...] = (
    ("ozon_ad_campaign", "campaign"),
    ("ozon_ad_products", "campaign_products"),
    ("ozon_search_promo", "search_promo"),
    ("ozon_product", "product"),
    ("ozon_set_prices", "price"),
    ("ozon_pricing", "pricing_strategy"),
    ("ozon_min_price", "price"),
    ("ozon_action", "action"),
    ("ozon_seller_action", "seller_action"),
    ("ozon_discount", "discount"),
    ("ozon_order", "posting"),
    ("ozon_cancellation", "cancellation"),
    ("ozon_carriage", "carriage"),
    ("ozon_returns", "return"),
    ("ozon_chat", "chat"),
    ("ozon_question", "question"),
    ("ozon_review", "review"),
)

#: Имена аргументов, по которым опознаётся объект действия.
_ID_ARGS = ("campaign_id", "product_id", "offer_id", "sku", "skus", "posting_number",
            "strategy_id", "action_id", "review_id", "question_id", "chat_id")


def object_kind(name: str) -> str:
    for prefix, kind in _OBJECT_KINDS:
        if name.startswith(prefix):
            return kind
    return "tool"


def object_id(arguments: dict) -> str:
    """Что именно пытались изменить. Пусто — значит не опознали, и так и написано."""
    for key in _ID_ARGS:
        value = arguments.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            head = ", ".join(str(item) for item in value[:5])
            return f"{head}…" if len(value) > 5 else head
        return str(value)
    return ""


def attempted_change(arguments: dict) -> str:
    """Что было бы применено. Без `shop_id`: он подставляется нами, а не моделью."""
    import json as _json

    payload = {key: value for key, value in arguments.items()
               if key not in ("shop_id", "view")}
    return _json.dumps(payload, ensure_ascii=False, default=str)[:1000]
