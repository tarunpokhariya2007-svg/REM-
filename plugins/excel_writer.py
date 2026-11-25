from pathlib import Path
import re
from plugins._common import plugin, safe_run

PLUGIN = plugin("excel_writer", "Create a workbook or edit an existing workbook by saving the changes as a separate output copy. The original is never overwritten; safe totals, basic formatting, and optional charts are supported.", {
    "action": {"type": "STRING", "description": "create | edit (edit writes to a new output path)"},
    "path": {"type": "STRING", "description": "Output .xlsx path; must not already exist"},
    "source_path": {"type": "STRING", "description": "For edit, existing .xlsx workbook to copy and modify"},
    "sheet_name": {"type": "STRING", "description": "Worksheet name"},
    "headers": {"type": "ARRAY", "description": "Column header strings", "items": {"type": "STRING"}},
    "rows": {"type": "ARRAY", "description": "Rows as arrays of cell values", "items": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "total_columns": {"type": "ARRAY", "description": "Optional zero-based numeric columns to total in a final row", "items": {"type": "INTEGER"}},
    "chart_type": {"type": "STRING", "description": "Optional chart type: bar | line | pie"},
    "chart_category_column": {"type": "INTEGER", "description": "Zero-based category column for chart"},
    "chart_value_column": {"type": "INTEGER", "description": "Zero-based numeric value column for chart"},
}, ["path"])

def _run(p, player, memory):
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.chart import BarChart, LineChart, PieChart, Reference
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.table import Table, TableStyleInfo
    except ImportError:
        return "Excel creation requires the optional openpyxl package."
    path = Path(str(p.get("path", ""))).expanduser()
    action = str(p.get("action", "create")).lower()
    if action not in ("create", "edit"): return "Choose create or edit."
    if path.suffix.lower() != ".xlsx": return "Output path must end in .xlsx."
    if path.exists(): return "That workbook already exists; it was not overwritten. Choose a new filename."
    headers, rows = p.get("headers", []), p.get("rows", [])
    if action == "create" and (not isinstance(headers, list) or not headers or len(headers) > 100): return "Provide between 1 and 100 column headers."
    if not isinstance(rows, list) or len(rows) > 10000: return "Rows must be a list with no more than 10,000 entries."
    if action == "create":
        wb = Workbook(); ws = wb.active
        ws.title = str(p.get("sheet_name", "Sheet1"))[:31] or "Sheet1"
        headers = [str(v).strip()[:255] for v in headers]
        if any(not h for h in headers) or len({h.casefold() for h in headers}) != len(headers): return "Headers must be non-empty and unique."
        ws.append(headers)
    else:
        source = Path(str(p.get("source_path", ""))).expanduser()
        if not source.is_file() or source.suffix.lower() != ".xlsx": return "Edit requires an existing .xlsx source_path."
        wb = load_workbook(source)
        sheet = str(p.get("sheet_name", wb.active.title))[:31]
        ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet or "Sheet1")
        existing_headers = [ws.cell(1,c).value for c in range(1,ws.max_column+1)] if ws.max_row else []
        if not headers: headers = existing_headers
        if not headers: return "The selected worksheet has no header row; provide headers before appending data."
        if existing_headers and any(value is not None for value in existing_headers) and [str(h) for h in headers] != [str(h) for h in existing_headers]:
            return "Provided headers do not match the worksheet's existing columns."
        if not existing_headers or not any(value is not None for value in existing_headers): ws.append([str(v).strip()[:255] for v in headers])
        if len(headers) > 100: return "A worksheet may have at most 100 supplied columns."
    initial_rows = ws.max_row
    numeric_columns = {i for i in p.get("total_columns", [])
                       if isinstance(i, int) and not isinstance(i, bool)
                       and 0 <= i < len(headers)}
    chart_value_hint = p.get("chart_value_column")
    if isinstance(chart_value_hint, int) and not isinstance(chart_value_hint, bool) \
            and 0 <= chart_value_hint < len(headers):
        numeric_columns.add(chart_value_hint)
    for row in rows:
        if not isinstance(row, list): return "Every row must be an array."
        for value in row:
            if isinstance(value, str) and value.startswith("="):
                safe_formula = re.fullmatch(r"=(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTA)\([A-Z0-9:$,() ]+\)|=[A-Z0-9$]+(?:[+*/-][A-Z0-9$]+)+", value, re.I)
                if not safe_formula: return "A workbook formula was refused because it uses unsupported or potentially unsafe syntax."
        values = row[:len(headers)]
        # Rows arrive as strings from the tool schema. Convert only plain
        # numeric literals so Excel charts and SUM formulas treat them as data.
        converted = []
        for col_index, value in enumerate(values):
            if (col_index in numeric_columns and isinstance(value, str)
                    and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip())):
                number = float(value) if "." in value else int(value)
                converted.append(number)
            else:
                converted.append(value)
        ws.append(converted)
    last_data_row = ws.max_row
    total_columns = p.get("total_columns", [])
    if total_columns:
        if not isinstance(total_columns, list) or any(
                isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(headers)
                for i in total_columns):
            return "total_columns must contain valid zero-based column numbers."
        total_columns = sorted(set(total_columns))
        if last_data_row >= 2:
            total_row = last_data_row + 1
            ws.cell(total_row, 1, "TOTAL")
            for index in total_columns:
                col = ws.cell(1, index + 1).column_letter
                ws.cell(total_row, index + 1, f"=SUM({col}2:{col}{last_data_row})")
                ws.cell(total_row, index + 1).font = Font(bold=True)
            last_data_row = total_row
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    if action == "create": ws.freeze_panes = "A2"
    if rows and action == "create":
        data_end = ws.max_row - (1 if total_columns and ws.max_row >= 2 else 0)
        ref = f"A1:{ws.cell(data_end, len(headers)).coordinate}"
        table = Table(displayName="REMData", ref=ref)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)
    chart_type = str(p.get("chart_type", "") or "").lower()
    if chart_type:
        charts = {"bar": BarChart, "line": LineChart, "pie": PieChart}
        if chart_type not in charts:
            return "chart_type must be bar, line, or pie."
        category = p.get("chart_category_column", 0)
        value = p.get("chart_value_column", 1)
        if (isinstance(category, bool) or not isinstance(category, int) or
                isinstance(value, bool) or not isinstance(value, int) or
                not 0 <= category < len(headers) or not 0 <= value < len(headers)):
            return "Chart category and value columns must be valid zero-based columns."
        data_end = ws.max_row - (1 if total_columns and ws.max_row >= 2 else 0)
        if data_end < 2:
            return "A chart needs at least one data row."
        chart = charts[chart_type]()
        chart.title = str(p.get("sheet_name", ws.title))[:80] or ws.title
        chart.add_data(Reference(ws, min_col=value + 1, max_col=value + 1,
                                 min_row=1, max_row=data_end), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=category + 1,
                                       min_row=2, max_row=data_end))
        ws.add_chart(chart, "J2")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create protects against a file appearing after the initial check.
    try:
        with path.open("xb") as raw: wb.save(raw)
    except Exception:
        try: path.unlink()
        except OSError: pass
        raise
    check = load_workbook(path, read_only=True, data_only=False)
    try:
        expected_rows = initial_rows + len(rows)
        if total_columns and expected_rows >= 2:
            expected_rows += 1
        if ws.title not in check.sheetnames or check[ws.title].max_row != expected_rows: return "Workbook was written but validation failed; check the output file."
    finally: check.close()
    return f"Created and verified workbook at {path}."

run = safe_run(_run)
