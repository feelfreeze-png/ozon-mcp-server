"""Привязка клиента к его магазинам токеном.

Главный тест режима ОДНОГО магазина — `test_foreign_shop_id_is_ignored_not_rejected`:
он проверяет не то, что чужой `shop_id` отвергается, а то, что он **ни на что не
влияет**. Разница существенная. Отказ — это правило, которое кто-то должен не забыть
применить в каждой из 150 веток диспетчера; подстановка работает одна на всех,
и забыть её негде.

Главный тест режима НЕСКОЛЬКИХ — `test_brief_over_several_cabinets_sees_all_of_them`:
он воспроизводит дефект 23.09, когда утренний бриф показал один кабинет из четырёх
и назвал покрытие полным. Там проверяются обе половины отказа: что `ozon_list_shops`
отдаёт все свои магазины и что `shop_id` остаётся в схемах — без второго модель
видит четыре магазина и не может обратиться ни к одному, кроме первого.
"""

import pytest

from ozon_mcp import server, tenancy
from ozon_mcp.server import TOOLS, _call_tool_impl, _visible_tools
from ozon_mcp.settings import save_shops


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Каждый тест стартует с выключенным режимом и полным каталогом."""
    monkeypatch.delenv("MCP_CLIENT_TOKENS", raising=False)
    monkeypatch.delenv("OZON_TOOLSETS", raising=False)
    yield


@pytest.fixture
def pinned_to_alfa():
    """Сессия привязана к «alfa». Магазина с таким id в хранилище нет намеренно."""
    token = tenancy.pin("alfa")
    yield "alfa"
    tenancy.unpin(token)


# ─── Разбор переменной ──────────────────────────────────────

def test_disabled_while_variable_is_empty(monkeypatch):
    assert tenancy.client_tokens() == {}
    assert tenancy.is_enabled() is False
    assert tenancy.pinned() is None


def test_parses_pairs(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1, bbb:shop2 ;ccc:shop3")
    assert tenancy.client_tokens() == {
        "aaa": ("shop1",), "bbb": ("shop2",), "ccc": ("shop3",)}


def test_parses_several_shops_per_token(monkeypatch):
    """Владелец нескольких кабинетов — один клиент, а не несколько токенов."""
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:main|Shop2|shop3,bbb:solo")
    assert tenancy.client_tokens() == {
        "aaa": ("main", "Shop2", "shop3"), "bbb": ("solo",)}


def test_shop_order_is_preserved_and_duplicates_collapse(monkeypatch):
    """Порядок значим (первый — по умолчанию), повтор магазином не становится."""
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa: beta | alfa | beta ")
    assert tenancy.client_tokens() == {"aaa": ("beta", "alfa")}


def test_garbage_entries_are_dropped_not_guessed(monkeypatch):
    """Строка без двоеточия — не «токен без магазина», а мусор: пропускаем."""
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,broken,:no-token,ddd:")
    assert tenancy.client_tokens() == {"aaa": ("shop1",)}


def test_token_with_only_separators_is_dropped_not_opened(monkeypatch):
    """`ddd:||` — опечатка, а не «доступно ничего».

    Пустой кортеж допустить нельзя: `resolve` вернул бы «не None», сессия открылась
    бы, а список магазинов в ней был бы пуст. Клиент увидел бы рабочий сервер без
    единого кабинета и не смог бы назвать причину.
    """
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,ddd:||")
    assert tenancy.client_tokens() == {"aaa": ("shop1",)}
    assert tenancy.resolve("ddd") is None


def test_unparseable_variable_closes_the_door_instead_of_opening_it(monkeypatch):
    """🔴 Блокер: опечатка в переменной не должна открывать сервер настежь.

    Записи отбрасываются поштучно. Если отброшены ВСЕ, словарь пуст — и по
    `is_enabled()` сервер неотличим от «режим выключен». Дальше при пустом
    `MCP_AUTH_TOKEN` он пускает вообще без токена: одна опечатка снимала бы
    и привязку, и авторизацию разом, а снаружи это выглядит как «всё работает».
    """
    from ozon_mcp import app as app_mod
    monkeypatch.setattr(app_mod, "MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "мусор-без-двоеточия,ddd:||")

    assert tenancy.client_tokens() == {}, "мусор разбираться не должен"
    assert tenancy.is_enabled() is False
    assert tenancy.is_configured() is True, "переменная ЗАДАНА — это и есть отличие"

    assert app_mod._resolve_mcp_auth(_request("хоть что-то")) == (False, None)
    assert app_mod._resolve_mcp_auth(_request("")) == (False, None)


def test_truly_empty_variable_still_means_mode_off(monkeypatch):
    """Обратная сторона: пустая переменная — это по-прежнему выключенный режим."""
    from ozon_mcp import app as app_mod
    monkeypatch.setattr(app_mod, "MCP_AUTH_TOKEN", "shared")
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "   ")
    assert tenancy.is_configured() is False
    assert app_mod._resolve_mcp_auth(_request("shared")) == (True, None)


def test_resolve(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,bbb:shop2|shop3")
    assert tenancy.resolve("aaa") == ("shop1",)
    assert tenancy.resolve("bbb") == ("shop2", "shop3")
    assert tenancy.resolve("ccc") is None
    assert tenancy.resolve("") is None


# ─── Подстановка ────────────────────────────────────────────

def test_enforce_is_a_noop_without_pinning():
    args = {"shop_id": "beta", "campaign_id": 7}
    assert tenancy.enforce(args) is args


def test_enforce_replaces_foreign_shop(pinned_to_alfa):
    args = {"shop_id": "beta", "campaign_id": 7}
    result = tenancy.enforce(args)
    assert result["shop_id"] == "alfa"
    assert result["campaign_id"] == 7
    assert args["shop_id"] == "beta", "исходный словарь мутировать нельзя"


def test_enforce_fills_in_missing_shop(pinned_to_alfa):
    assert tenancy.enforce({})["shop_id"] == "alfa"


# ─── Подстановка при нескольких магазинах ───────────────────
#
# Здесь правило противоположное, и это не непоследовательность. При одном магазине
# аргумент не несёт информации — подставить его молча безопасно, потому что другого
# ответа не существует. При нескольких подстановка вернула бы достоверно выглядящие
# числа не того магазина: самый дорогой класс дефектов в этом проекте.

@pytest.fixture
def pinned_to_three():
    """Сессия привязана к трём магазинам; «alfa» — первый, то есть по умолчанию."""
    token = tenancy.pin(["alfa", "beta", "gamma"])
    yield ("alfa", "beta", "gamma")
    tenancy.unpin(token)


def test_several_shops_do_not_guess_a_default(pinned_to_three):
    """Не назван магазин — аргумент не трогаем, а НЕ подставляем первый.

    Подстановка «первого» вернула бы дефект 23.09 через другую дверь: модель,
    забывшая `shop_id` на одном из четырёх кабинетов, получила бы числа первого
    и записала их под заголовком того, о котором спрашивала. Спросит диспетчер
    (`_call_tool_impl`, ветка «Укажите shop_id»).
    """
    assert tenancy.enforce({}) == {}
    assert tenancy.enforce({"shop_id": ""}) == {"shop_id": ""}


@pytest.mark.asyncio
async def test_missing_shop_asks_and_lists_only_own_shops(monkeypatch, tmp_path):
    """Диспетчер спрашивает магазин — и перечисляет ТОЛЬКО свои.

    Прежняя редакция брала весь каталог сервера, то есть на вопрос «а какие у
    меня есть» отвечала в том числе чужими идентификаторами — ровно тем, что
    `ozon_list_shops` прячет намеренно.
    """
    save_shops(tmp_path, {"alfa": {"name": "Альфа"}, "beta": {"name": "Бета"},
                          "alien": {"name": "Чужой"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    token = tenancy.pin(["alfa", "beta"])
    try:
        blocks = await _call_tool_impl("ozon_ad_campaigns", {})
    finally:
        tenancy.unpin(token)

    text = blocks[0].text
    assert "Укажите shop_id" in text
    assert "alfa" in text and "beta" in text
    assert "alien" not in text, "чужой магазин не перечисляем даже в подсказке"


def test_several_shops_honour_an_own_shop(pinned_to_three):
    args = {"shop_id": "gamma", "campaign_id": 7}
    assert tenancy.enforce(args) is args, "свой магазин подменять нечем и незачем"


def test_several_shops_refuse_a_foreign_shop(pinned_to_three):
    """🔴 Отказ, а НЕ подстановка: подставить нечего, а соврать — можно."""
    with pytest.raises(tenancy.ForeignShopError) as caught:
        tenancy.enforce({"shop_id": "delta"})
    assert caught.value.requested == "delta"
    assert caught.value.allowed == ("alfa", "beta", "gamma")
    assert "delta" in str(caught.value)


def test_single_shop_mode_still_substitutes_silently():
    """Сторож против «починили одно, сломали другое».

    Режим одного магазина проверен и держит границу подстановкой. Если чинящий
    многомагазинный режим заодно переведёт и его на отказ, каждый существующий
    клиент начнёт получать отказы там, где раньше молча работал.
    """
    token = tenancy.pin("alfa")
    try:
        assert tenancy.enforce({"shop_id": "чужой"})["shop_id"] == "alfa"
    finally:
        tenancy.unpin(token)


@pytest.mark.parametrize("weird", [7, ["alfa"], {"id": "alfa"}, True])
def test_non_string_shop_id_fails_closed(pinned_to_three, weird):
    """Нестроковый `shop_id` обязан падать в отказ, а не в пропуск.

    Модель присылает аргументы как попало, и `in` на кортеже строк вернёт False
    для любого не-строкового значения. Важно, в какую сторону: False здесь — это
    отказ (закрыто), а не «не совпало, значит подставим» (открыто). Тест сторожит
    именно направление: если кто-то заменит `raise` на возврат первого магазина,
    число `7` тихо станет магазином `alfa`.

    Пустая строка сюда не входит намеренно: она проверяется отдельно как
    «магазин не назван» и штатно означает магазин по умолчанию.
    """
    with pytest.raises(tenancy.ForeignShopError):
        tenancy.enforce({"shop_id": weird})


def test_shop_id_matching_is_exact_not_fuzzy(pinned_to_three):
    """Ни регистр, ни пробелы не должны «дотягивать» чужое имя до своего.

    Свёртка имён в этом проекте уже дважды давала дефект (регистр и разделитель
    в именах складов). Там она чинила сопоставление; здесь она ослабила бы
    границу — `ALFA` и `alfa ` не один и тот же магазин, пока их не признал
    владелец токена.
    """
    for near_miss in ("ALFA", "Alfa", " alfa", "alfa "):
        with pytest.raises(tenancy.ForeignShopError):
            tenancy.enforce({"shop_id": near_miss})


def test_pinned_one_answers_a_different_question_than_pinned(pinned_to_three):
    """«Привязка есть» и «выбора нет» совпадали, пока магазин был один."""
    assert tenancy.pinned() == ("alfa", "beta", "gamma")
    assert tenancy.pinned_one() is None


# ─── Поведение сервера ──────────────────────────────────────

def test_pinned_session_hides_shop_id_even_with_many_shops(monkeypatch, tmp_path, pinned_to_alfa):
    """Без привязки два магазина возвращают параметр в схемы; с привязкой — нет."""
    save_shops(tmp_path, {
        "alfa": {"name": "Альфа", "ozon_client_id": "1", "ozon_api_key": "a"},
        "beta": {"name": "Бета", "ozon_client_id": "2", "ozon_api_key": "b"},
    })
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    with_pin = {t.name for t in _visible_tools()
                if "shop_id" in (t.inputSchema.get("properties") or {})}
    assert with_pin == set(), "в привязанной сессии shop_id не должен быть виден"

    tenancy.unpin(tenancy.pin(None))  # проверяем обратное — без привязки
    token = tenancy.pin(None)
    try:
        without_pin = [t for t in _visible_tools()
                       if "shop_id" in (t.inputSchema.get("properties") or {})]
    finally:
        tenancy.unpin(token)
    assert without_pin, "без привязки при двух магазинах параметр обязан вернуться"


@pytest.mark.asyncio
async def test_list_shops_hides_neighbours(monkeypatch, tmp_path):
    save_shops(tmp_path, {
        "alfa": {"name": "Альфа", "ozon_client_id": "1", "ozon_api_key": "a"},
        "beta": {"name": "Бета", "ozon_client_id": "2", "ozon_api_key": "b"},
    })
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    token = tenancy.pin("alfa")
    try:
        blocks = await _call_tool_impl("ozon_list_shops", {})
    finally:
        tenancy.unpin(token)
    text = blocks[0].text
    assert "alfa" in text
    assert "beta" not in text, "чужой shop_id не должен попадать клиенту"


@pytest.mark.asyncio
async def test_foreign_shop_id_is_ignored_not_rejected(monkeypatch, tmp_path, pinned_to_alfa):
    """Клиент называет чужой магазин — сервер работает со своим.

    В хранилище есть только «beta». Сессия привязана к «alfa», которого нет.
    Если подстановка работает, поиск ключей уйдёт за «alfa» и упрётся в его
    отсутствие. Если не работает — сервер возьмёт ключи «beta», то есть чужие.
    """
    save_shops(tmp_path, {"beta": {"name": "Бета", "ozon_perf_client_id": "2",
                                   "ozon_perf_client_secret": "b"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    with pytest.raises(ValueError, match="Магазин 'alfa' не найден"):
        await _call_tool_impl("ozon_ad_campaigns", {"shop_id": "beta"})


@pytest.mark.asyncio
async def test_stats_are_attributed_to_the_pinned_shop(monkeypatch, tmp_path, pinned_to_alfa):
    """Расход должен записываться на владельца сессии, а не на названный магазин."""
    save_shops(tmp_path, {"beta": {"name": "Бета"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    seen: list[str] = []

    async def _record(name, duration_ms, success, error_text, shop_id):
        seen.append(shop_id)

    server.set_stats_callback(_record)
    try:
        await server.call_tool("ozon_ad_campaigns", {"shop_id": "beta"})
    finally:
        server.set_stats_callback(None)
    assert seen == ["alfa"]


# ─── Дефект 23.09: бриф по одному кабинету из четырёх ───────

@pytest.mark.asyncio
async def test_brief_over_several_cabinets_sees_all_of_them(monkeypatch, tmp_path):
    """Воспроизведение дефекта 23.09 целиком.

    Утренний бриф вызвал `ozon_list_shops`, получил ОДИН магазин из четырёх и
    честно отчитался по нему, написав «не собраны: нет». Три кабинета с 81 %
    расхода остались вне отчёта, и ни в ответе, ни в тексте не было признака,
    что чего-то не хватает.

    Проверяются обе половины, потому что каждая по отдельности оставляет дефект:
    список магазинов и наличие `shop_id` в схемах. Видеть четыре и не иметь чем
    их выбрать — то же самое, что видеть один.
    """
    save_shops(tmp_path, {
        "main":  {"name": "StockPot", "ozon_client_id": "1", "ozon_api_key": "a"},
        "Shop2": {"name": "Super_Detki", "ozon_client_id": "2", "ozon_api_key": "b"},
        "shop3": {"name": "Restopit", "ozon_client_id": "3", "ozon_api_key": "c"},
        "alien": {"name": "Чужой", "ozon_client_id": "9", "ozon_api_key": "z"},
    })
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    token = tenancy.pin(["main", "Shop2", "shop3"])
    try:
        blocks = await _call_tool_impl("ozon_list_shops", {})
        visible = {t.name for t in _visible_tools()
                   if "shop_id" in (t.inputSchema.get("properties") or {})}
    finally:
        tenancy.unpin(token)

    listed = blocks[0].text
    for shop_id in ("main", "Shop2", "shop3"):
        assert shop_id in listed, f"{shop_id} обязан быть в списке — он свой"
    assert "alien" not in listed, "чужой магазин не перечисляем даже владельцу трёх"
    assert visible, "при нескольких магазинах shop_id обязан остаться в схемах"


def test_multishop_pin_keeps_shop_id_even_when_catalogue_has_one(monkeypatch, tmp_path):
    """Изолирует ветку «привязка есть, магазинов в ней несколько».

    В brief-тесте её не отличить: там каталог из четырёх магазинов, и параметр
    вернулся бы и по старой ветке «магазинов на сервере больше одного». Здесь
    каталог из ОДНОГО, поэтому решает только привязка.

    Ветка несущая, а не косметическая. Без неё получается тупик: схемы прячут
    `shop_id`, модель его не присылает, а `enforce` при нескольких магазинах
    ничего не подставляет — каждый вызов упирается в «Укажите shop_id», ответить
    на который нечем.
    """
    save_shops(tmp_path, {"alfa": {"name": "Альфа", "ozon_client_id": "1",
                                   "ozon_api_key": "a"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    token = tenancy.pin(["alfa", "beta"])
    try:
        visible = [t.name for t in _visible_tools()
                   if "shop_id" in (t.inputSchema.get("properties") or {})]
    finally:
        tenancy.unpin(token)
    assert visible, "при нескольких магазинах в токене параметр обязан остаться"


@pytest.mark.asyncio
async def test_foreign_shop_refusal_is_structured_and_counted(monkeypatch, tmp_path):
    """Отказ уходит конвертом рода `forbidden` и попадает в статистику.

    Две вещи, которые легко потерять. Конверт: без него наружу выпало бы голое
    исключение — то есть отказ, неотличимый от поломки транспорта. Статистика:
    `enforce` срабатывает раньше, чем заводится контекст вызова, и отказ ушёл бы
    мимо журнала, оставив в нём вид, будто вызова не было вовсе.
    """
    import json

    save_shops(tmp_path, {"alfa": {"name": "Альфа"}, "beta": {"name": "Бета"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    seen: list[tuple] = []

    async def _record(name, duration_ms, success, error_text, shop_id):
        seen.append((name, success, shop_id))

    token = tenancy.pin(["alfa", "beta"])
    server.set_stats_callback(_record)
    try:
        blocks = await server.call_tool("ozon_ad_campaigns", {"shop_id": "delta"})
    finally:
        server.set_stats_callback(None)
        tenancy.unpin(token)

    error = json.loads(blocks[0].text)["_error"]
    assert error["kind"] == "forbidden", "не our_bug: чинить тут нечего, это штатный отказ"
    assert error["retryable"] is False
    assert error["requested"] == "delta"
    assert error["allowed"] == ["alfa", "beta"]
    assert "delta" in blocks[1].text

    assert len(seen) == 1, "отказ обязан попасть в журнал вызовов"
    assert seen[0][0] == "ozon_ad_campaigns"
    assert seen[0][1] is False
    assert seen[0][2] == "alfa", (
        "отказ пишется на СВОЙ магазин: иначе арендатор пишет строки журнала под "
        "чужим идентификатором, просто называя его в аргументе")


def test_refusal_text_does_not_publish_the_shop_list_to_neighbours():
    """🔴 Перечень магазинов не должен попадать в ТЕКСТ исключения.

    `str(exc)` уходит в общую таблицу вызовов (`stats.record_call`), а она не
    разделена по арендаторам: `ozon_degradations` — профиль core, выключить
    нельзя, фильтра по магазину нет — отдаёт тексты ошибок любому клиенту.
    Состав кабинета в этом тексте означал бы, что достаточно один раз попросить
    чужой магазин, чтобы опубликовать соседям список своих.

    Спросившему список всё равно нужен — он приходит отдельным полем конверта,
    и конверт уходит только ему.
    """
    exc = tenancy.ForeignShopError("delta", ("alfa", "beta", "gamma"))
    text = str(exc)
    assert "delta" in text, "что именно отвергнуто — сказать обязаны"
    for own in ("alfa", "beta", "gamma"):
        assert own not in text, f"{own} не должен попасть в общий журнал"

    from ozon_mcp import failures
    detail = failures.classify(exc)
    assert detail["allowed"] == ["alfa", "beta", "gamma"], "спросившему — полный список"
    assert "alfa" in failures.human_text({"_error": detail})


@pytest.mark.asyncio
async def test_shop_in_token_but_missing_on_server_is_named_not_hidden(monkeypatch, tmp_path):
    """Опечатка в токене не должна выглядеть как «столько кабинетов и есть».

    Ровно форма дефекта 23.09: клиенту видно МЕНЬШЕ, чем у него есть, и узнать
    об этом неоткуда. Недостача называется вслух.
    """
    save_shops(tmp_path, {"alfa": {"name": "Альфа"}, "beta": {"name": "Бета"}})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    token = tenancy.pin(["alfa", "beta", "opechatka"])
    try:
        blocks = await _call_tool_impl("ozon_list_shops", {})
    finally:
        tenancy.unpin(token)

    text = blocks[0].text
    assert "opechatka" in text, "пропавший магазин обязан быть назван"
    assert "НЕ НАЙДЕНЫ" in text
    assert "alfa" in text and "beta" in text


@pytest.mark.asyncio
async def test_refused_call_never_reaches_ozon(monkeypatch, tmp_path):
    """Граница срабатывает ДО сети: у чужого магазина ключей мы не спрашиваем.

    Отдельно от предыдущего, потому что конверт правильного рода можно получить и
    после похода в Ozon — а тогда чужой запрос уже отправлен и, возможно, оплачен.
    """
    # Ключи ОБЕ половины и у обоих магазинов: без них «delta» упрётся в их
    # отсутствие ещё до всякой границы, и растяжка не сработает ни при каком
    # исходе. Первая редакция этого теста так и была написана — «delta» имел
    # только Performance-ключи, а диспетчер строит Seller-клиента раньше, чем
    # доходит до рекламных веток. Тест зеленел, не проверяя заявленного.
    full = lambda n: {"name": n, "ozon_client_id": "1", "ozon_api_key": "k",
                      "ozon_perf_client_id": "2", "ozon_perf_client_secret": "s"}
    save_shops(tmp_path, {"alfa": full("Альфа"), "beta": full("Бета"),
                          "delta": full("Дельта")})
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    calls: list[str] = []

    def _tripwire(shop_id, *args, **kwargs):
        calls.append(shop_id)
        raise AssertionError("отвергнутый вызов не должен доходить до клиента Ozon")

    # Растяжки на ОБЕ двери к сети, и обе с raising=True: с `raising=False`
    # опечатка в имени завела бы атрибут с нуля, растяжка не встала бы ни на чей
    # путь, и тест прошёл бы впустую — доказав ровно ничего.
    monkeypatch.setattr(server, "_get_seller", _tripwire, raising=True)
    monkeypatch.setattr(server, "_get_perf", _tripwire, raising=True)

    token = tenancy.pin(["alfa", "beta"])
    try:
        with pytest.raises(tenancy.ForeignShopError):
            await _call_tool_impl("ozon_ad_campaigns", {"shop_id": "delta"})
    finally:
        tenancy.unpin(token)
    assert calls == []


# ─── Авторизация ────────────────────────────────────────────

def _request(token: str):
    """Минимальный Request с заголовком Authorization."""
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/sse", "query_string": b"",
             "headers": [(b"authorization", f"Bearer {token}".encode())]}
    return Request(scope)


def test_client_token_resolves_to_its_shop(monkeypatch):
    from ozon_mcp import app as app_mod
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1,bbb:shop2|shop3")
    assert app_mod._resolve_mcp_auth(_request("aaa")) == (True, ("shop1",))
    assert app_mod._resolve_mcp_auth(_request("bbb")) == (True, ("shop2", "shop3"))
    assert app_mod._resolve_mcp_auth(_request("zzz")) == (False, None)


def test_shared_token_stops_working_once_client_tokens_exist(monkeypatch):
    """Общий токен не привязан к магазину — оставить его значит оставить обход."""
    from ozon_mcp import app as app_mod
    monkeypatch.setattr(app_mod, "MCP_AUTH_TOKEN", "shared")
    monkeypatch.setenv("MCP_CLIENT_TOKENS", "aaa:shop1")
    assert app_mod._resolve_mcp_auth(_request("shared")) == (False, None)


def test_original_behaviour_is_untouched_while_disabled(monkeypatch):
    from ozon_mcp import app as app_mod
    monkeypatch.setattr(app_mod, "MCP_AUTH_TOKEN", "shared")
    assert app_mod._resolve_mcp_auth(_request("shared")) == (True, None)
    assert app_mod._resolve_mcp_auth(_request("nope")) == (False, None)
