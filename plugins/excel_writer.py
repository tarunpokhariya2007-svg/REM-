from pathlib import Path
import re
from plugins._common import plugin, safe_run

PLUGIN = plugin("excel_writer", "Create a workbook or edit an existing workbook by saving the changes as a separate output copy. The original is never overwritten; formulas and basic formatting are supported.", {
    "action": {"type": "STRING", "description": "create | edit (edit writes to a new output path)"},
    "path": {"type": "STRING", "description": "Output .xlsx path; must not already exist"},
    "source_path": {"type": "STRING", "description": "For edit, existing .xlsx workbook to copy and modify"},
    "sheet_name": {"type": "STRING", "description": "Worksheet name"},
    "headers": {"type": "ARRAY", "description": "Column header strings", "items": {"type": "STRING"}},
    "rows": {"type": "ARRAY", "description": "Rows as arrays of cell values", "items": {"type": "ARRAY", "items": {"type": "STRING"}}},
}, ["path"])

def _run(p, player, memory):
    try:
        from openpyxl import Workbook, load_workbook
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
    for row in rows:
        if not isinstance(row, list): return "Every row must be an array."
        for value in row:
            if isinstance(value, str) and value.startswith("="):
                safe_formula = re.fullmatch(r"=(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTA)\([A-Z0-9:$,() ]+\)|=[A-Z0-9$]+(?:[+*/-][A-Z0-9$]+)+", value, re.I)
                if not safe_formula: return "A workbook formula was refused because it uses unsupported or potentially unsafe syntax."
        ws.append(row[:len(headers)])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    if action == "create": ws.freeze_panes = "A2"
    if rows and action == "create":
        ref = f"A1:{ws.cell(ws.max_row, len(headers)).coordinate}"
        table = Table(displayName="REMData", ref=ref)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)
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
        if ws.title not in check.sheetnames or check[ws.title].max_row != initial_rows + len(rows): return "Workbook was written but validation failed; check the output file."
    finally: check.close()
    return f"Created and verified workbook at {path}."

run = safe_run(_run)
