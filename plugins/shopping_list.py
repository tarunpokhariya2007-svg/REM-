from plugins._common import data_file, plugin, read_json, write_json, safe_run

PLUGIN = plugin("shopping_list", "Manage REM's local shopping list: add one or comma-separated items, list them, mark purchased items as bought, remove items, or clear all/bought entries.", {
    "action": {"type": "STRING", "description": "add | list | check | remove | clear"},
    "item": {"type": "STRING", "description": "One shopping item (legacy singular form)"},
    "items": {"type": "STRING", "description": "One item or comma-separated item names"},
    "quantity": {"type": "STRING", "description": "Quantity or amount"},
    "category": {"type": "STRING", "description": "Optional category"},
    "which": {"type": "STRING", "description": "For clear: all (default) or done (checked items only)"},
}, ["action"])

def _parse_items(value):
    seen, out = set(), []
    for candidate in str(value or "").replace(";", ",").replace("/", ",").split(","):
        name = candidate.strip().strip(".")[:60]
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out

def _run(p, player, memory):
    path = data_file("shopping_list.json")
    rows = read_json(path, [])
    if not isinstance(rows, list): rows = []
    action = str(p["action"]).lower()
    items = _parse_items(p.get("items") or p.get("item"))
    # Migrate the reconstructed {item, quantity, category} rows in place to
    # the original feature's {name, qty, done} shape without dropping fields.
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            entry = dict(row)
            entry["name"] = str(entry.get("name") or entry.get("item") or "")[:60]
            entry["qty"] = str(entry.get("qty") or entry.get("quantity") or "")[:20]
            entry["done"] = bool(entry.get("done", False))
            entry.pop("item", None); entry.pop("quantity", None)
            if entry["name"]: normalized.append(entry)
    rows = normalized
    if action == "add":
        if not items: return "Name the item or items to add. Separate multiple items with commas."
        added, already = [], []
        for name in items:
            match = next((r for r in rows if r["name"].casefold() == name.casefold()), None)
            if match:
                already.append(match["name"]); continue
            qty = p.get("quantity", "1") if len(items) == 1 else ""
            rows.append({"name": name, "qty": str(qty)[:20],
                         "category": str(p.get("category", ""))[:40], "done": False})
            added.append(name)
        write_json(path, rows)
        parts = ([f"Added {', '.join(added)}."] if added else [])
        if already: parts.append(f"Already on the list: {', '.join(already)}.")
        return " ".join(parts)
    if action == "list":
        if not rows: return "Shopping list is empty."
        return "Shopping list: " + "; ".join(("[bought] " if r["done"] else "") +
            (f"{r['qty']} " if r["qty"] else "") + r["name"] +
            (f" ({r['category']})" if r.get("category") else "") for r in rows)
    if action == "remove":
        if not items: return "Name the item or items to remove."
        removed, missing = [], []
        for name in items:
            match = next((r for r in rows if r["name"].casefold() == name.casefold()), None)
            if match: rows.remove(match); removed.append(name)
            else: missing.append(name)
        write_json(path, rows)
        return (f"Removed {', '.join(removed)}." if removed else "No matching items were found.") + (
            f" Not found: {', '.join(missing)}." if missing else "")
    if action in ("check", "done", "bought"):
        if not items: return "Name the item or items you bought."
        checked, missing = [], []
        for name in items:
            match = next((r for r in rows if r["name"].casefold() == name.casefold()), None)
            if match: match["done"] = True; checked.append(match["name"])
            else: missing.append(name)
        write_json(path, rows)
        return (f"Marked {', '.join(checked)} as bought." if checked else "No matching items were found.") + (
            f" Not found: {', '.join(missing)}." if missing else "")
    if action == "clear":
        which = str(p.get("which", "all")).lower()
        if which == "done":
            before = len(rows); rows = [r for r in rows if not r["done"]]
            write_json(path, rows); return f"Cleared {before-len(rows)} bought item(s)."
        if which != "all": return "For clear, choose which=all or which=done."
        write_json(path, []); return "Cleared the shopping list."
    return "Choose add, list, check, remove, or clear."

run = safe_run(_run)
