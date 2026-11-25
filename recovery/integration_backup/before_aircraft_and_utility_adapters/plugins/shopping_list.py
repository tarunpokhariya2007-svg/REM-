from plugins._common import data_file, plugin, read_json, write_json, safe_run

PLUGIN = plugin("shopping_list", "Create and manage REM's local shopping list.", {
    "action": {"type": "STRING", "description": "add | list | remove | clear"},
    "item": {"type": "STRING", "description": "Shopping item"},
    "quantity": {"type": "STRING", "description": "Quantity or amount"},
    "category": {"type": "STRING", "description": "Optional category"},
}, ["action"])

def _run(p, player, memory):
    path = data_file("shopping_list.json")
    rows = read_json(path, [])
    if not isinstance(rows, list): rows = []
    action, item = str(p["action"]).lower(), str(p.get("item", "")).strip()
    if action == "add":
        if not item: return "Name the item to add."
        rows.append({"item": item, "quantity": str(p.get("quantity", "1")), "category": str(p.get("category", ""))})
        write_json(path, rows); return f"Added {item} to the shopping list."
    if action == "list":
        return "Shopping list is empty." if not rows else "Shopping list: " + "; ".join(f"{r.get('quantity','1')} {r.get('item','')}" + (f" ({r['category']})" if r.get('category') else "") for r in rows)
    if action == "remove":
        if not item: return "Name the item to remove."
        updated = [r for r in rows if str(r.get("item", "")).casefold() != item.casefold()]
        if len(updated) == len(rows): return f"{item} was not on the list."
        write_json(path, updated); return f"Removed {item}."
    if action == "clear":
        write_json(path, []); return "Cleared the shopping list."
    return "Choose add, list, remove, or clear."

run = safe_run(_run)
