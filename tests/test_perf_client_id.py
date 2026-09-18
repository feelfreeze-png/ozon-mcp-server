"""Полная форма client_id для Performance API.

Ozon принимает client_id только как <id>@advertising.performance.ozon.ru. На
короткой форме токен отвечает `401 invalid_client`, причём текст ошибки не
называет причину — снаружи это неотличимо от «ключи неверные», и разбор уходит
искать проблему в правах, в сети и в чём угодно ещё.

Проверено живьём 2026-09-18: та же пара ключей даёт 401 без суффикса и
200 с `expires_in: 1800` с ним.
"""

from ozon_mcp.client import PERF_CLIENT_ID_SUFFIX, OzonPerformanceClient


def test_short_form_gets_the_suffix():
    c = OzonPerformanceClient("12345678", "secret")
    assert c.client_id == "12345678" + PERF_CLIENT_ID_SUFFIX


def test_full_form_is_left_alone():
    full = "12345678" + PERF_CLIENT_ID_SUFFIX
    assert OzonPerformanceClient(full, "secret").client_id == full


def test_foreign_domain_is_not_mangled():
    """Чужая доменная часть — не наш случай: дописывать к ней нельзя."""
    other = "12345678@example.invalid"
    assert OzonPerformanceClient(other, "secret").client_id == other


def test_empty_stays_empty():
    """Пустой id должен упасть как «ключи не заданы», а не как «неверный клиент»."""
    assert OzonPerformanceClient("", "secret").client_id == ""
