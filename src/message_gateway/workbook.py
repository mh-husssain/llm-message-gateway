"""Generate a blank .xlsx workbook matching `message_gateway.schema`.

One worksheet per tab plus a leading Schema_README sheet. Each header cell
carries the column description as a comment, and the header row is frozen.

    python -m message_gateway.workbook [output.xlsx]

Requires the optional `workbook` extra (openpyxl).
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import schema

DEFAULT_OUTPUT = "gateway_sheet_blank.xlsx"


def generate(output: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    header_align = Alignment(horizontal="left", vertical="center", wrap_text=False)
    desc_align = Alignment(vertical="top", wrap_text=True)

    wb = Workbook()
    wb.remove(wb.active)

    readme = wb.create_sheet(title="Schema_README", index=0)
    for col, width in (("A", 26), ("B", 6), ("C", 28), ("D", 80)):
        readme.column_dimensions[col].width = width
    readme.cell(row=1, column=1, value="Sheet schema").font = Font(bold=True, size=12)
    readme.cell(
        row=1,
        column=2,
        value=(
            "Generated from message_gateway/schema.py. "
            "Regenerate: python -m message_gateway.workbook"
        ),
    )
    row = 3
    for tab in schema.ALL_TABS:
        readme.cell(row=row, column=1, value=tab.name).font = Font(
            bold=True, size=11, color="305496"
        )
        if tab.description:
            d = readme.cell(row=row, column=2, value=tab.description)
            readme.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            d.alignment = desc_align
        row += 1
        for label, col_idx in (("col", 1), ("letter", 2), ("name", 3), ("description", 4)):
            readme.cell(row=row, column=col_idx, value=label).font = Font(
                italic=True, color="555555"
            )
        row += 1
        for i, col in enumerate(tab.columns, start=1):
            readme.cell(row=row, column=1, value=i)
            readme.cell(row=row, column=2, value=col.letter)
            readme.cell(row=row, column=3, value=col.name)
            readme.cell(row=row, column=4, value=col.description).alignment = desc_align
            row += 1
        row += 1
    readme.freeze_panes = "A4"

    for tab in schema.ALL_TABS:
        ws = wb.create_sheet(title=tab.name)
        for i, col in enumerate(tab.columns, start=1):
            cell = ws.cell(row=1, column=i, value=col.name)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align
            if col.description:
                cell.comment = Comment(col.description, "Schema")
            ws.column_dimensions[get_column_letter(i)].width = max(len(col.name) + 2, 14)
        ws.freeze_panes = "A2"

    wb.save(output)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out = Path(args[0]) if args else Path(DEFAULT_OUTPUT)
    generate(out)
    print(f"Wrote {out.resolve()}")
    print(
        f"  {1 + len(schema.ALL_TABS)} sheets (Schema_README + {len(schema.ALL_TABS)} data tabs):"
    )
    for tab in schema.ALL_TABS:
        first = tab.description.split(".")[0] if tab.description else ""
        print(f"    {tab.name:<22} A:{tab.last_letter}  ({tab.num_cols} cols)  {first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
