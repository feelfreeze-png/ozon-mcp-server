"""Привязка клиента к его магазинам личным bearer-токеном.

Штатная схема сервера — один общий `MCP_AUTH_TOKEN` на всех, а магазин выбирается
аргументом `shop_id`. В мультиарендной установке это означает, что клиент, узнавший
чужой `shop_id`, тратит чужой рекламный бюджет: аргумент приходит от модели, а не от
инфраструктуры, и никакая проверка «а свой ли это магазин» на него не опирается.

Здесь токен перестаёт быть пропуском и становится удостоверением: каждому клиенту
выдаётся свой, токен сопоставлен его магазинам, и чужой `shop_id` до вызова не
доходит. Разница принципиальная: проверку модель может обойти подбором, границу
инфраструктуры — нет, потому что аргумент либо перестаёт на что-либо влиять, либо
отвергается вместе со всем вызовом.

Включается переменной `MCP_CLIENT_TOKENS`:

    MCP_CLIENT_TOKENS=<токен1>:<shop_id1>,<токен2>:<shop_id2>|<shop_id3>

Магазины одного токена перечисляются через `|`; записи разделяются `,` или `;`.
Пока переменная пуста, модуль не меняет ничего и сервер ведёт себя как оригинальный.

🔴 **Один магазин и несколько — это два разных режима, и разница не косметическая.**

*Один* (`токен:shop`) — `shop_id` **подставляется** молча, каким бы ни пришёл.
Выбора у клиента нет, аргумент не несёт информации, и подменить его нечем: ответ
всегда про единственный доступный магазин. Это поведение проверено и не менялось.

*Несколько* (`токен:shop1|shop2`) — выбор появляется, и молчаливая подстановка
становится опасной ровно настолько, насколько была безопасна раньше. Спросили про
`shop2`, подставили `shop1` — вернутся достоверно выглядящие числа не того магазина,
без единого признака подмены. Поэтому:

- `shop_id` не назван        → берётся первый в списке (он и есть «по умолчанию»);
- назван и входит в список   → исполняется как есть;
- назван и в список НЕ входит → `ForeignShopError`, весь вызов отвергается.

Отказ здесь дешевле подстановки: неверный ответ, похожий на верный, — самый дорогой
класс дефектов в этом проекте, а явный отказ модель видит и может назвать причину.

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
from collections.abc import Sequence

# Разделитель магазинов внутри одной записи. Запятая и точка с запятой уже заняты
# под разделение записей, поэтому взят символ, которого в shop_id быть не может.
SHOP_SEPARATOR = "|"


class ForeignShopError(Exception):
    """Запрошен магазин, которого нет у этого токена.

    Поднимается только в режиме нескольких магазинов: при одном подставляется
    единственный доступный и отвергать нечего.
    """

    def __init__(self, requested: str, allowed: tuple[str, ...]) -> None:
        self.requested = requested
        self.allowed = allowed
        super().__init__(
            f"Магазин {requested!r} не доступен этому токену. "
            f"Доступны: {', '.join(allowed)}."
        )


# Магазины, к которым привязана текущая MCP-сессия; порядок значим — первый
# считается магазином по умолчанию.
#
# Ставится в обработчике GET /sse ДО запуска цикла `mcp_app.run(...)`, а вызовы
# инструментов исполняются внутри этого цикла — в той же задаче. Дочерние задачи
# anyio копируют контекст в момент старта, поэтому привязка достаётся им сама.
# POST /messages лишь передаёт сообщение в поток сессии и своего контекста не несёт.
_PINNED_SHOPS: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "ozon_pinned_shops", default=None)


def _normalise(shops: str | Sequence[str] | None) -> tuple[str, ...] | None:
    """Привести привязку к кортежу без пустых значений и без повторов.

    Повторы снимаются с сохранением порядка: `shop|shop` — это один магазин,
    а не два, и «по умолчанию» у него тот же. Пустой результат — это отсутствие
    привязки, а не привязка к пустому множеству: иначе токен с опечаткой в
    значении открыл бы сессию, в которой недоступно вообще ничего, и причина
    была бы не видна ни в одном ответе.
    """
    if shops is None:
        return None
    if isinstance(shops, str):
        shops = [shops]
    seen: list[str] = []
    for shop in shops:
        shop = (shop or "").strip()
        if shop and shop not in seen:
            seen.append(shop)
    return tuple(seen) or None


def client_tokens() -> dict[str, tuple[str, ...]]:
    """`{токен: (shop_id, ...)}` из `MCP_CLIENT_TOKENS`. Пустой словарь — режим выключен.

    Читается при каждом обращении, а не на импорте: так переменную видно из тестов
    и из перезапуска без пересборки образа.
    """
    raw = (os.environ.get("MCP_CLIENT_TOKENS") or "").strip()
    if not raw:
        return {}
    pairs: dict[str, tuple[str, ...]] = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        token, _, shop_ids = chunk.partition(":")
        token = token.strip()
        shops = _normalise(shop_ids.split(SHOP_SEPARATOR))
        if token and shops:
            pairs[token] = shops
    return pairs


def is_enabled() -> bool:
    """Задан ли хоть один клиентский токен."""
    return bool(client_tokens())


def resolve(token: str) -> tuple[str, ...] | None:
    """Магазины по токену, иначе None.

    Перебираются все записи без раннего выхода: время ответа не должно зависеть от
    того, сколько первых символов токена угаданы.
    """
    if not token:
        return None
    found: tuple[str, ...] | None = None
    probe = token.encode("utf-8", "surrogatepass")
    for known, shops in client_tokens().items():
        if secrets.compare_digest(probe, known.encode("utf-8", "surrogatepass")):
            found = shops
    return found


def pin(shops: str | Sequence[str] | None):
    """Привязать сессию к магазинам. Возвращает токен для `unpin`."""
    return _PINNED_SHOPS.set(_normalise(shops))


def unpin(token) -> None:
    """Снять привязку, поставленную `pin`."""
    _PINNED_SHOPS.reset(token)


def pinned() -> tuple[str, ...] | None:
    """Магазины текущей сессии или None, если режим выключен."""
    return _PINNED_SHOPS.get()


def pinned_one() -> str | None:
    """Магазин сессии, если он ровно один; иначе None.

    Отдельная функция, потому что «привязка есть» и «выбора нет» — разные вопросы,
    а раньше они совпадали. Схемы инструментов прячут `shop_id` по второму из них:
    при нескольких магазинах параметр модели нужен.
    """
    shops = pinned()
    return shops[0] if shops and len(shops) == 1 else None


def enforce(arguments: dict) -> dict:
    """Привести `shop_id` к границе токена. Правила — в докстринге модуля.

    Без привязки словарь возвращается как есть — оригинальное поведение.
    Исходный словарь не мутируется: он же уходит в `_CALL_CONTEXT` и в статистику.
    """
    shops = pinned()
    if shops is None:
        return arguments
    requested = arguments.get("shop_id")
    if len(shops) == 1:
        # Единственный магазин: аргумент не несёт информации, подставляем молча.
        return arguments if requested == shops[0] else {**arguments, "shop_id": shops[0]}
    if requested is None or requested == "":
        return {**arguments, "shop_id": shops[0]}
    if requested in shops:
        return arguments
    raise ForeignShopError(str(requested), shops)
