"""Режим только для чтения.

Смысл режима — чтобы ошибка модели не стоила денег. Поэтому проверяется не «список
непустой», а два конкретных свойства: запрещённый инструмент не доходит до Ozon, и
в списке нет дыр.
"""

import pytest

from ozon_mcp import readonly, toolsets
from ozon_mcp.server import TOOLS, _call_tool_impl, _enabled_tools, _visible_tools


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OZON_READONLY", raising=False)
    monkeypatch.delenv("OZON_TOOLSETS", raising=False)


def test_disabled_by_default():
    assert readonly.is_enabled() is False
    assert readonly.is_blocked("ozon_ad_campaign_bids") is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "да"])
def test_truthy_values(monkeypatch, value):
    monkeypatch.setenv("OZON_READONLY", value)
    assert readonly.is_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", " "])
def test_falsy_values(monkeypatch, value):
    monkeypatch.setenv("OZON_READONLY", value)
    assert readonly.is_enabled() is False


def test_bids_tool_is_in_the_list():
    """Отдельным тестом, потому что имя обманывает.

    `ozon_ad_campaign_bids` звучит как чтение ставок, а обновляет их. Любой шаблон
    вида «_set/_update/_create — значит запись» его пропустит, и режим станет ложным
    обещанием: инструмент, тратящий деньги, останется доступным.
    """
    assert "ozon_ad_campaign_bids" in readonly.WRITE_TOOLS


def test_every_listed_tool_exists():
    """Опечатка в имени = молча не запрещённый инструмент."""
    names = {t.name for t in TOOLS}
    assert readonly.WRITE_TOOLS <= names, readonly.WRITE_TOOLS - names


def test_no_money_moving_tool_is_missing_from_the_list():
    """Сторож на будущее: новый мутирующий рекламный инструмент должен попасть в список.

    Эвристика намеренно шире самого списка — она обязана ловить кандидатов, которых
    забыли внести. Срабатывание означает «проверь и внеси», а не «эвристика плохая».
    """
    verbs = ("_create", "_update", "_delete", "_add", "_stop", "_activate",
             "_enable", "_disable", "_set")
    suspects = {
        t.name for t in TOOLS
        if toolsets.profile_of(t.name) == "ads"
        and any(v in t.name for v in verbs)
        and not t.name.startswith("ozon_report_")
    }
    assert suspects <= readonly.WRITE_TOOLS, suspects - readonly.WRITE_TOOLS


def test_reports_stay_available():
    """Отчёты пишут задание, но денег не тратят — без них аналитика обрезана."""
    assert not any(n.startswith("ozon_report_") for n in readonly.WRITE_TOOLS)


def test_catalogue_hides_write_tools(monkeypatch):
    monkeypatch.setenv("OZON_READONLY", "1")
    visible = {t.name for t in _enabled_tools(_visible_tools())}
    assert not (visible & readonly.WRITE_TOOLS), visible & readonly.WRITE_TOOLS
    assert "ozon_ad_campaigns" in visible, "чтение обязано остаться"


@pytest.mark.asyncio
async def test_call_is_refused_before_reaching_ozon(monkeypatch):
    """Схема у клиента могла остаться от прошлой сессии — вызов дойдёт до сервера.

    Если бы отказ стоял после разрешения магазина, запрос ушёл бы в Ozon и списал
    деньги. Здесь магазин заведомо не задан: дойди вызов до него, упал бы с другой
    ошибкой, и тест это поймает.
    """
    monkeypatch.setenv("OZON_READONLY", "1")
    blocks = await _call_tool_impl("ozon_ad_campaign_bids",
                                   {"shop_id": "нет-такого", "campaign_id": 1, "bids": []})
    text = blocks[0].text
    assert "OZON_READONLY" in text
    assert "только для чтения" in text


@pytest.mark.asyncio
async def test_read_tool_is_not_refused(monkeypatch):
    """Режим не должен глушить чтение: отказ обязан прийти про магазин, а не про режим."""
    monkeypatch.setenv("OZON_READONLY", "1")
    with pytest.raises(Exception) as exc:
        await _call_tool_impl("ozon_ad_campaigns", {"shop_id": "нет-такого"})
    assert "OZON_READONLY" not in str(exc.value)
