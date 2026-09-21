"""D1: режим только для чтения закрывает пишущих ВСЕХ профилей, а не только рекламных.

🔴 **Порядок в плане обратный привычному, и вот почему.** Замер до правки: в профиле
`catalog` 28 инструментов, из них 10 пишущих, и `OZON_READONLY` не блокировал **ни
одного** — он закрывал ровно девять рекламных. Включив профиль как есть, мы дали бы
ассистенту `ozon_product_delete`, `ozon_product_archive` и `ozon_product_update_images`
(последний стирает всё, что не передано). Цена ошибки в карточке выше, чем в ставке:
ставку можно вернуть, удалённую карточку — нет.

**Главный сторож здесь структурный, а не по именам.** Список пишущих выводится из
самого исходника: какой метод клиента зовёт диспетчер и по какому пути тот ходит. Сторож
по имени пропустил бы `ozon_chat_send` и `ozon_product_update_stocks` — в их путях нет
слова-признака, — а именно они отправляют сообщение покупателю и меняют остатки.
"""

import os
import re
from pathlib import Path

import pytest

from ozon_mcp import readonly, server

REPO = Path(__file__).resolve().parents[1]

#: Пути, которые меняют состояние. Слова взяты из самих путей Ozon, а не придуманы.
_WRITE_PATH = re.compile(
    r"/(create|update|delete|archive|unarchive|add|remove|activate|deactivate|"
    r"stop|start|enable|disable|import|set|confirm|cancel|ship|approve|reject|"
    r"answer|comment|decline|change-activity|read|send)\b"
)


def _dispatch_map() -> dict[str, list[str]]:
    """Инструмент → методы клиента, которые зовёт диспетчер."""
    source = (REPO / "ozon_mcp/server.py").read_text(encoding="utf-8")
    found = {}
    for match in re.finditer(
        r'if name == "([a-z0-9_]+)":(.*?)(?=\n    if name == "|\n    raise|\Z)',
        source, re.S,
    ):
        found[match.group(1)] = sorted(set(
            re.findall(r"\b(?:p|s|seller|perf|client)\.([a-z_0-9]+)\(", match.group(2))))
    return found


def _client_paths() -> dict[str, list[tuple[str, str]]]:
    """Метод клиента → (глагол, путь)."""
    source = (REPO / "ozon_mcp/client.py").read_text(encoding="utf-8")
    found = {}
    for match in re.finditer(
        r"async def ([a-z_0-9]+)\(.*?\n(.*?)(?=\n    async def |\n    @|\nclass |\Z)",
        source, re.S,
    ):
        found[match.group(1)] = re.findall(
            r'self\._(post|put|get|delete|send)\(\s*f?"([^"]+)"', match.group(2))
    return found


def _tools_hitting_write_paths() -> dict[str, str]:
    """Инструменты, чей диспетчер ходит по пишущему пути."""
    paths = _client_paths()
    writing = {}
    for tool, methods in _dispatch_map().items():
        for method in methods:
            for verb, path in paths.get(method, []):
                if verb in ("post", "put", "send") and _WRITE_PATH.search(path):
                    writing[tool] = path
                    break
    return writing


def test_every_tool_on_a_write_path_is_classified():
    """🔴 Структурный сторож: новый пишущий инструмент обязан быть разобран.

    Три исхода, и все три названы явно: закрыт режимом, отчётный (пишет задание, но не
    состояние), либо путь только выглядит пишущим. Четвёртого — «забыли» — быть не должно.
    """
    classified = (readonly.WRITE_TOOLS | readonly.REPORT_TOOLS
                  | readonly.READ_PATHS_THAT_LOOK_LIKE_WRITES)
    unclassified = {tool: path for tool, path in _tools_hitting_write_paths().items()
                    if tool not in classified}
    assert not unclassified, (
        "инструменты ходят по пишущим путям и ни к чему не отнесены: "
        f"{unclassified}. Добавьте их в WRITE_TOOLS, REPORT_TOOLS или "
        "READ_PATHS_THAT_LOOK_LIKE_WRITES — с объяснением."
    )


def test_no_phantom_names_in_the_lists():
    """Опечатка в имени делает запись бесполезной и при этом незаметной."""
    catalogue = {tool.name for tool in server.TOOLS}
    for name, group in (("WRITE_TOOLS", readonly.WRITE_TOOLS),
                        ("REPORT_TOOLS", readonly.REPORT_TOOLS),
                        ("READ_PATHS…", readonly.READ_PATHS_THAT_LOOK_LIKE_WRITES)):
        missing = sorted(group - catalogue)
        assert not missing, f"{name}: имён нет в каталоге инструментов: {missing}"


def test_the_three_named_catalog_tools_are_closed():
    """Поимённо, а не счётом — так требует приёмка D1."""
    for name in ("ozon_product_delete", "ozon_product_archive",
                 "ozon_product_update_images"):
        assert name in readonly.WRITE_TOOLS, name


@pytest.mark.parametrize("name", sorted(readonly.WRITE_TOOLS))
def test_each_write_tool_is_hidden_and_refused(monkeypatch, name):
    """Оба пути: инструмента нет в каталоге И прямой вызов отвергается.

    Одного мало: спрятанный, но исполняемый инструмент — это защита, которой нет,
    а выглядит она как защита.
    """
    monkeypatch.setenv("OZON_READONLY", "1")
    assert readonly.is_blocked(name), f"{name} не блокируется"
    assert name in readonly.refusal_message(name)


def test_catalogue_shrinks_by_exactly_the_write_tools(monkeypatch):
    monkeypatch.delenv("OZON_READONLY", raising=False)
    full = {tool.name for tool in server.TOOLS}
    monkeypatch.setenv("OZON_READONLY", "1")
    allowed = {name for name in full if not readonly.is_blocked(name)}
    assert full - allowed == set(readonly.WRITE_TOOLS)
    assert len(allowed) == len(full) - len(readonly.WRITE_TOOLS)


def test_reading_tools_stay_available(monkeypatch):
    """Режим не должен глушить то, ради чего сервер и нужен."""
    monkeypatch.setenv("OZON_READONLY", "1")
    for name in ("ozon_ad_campaigns", "ozon_ad_statistics_products_sku",
                 "ozon_product_list", "ozon_analytics", "ozon_placement_zone"):
        assert not readonly.is_blocked(name), name


def test_report_generation_is_deliberately_allowed(monkeypatch):
    """Решение записано в модуле: отчёт создаёт задание, но денег не тратит."""
    monkeypatch.setenv("OZON_READONLY", "1")
    for name in sorted(readonly.REPORT_TOOLS):
        assert not readonly.is_blocked(name), name


def test_mode_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OZON_READONLY", raising=False)
    assert not readonly.is_enabled()
    assert not readonly.is_blocked("ozon_product_delete")


def test_refusal_names_the_reason_not_just_the_fact():
    """«Выключено политикой» можно попросить изменить, «не умеем» закрывает разговор."""
    message = readonly.refusal_message("ozon_product_delete")
    assert "OZON_READONLY" in message
    assert "только для чтения" in message
    assert "карточки" in message, "текст отказа отстал от состава списка"


def test_coverage_grew_past_the_advertising_nine():
    """Замер до правки: девять рекламных и ни одного из остальных профилей."""
    assert len(readonly.WRITE_TOOLS) > 9
    assert os.path.basename(readonly.__file__) == "readonly.py"
