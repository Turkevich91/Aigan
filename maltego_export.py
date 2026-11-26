"""Export explicitly supplied relationships for the Maltego table import wizard."""
from __future__ import annotations

import csv
from io import BytesIO, StringIO
import unicodedata

from telegram import Message


MAX_INPUT_CHARS = 4096
MAX_ROWS = 50
MAX_CELL_CHARS = 256
USAGE = (
    "Після /maltego вкажи по одному зв’язку в рядку у форматі "
    "«Джерело | Зв’язок | Ціль». Наприклад:\n"
    "/maltego Міський музей | видає | Каталог виставок"
)
IMPORT_GUIDE = (
    "У Maltego Graph Desktop відкрий Import a 3rd Party Table, вибери CSV та режим Manual. "
    "Познач Row is Headers. Зістав source і target із двома окремими сутностями "
    "(наприклад, Phrase). У Connectivity Graph створи зв’язок source → target. "
    "У Map Columns to Links зістав relationship із підписом цього зв’язку (Label)."
)


def build_maltego_csv(relationships: str) -> bytes:
    if len(relationships) > MAX_INPUT_CHARS:
        raise ValueError(f"Скороти текст до {MAX_INPUT_CHARS} символів.")
    output = StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(("source", "relationship", "target"))
    count = 0
    for line_number, line in enumerate(relationships.split("\n"), start=1):
        if not line.strip():
            continue
        count += 1
        if count > MAX_ROWS:
            raise ValueError(f"В одному файлі може бути до {MAX_ROWS} зв’язків.")
        cells = [part.strip() for part in line.split("|")]
        if len(cells) != 3 or not all(cells):
            raise ValueError(f"Рядок {line_number} має містити три значення через |.\n{USAGE}")
        for cell in cells:
            if len(cell) > MAX_CELL_CHARS:
                raise ValueError(f"Скороти значення в рядку {line_number} до {MAX_CELL_CHARS} символів.")
            if any(unicodedata.category(ch) == "Cc" for ch in cell):
                raise ValueError(f"Прибери керівні символи з рядка {line_number}.")
            # Reject formula prefixes rather than silently changing the identity of a graph node.
            prefix = unicodedata.normalize("NFKC", cell[0])
            if prefix.startswith(("=", "+", "-", "@")) or unicodedata.category(cell[0]) == "Cf":
                raise ValueError(
                    f"У рядку {line_number} почни значення зі слова: "
                    "початкові =, +, -, @ та невидимі знаки небезпечні в CSV."
                )
        writer.writerow(cells)
    if not count:
        raise ValueError(USAGE)
    return output.getvalue().encode("utf-8")


async def handle_maltego_export(message: Message) -> None:
    command = (message.text or "").split(maxsplit=1)
    try:
        data = build_maltego_csv(command[1] if len(command) == 2 else "")
    except ValueError as exc:
        await message.reply_text(str(exc), parse_mode=None, do_quote=True)
        return
    await message.reply_document(
        document=BytesIO(data), filename="maltego-relationships.csv",
        caption=IMPORT_GUIDE, parse_mode=None, do_quote=True,
    )
