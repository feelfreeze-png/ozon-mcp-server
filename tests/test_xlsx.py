"""Чтение XLSX стандартной библиотекой.

Замерено 21.09.2026: в прод-образе нет ни `openpyxl`, ни `pandas`, ни `xlrd` — только
`zipfile` и `xml.etree`. Новая зависимость поехала бы на прод ради одного отчёта в сутки.

Эталон здесь собран **чужими руками** — файл от `openpyxl`, лежит в `tests/data`. Сверять
свою читалку со своим же писателем бессмысленно: оба согласятся на одной ошибке.
"""

import zipfile
from pathlib import Path

import pytest

from ozon_mcp import xlsx

REFERENCE = Path(__file__).parent / "data" / "reference-openpyxl.xlsx"


def _minimal(sheet_xml: str, shared_xml: str | None = None) -> bytes:
    """Собрать xlsx руками — так пишут не-питоновские генераторы (POI, ExcelJS)."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if shared_xml is not None:
            archive.writestr("xl/sharedStrings.xml", shared_xml)
    return buffer.getvalue()


_SHEET = (
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheetData>{}</sheetData></worksheet>"
)


# ── Позиция ячейки ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("reference,expected", [
    ("A1", 0), ("B7", 1), ("Z3", 25), ("AA1", 26), ("AB12", 27),
])
def test_column_index(reference, expected):
    assert xlsx.column_index(reference) == expected


def test_a_reference_without_a_column_is_refused():
    with pytest.raises(xlsx.XlsxError):
        xlsx.column_index("7")


def test_a_missing_cell_does_not_shift_the_row():
    """🔴 В XML ячейки без значения просто отсутствуют.

    Наивный разбор «подряд» сдвинул бы строку влево, и `qty` уехал бы в `warehouse` —
    не ошибка разбора, а другое число на своём месте.
    """
    data = _minimal(_SHEET.format(
        '<row r="1"><c r="A1"><v>1</v></c><c r="C1"><v>3</v></c></row>'))
    (row,) = list(xlsx.rows(data))
    assert row == [1, None, 3]


# ── Типы значений ────────────────────────────────────────────────────────────


def test_shared_strings_are_resolved():
    """Общую таблицу строк пишут не-питоновские генераторы; без неё будут числа."""
    data = _minimal(
        _SHEET.format('<row r="1"><c r="A1" t="s"><v>0</v></c></row>'),
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>ХАБАРОВСК_2_РФЦ</t></si></sst>")
    assert list(xlsx.rows(data)) == [["ХАБАРОВСК_2_РФЦ"]]


def test_rich_text_is_glued_not_truncated():
    """Имя склада, разбитое начертанием, обязано склеиться целиком."""
    data = _minimal(
        _SHEET.format('<row r="1"><c r="A1" t="s"><v>0</v></c></row>'),
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><r><t>ТВЕРЬ</t></r><r><t>_РФЦ</t></r></si></sst>")
    assert list(xlsx.rows(data)) == [["ТВЕРЬ_РФЦ"]]


def test_a_broken_shared_string_reference_is_loud():
    data = _minimal(
        _SHEET.format('<row r="1"><c r="A1" t="s"><v>99</v></c></row>'),
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>одна</t></si></sst>")
    with pytest.raises(xlsx.XlsxError, match="вне таблицы"):
        list(xlsx.rows(data))


def test_numbers_keep_their_kind():
    data = _minimal(_SHEET.format(
        '<row r="1"><c r="A1"><v>3</v></c><c r="B1"><v>12.75</v></c></row>'))
    (row,) = list(xlsx.rows(data))
    assert row == [3, 12.75]
    assert isinstance(row[0], int) and isinstance(row[1], float)


def test_inline_strings_are_read():
    data = _minimal(_SHEET.format(
        '<row r="1"><c r="A1" t="inlineStr"><is><t>СОФЬИНО</t></is></c></row>'))
    assert list(xlsx.rows(data)) == [["СОФЬИНО"]]


# ── Даты ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("serial,day", [(46082, "2026-03-01"), (1, "1899-12-31")])
def test_excel_serial_converts(serial, day):
    """Ноль Excel — 30.12.1899: в нём есть несуществующее 29 февраля 1900 года."""
    assert xlsx.excel_serial_to_day(serial) == day


# ── Отказы ───────────────────────────────────────────────────────────────────


def test_a_non_zip_is_named_not_parsed():
    """Протухшая ссылка отдаёт XML. Разобрать его как таблицу значит получить пустоту."""
    with pytest.raises(xlsx.XlsxError, match="не является zip"):
        list(xlsx.rows(b"<Error><Code>AccessDenied</Code></Error>"))


def test_a_zip_without_a_sheet_is_named():
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("что-то.txt", "не книга")
    with pytest.raises(xlsx.XlsxError, match="нет ни одного листа"):
        list(xlsx.rows(buffer.getvalue()))


def test_an_empty_sheet_is_not_an_empty_report():
    data = _minimal(_SHEET.format(""))
    with pytest.raises(xlsx.XlsxError, match="Пустой лист"):
        xlsx.table(data)


# ── Таблица целиком ──────────────────────────────────────────────────────────


def test_rows_are_padded_to_the_header_width():
    """⚠️ Хвостовые пустые ячейки в XML отсутствуют, и строка приходит короче.

    Без выравнивания загрузчик получил бы IndexError на последней колонке — отказ в
    разборе вместо пустого значения.
    """
    data = _minimal(_SHEET.format(
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Дата</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>SKU</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>Кол-во</t></is></c></row>'
        '<row r="2"><c r="A2"><v>46082</v></c></row>'))
    header, body = xlsx.table(data)
    assert header == ["Дата", "SKU", "Кол-во"]
    assert body == [[46082, None, None]]


@pytest.mark.skipif(not REFERENCE.exists(), reason="эталонный файл не приложен")
def test_matches_a_file_written_by_openpyxl():
    """Эталон собран ЧУЖИМ писателем: своя читалка со своим писателем согласятся на ошибке."""
    header, body = xlsx.table(REFERENCE.read_bytes())
    assert header == ["Дата", "SKU", "Склад", "Кол-во экземпляров", "Платный объём"]
    assert len(body) == 3
    assert xlsx.excel_serial_to_day(body[0][0]) == "2026-03-01"
    assert body[0][2] == "ХАБАРОВСК_2_РФЦ"
    assert body[1][2] is None, "пропущенная ячейка в середине сдвинула строку"
    assert body[2] == [46084, 914120635, "ТВЕРЬ_РФЦ", None, None]
