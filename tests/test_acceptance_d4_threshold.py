"""Порог приёмки D4 читается из документа, а не хранится в скрипте отдельной копией.

Порог был записан **до** первого прогона отчёта (коммит b28e04a): в этом и был смысл.
Две копии одного числа — документ и скрипт — расходятся молча, и первым расходится тот,
на который никто не смотрит. Здесь они сверяются механически: правка документа без
правки скрипта (или наоборот) роняет тест.

⚠️ Тест сверяет **совпадение**, а не правильность. Если когда-нибудь порог будет
пересмотрен, менять надо оба места и объяснять причину в документе — а не подгонять
число под полученный результат.
"""

import re
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parent.parent / "docs" / "ACCEPTANCE-D4.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.exists(), f"документ порога отсутствует: {DOC}"
    return DOC.read_text(encoding="utf-8")


def _constants():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_d4.py"
    spec = importlib.util.spec_from_file_location("reconcile_d4", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_share_threshold_matches_the_document(text):
    share = re.search(r"не ниже (\d+)\s*%", text)
    assert share, "в документе не нашлась формулировка «не ниже N %»"
    assert _constants().MIN_SHARE == int(share.group(1)) / 100


def test_absolute_threshold_matches_the_document(text):
    absolute = re.search(r"не более (\d+) несовпавших", text)
    assert absolute, "в документе не нашлась формулировка «не более N несовпавших»"
    assert _constants().MAX_MISMATCH == int(absolute.group(1))


def test_core_floor_matches_the_document(text):
    floor = re.search(r"ядро меньше \*\*(\d+) SKU\*\*", text)
    assert floor, "в документе не нашлась формулировка «если ядро меньше N SKU»"
    assert _constants().MIN_CORE == int(floor.group(1))


def test_document_still_states_that_a_small_core_is_not_a_pass(text):
    """Третий исход — законный ответ, и сворачивать его в первый запрещено."""
    assert "НЕ ПРОВЕДЁННОЙ" in text or "НЕ ПРОВЕДЕНА" in text


# ── Мост по именам складов ───────────────────────────────────────────────────


def test_case_and_separator_are_both_folded():
    """🔴 Дважды подряд отказ моста выглядел как отсутствие данных.

    Замеры на боевом кабинете 22.09.2026:
      регистр    — `Казань_РФЦ_НОВЫЙ`   против `КАЗАНЬ_РФЦ_НОВЫЙ`;
      разделитель — `Ростов_на_Дону_РФЦ` против `РОСТОВ-НА-ДОНУ_РФЦ`.
    В обоих случаях склад считался отсутствующим в снимке, а его SKU уезжали в корзину
    «не проверено» — то есть в «не сошлось», а не в «не смогли сверить».
    """
    from scripts.reconcile_d4 import _fold

    assert _fold("Казань_РФЦ_НОВЫЙ") == _fold("КАЗАНЬ_РФЦ_НОВЫЙ")
    assert _fold("Ростов_на_Дону_РФЦ") == _fold("РОСТОВ-НА-ДОНУ_РФЦ")
    assert _fold("Санкт_Петербург_РФЦ") == _fold("САНКТ-ПЕТЕРБУРГ_РФЦ")
    assert _fold("СПБ КОЛПИНО РФЦ") == _fold("СПБ_КОЛПИНО_РФЦ")


def test_folding_does_not_merge_different_warehouses():
    """Свёртка обязана чинить мост, а не склеивать соседей.

    `РОСТОВ-НА-ДОНУ_РФЦ` и `РОСТОВ_НА_ДОНУ_2_РФЦ` — разные склады, и оба живые.
    """
    from scripts.reconcile_d4 import _fold

    assert _fold("РОСТОВ-НА-ДОНУ_РФЦ") != _fold("РОСТОВ_НА_ДОНУ_2_РФЦ")
    assert _fold("НОВОСИБИРСК_РФЦ_НОВЫЙ") != _fold("НОВОСИБИРСК_3_РФЦ")
    assert _fold("КРАСНОЯРСК_МРФЦ") != _fold("КРАСНОЯРСК_СТАРЦЕВО_РФЦ")


def test_an_empty_name_folds_to_empty_not_to_a_match():
    from scripts.reconcile_d4 import _fold

    assert _fold(None) == "" and _fold("") == ""
    assert _fold(None) != _fold("ТВЕРЬ_РФЦ")
