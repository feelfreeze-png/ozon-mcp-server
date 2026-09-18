"""Привязка клиента к магазину его личным bearer-токеном.

Штатная схема сервера — один общий `MCP_AUTH_TOKEN` на всех, а магазин выбирается
аргументом `shop_id`. В мультиарендной установке это означает, что клиент, узнавший
чужой `shop_id`, тратит чужой рекламный бюджет: аргумент приходит от модели, а не от
инфраструктуры, и никакая проверка «а свой ли это магазин» на него не опирается.

Здесь токен перестаёт быть пропуском и становится удостоверением: каждому клиенту
выдаётся свой, токен жёстко сопоставлен одному `shop_id`, и этот `shop_id`
**подставляется** вместо любого пришедшего в аргументах. Разница принципиальная:
проверку модель может обойти подбором, подстановку — нет, потому что аргумент просто
перестаёт на что-либо влиять.

Включается переменной `MCP_CLIENT_TOKENS`:

    MCP_CLIENT_TOKENS=<токен1>:<shop_id1>,<токен2>:<shop_id2>

Пока она пуста, модуль не меняет ничего и сервер ведёт себя как оригинальный.

Граница, которую этот модуль НЕ закрывает: `session_id`, выданный по успешному
GET /sse, остаётся самостоятельным секретом — POST с чужим валидным `session_id`
исполнится в контексте той сессии. Так устроен транспорт и в оригинале
(см. `_is_live_session` в app.py); 128-битный UUID клиенту-соседу неоткуда взять,
но это допущение, а не доказанное свойство.
"""

from __future__ import annotations

import contextvars
import os
import secrets

# Магазин, к которому привязана текущая MCP-сессия.
#
# Ставится в обработчике GET /sse ДО запуска цикла `mcp_app.run(...)`, а вызовы
# инструментов исполняются внутри этого цикла — в той же задаче. Дочерние задачи
# anyio копируют контекст в момент старта, поэтому привязка достаётся им сама.
# POST /messages лишь передаёт сообщение в поток сессии и своего контекста не несёт.
_PINNED_SHOP: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ozon_pinned_shop", default=None)


def client_tokens() -> dict[str, str]:
    """`{токен: shop_id}` из `MCP_CLIENT_TOKENS`. Пустой словарь — режим выключен.

    Читается при каждом обращении, а не на импорте: так переменную видно из тестов
    и из перезапуска без пересборки образа.
    """
    raw = (os.environ.get("MCP_CLIENT_TOKENS") or "").strip()
    if not raw:
        return {}
    pairs: dict[str, str] = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        token, _, shop_id = chunk.partition(":")
        token, shop_id = token.strip(), shop_id.strip()
        if token and shop_id:
            pairs[token] = shop_id
    return pairs


def is_enabled() -> bool:
    """Задан ли хоть один клиентский токен."""
    return bool(client_tokens())


def resolve(token: str) -> str | None:
    """`shop_id` по токену, иначе None.

    Перебираются все записи без раннего выхода: время ответа не должно зависеть от
    того, сколько первых символов токена угаданы.
    """
    if not token:
        return None
    found: str | None = None
    probe = token.encode("utf-8", "surrogatepass")
    for known, shop_id in client_tokens().items():
        if secrets.compare_digest(probe, known.encode("utf-8", "surrogatepass")):
            found = shop_id
    return found


def pin(shop_id: str | None):
    """Привязать сессию к магазину. Возвращает токен для `unpin`."""
    return _PINNED_SHOP.set(shop_id)


def unpin(token) -> None:
    """Снять привязку, поставленную `pin`."""
    _PINNED_SHOP.reset(token)


def pinned() -> str | None:
    """Магазин текущей сессии или None, если режим выключен."""
    return _PINNED_SHOP.get()


def enforce(arguments: dict) -> dict:
    """Подставить привязанный `shop_id` вместо пришедшего.

    Без привязки словарь возвращается как есть — оригинальное поведение.
    Исходный словарь не мутируется: он же уходит в `_CALL_CONTEXT` и в статистику.
    """
    shop_id = pinned()
    if shop_id is None or arguments.get("shop_id") == shop_id:
        return arguments
    return {**arguments, "shop_id": shop_id}
