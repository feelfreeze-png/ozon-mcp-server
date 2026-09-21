"""Чтение XLSX стандартной библиотекой. Без зависимостей.

**Почему без openpyxl.** В прод-образе нет ни одной библиотеки для XLSX — замерено
21.09.2026: `openpyxl`, `pandas`, `xlrd` отсутствуют, есть только `zipfile` и
`xml.etree`. Новая зависимость поехала бы на прод ради одного отчёта в сутки. XLSX — это
zip с XML, и нужная его часть читается сотней строк.

**Что проверено.** Разбор сверен с `openpyxl` на файле, собранном самим `openpyxl`
(независимый производитель), и на файле со `sharedStrings.xml`, как пишут не-питоновские
генераторы. Совпадение построчно, включая склейку rich-text (`<si><r><t>ТВЕРЬ</t></r>
<r><t>_РФЦ</t></r></si>` → `ТВЕРЬ_РФЦ`) и `None` на месте пропущенной ячейки.

🔴 **Пропущенная ячейка — не сдвиг.** В XML ячейки без значения просто отсутствуют, и
наивный разбор «подряд» сдвинул бы всю строку влево: `qty` уехал бы в `warehouse`.
Поэтому позиция берётся из атрибута `r` (`B7`), а не из порядка следования.

⚠️ **Даты.** Excel хранит их числом дней от 1899-12-30, и отличить дату от числа можно
только по стилю ячейки. Здесь стили не разбираются: значения возвращаются как есть, а
решение принимает вызывающий — он знает, какая колонка чем должна быть. Догадываться
здесь дороже, чем спросить у колонки: дата, прочитанная как 45 000, выглядит как число.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from typing import Iterator
from xml.etree import ElementTree

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: Ноль Excel. Именно 30 декабря 1899: в Excel есть несуществующее 29 февраля 1900,
#: и смещение на день уже заложено в этой константе.
_EXCEL_EPOCH = date(1899, 12, 30)


class XlsxError(ValueError):
    """Файл не читается как XLSX. Причина названа."""


def column_index(reference: str) -> int:
    """`B7` → 1. Позиция ячейки берётся отсюда, а не из порядка следования."""
    index = 0
    for char in reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    if index == 0:
        raise XlsxError(f"ссылка на ячейку без колонки: {reference!r}")
    return index - 1


def excel_serial_to_day(value: float) -> str:
    """Число Excel → `YYYY-MM-DD`. Вызывается только когда колонка объявлена датой."""
    return (_EXCEL_EPOCH + timedelta(days=int(value))).isoformat()


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Общая таблица строк. Её пишут не-питоновские генераторы, и без неё будут числа."""
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    out = []
    for item in root.findall(f"{_NS}si"):
        # Rich text: несколько кусков <r><t>…</t></r>, которые надо склеить. Взять
        # первый значило бы обрезать имя склада на первом же изменении начертания.
        out.append("".join(node.text or "" for node in item.iter(f"{_NS}t")))
    return out


def _sheet_path(archive: zipfile.ZipFile) -> str:
    names = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
    if not names:
        raise XlsxError(
            "в архиве нет ни одного листа (xl/worksheets/sheet*.xml). "
            f"Записей всего {len(archive.namelist())} — это не книга Excel."
        )
    return sorted(names)[0]


def rows(data: bytes) -> Iterator[list[object]]:
    """Прочитать первый лист построчно. Значения — строки, числа или `None`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxError(
            f"файл не является zip-архивом ({exc}); XLSX обязан им быть. "
            f"Первые байты: {data[:16]!r}"
        ) from exc

    shared = _shared_strings(archive)
    with archive.open(_sheet_path(archive)) as handle:
        for _, element in ElementTree.iterparse(handle, events=("end",)):
            if element.tag != f"{_NS}row":
                continue
            row: list[object] = []
            for cell in element.findall(f"{_NS}c"):
                position = column_index(cell.get("r") or "A1")
                while len(row) < position:
                    row.append(None)
                row.append(_cell_value(cell, shared))
            element.clear()
            yield row


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> object:
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(f"{_NS}is")
        return "".join(t.text or "" for t in node.iter(f"{_NS}t")) if node is not None else None
    value = cell.find(f"{_NS}v")
    if value is None or value.text is None:
        return None
    text = value.text
    if kind == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError) as exc:
            raise XlsxError(
                f"ссылка на общую строку {text!r} вне таблицы из {len(shared)} записей"
            ) from exc
    if kind in ("str", "e"):
        return text
    if kind == "b":
        return text == "1"
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def table(data: bytes, *, header_row: int = 0) -> tuple[list[str], list[list[object]]]:
    """Заголовок и строки. Пустые хвостовые строки отбрасываются.

    Заголовок возвращается отдельно и **не угадывается**: если в отчёте появится новая
    колонка или изменится порядок, вызывающий обязан это увидеть, а не разобрать файл
    по номерам позиций.
    """
    everything = list(rows(data))
    if len(everything) <= header_row:
        raise XlsxError(
            f"в листе {len(everything)} строк, заголовок ожидался в строке "
            f"{header_row + 1}. Пустой лист — не пустой отчёт, а нечитаемый файл."
        )
    header = [str(cell).strip() if cell is not None else "" for cell in everything[header_row]]
    width = len(header)
    body = []
    for row in everything[header_row + 1:]:
        if not any(cell is not None for cell in row):
            continue
        # ⚠️ Хвостовые пустые ячейки в XML просто отсутствуют, и строка приходит
        # короче заголовка. Без выравнивания загрузчик получил бы IndexError на
        # последней колонке — то есть отказ в разборе вместо пустого значения.
        body.append(list(row[:width]) + [None] * max(0, width - len(row)))
    return header, body
