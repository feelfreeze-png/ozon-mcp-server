"""Разбор чисел из ответов Ozon. Один парсер на все методы.

**Почему один.** Разделитель дробной части у Ozon не постоянен: в
`statistics/products/sku` это точка (`"1256.36"`), в остальных методах статистики —
запятая (`"2255,19"`). Два парсера рядом означают, что однажды вызовут не тот, и
получится не ошибка, а другое число.

🔴 **Неоднозначность не угадывается.** `"1,234"` — это 1.234 или 1234? Разница в тысячу
раз, и обе величины в отчёте о расходе выглядят правдоподобно: такая ошибка не всплывёт
ни на какой проверке. Поэтому ровно этот случай — отказ, а не выбор наугад. Рублёвые
суммы приходят с двумя знаками после разделителя, так что до живых данных он не дотянется.
"""

from __future__ import annotations

import re

#: Пробелы всех видов, включая неразрывный и узкий неразрывный.
_SPACES = dict.fromkeys(map(ord, " \t   "), None)

_AMBIGUOUS = re.compile(r"^-?\d{1,3},\d{3}$")


class AmbiguousNumberError(ValueError):
    """Строку нельзя прочитать однозначно: разница была бы в тысячу раз."""


def parse_number(value: object, *, field: str = "") -> float | None:
    """Прочитать число из ответа Ozon. `None` — значения нет.

    Принимает обе формы разделителя. Пустая строка и `None` дают `None` — это «поля
    нет», а не ноль: ноль означал бы «расхода не было», и слить их нельзя.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field or 'значение'}: булево вместо числа")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"{field or 'значение'}: {type(value).__name__} вместо числа")

    text = value.translate(_SPACES)
    if not text:
        return None

    if _AMBIGUOUS.match(text):
        raise AmbiguousNumberError(
            f"{field or 'значение'}={value!r}: запятая перед ровно тремя цифрами — "
            "это может быть и дробная часть, и разделитель тысяч. Разница в тысячу раз, "
            "и обе величины выглядят правдоподобно, поэтому выбирать наугад нельзя."
        )

    dot, comma = text.rfind("."), text.rfind(",")
    if dot >= 0 and comma >= 0:
        # Обе формы сразу: разделитель дробной части — последний, первый разделяет тысячи.
        if comma > dot:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif comma >= 0:
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{field or 'значение'}={value!r}: не число") from exc


def parse_int(value: object, *, field: str = "") -> int | None:
    """Целое из ответа. Дробное значение — отказ, а не молчаливое отбрасывание хвоста."""
    number = parse_number(value, field=field)
    if number is None:
        return None
    if number != int(number):
        raise ValueError(f"{field or 'значение'}={value!r}: ожидалось целое")
    return int(number)
