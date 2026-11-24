from datetime import date, timedelta
from plugins._common import data_file, plugin, read_json, write_json, safe_run

PLUGIN = plugin("spaced_repetition", "Create flashcards and review due cards with a simple SM-2 spaced repetition schedule.", {
    "action": {"type": "STRING", "description": "add | due | grade | list"},
    "front": {"type": "STRING", "description": "Flashcard question or front"},
    "back": {"type": "STRING", "description": "Flashcard answer or back"},
    "card_id": {"type": "STRING", "description": "Card identifier"},
    "quality": {"type": "INTEGER", "description": "Recall quality from 0 to 5"},
}, ["action"])

def _run(p, player, memory):
    path = data_file("spaced_repetition.json"); cards = read_json(path, [])
    if not isinstance(cards, list): cards = []
    action = str(p["action"]).lower()
    if action == "add":
        front, back = str(p.get("front", "")).strip(), str(p.get("back", "")).strip()
        if not front or not back: return "A flashcard needs both front and back text."
        card = {"id": str(max((int(c.get("id", 0)) for c in cards), default=0) + 1), "front": front, "back": back, "repetitions": 0, "interval": 0, "ease": 2.5, "due": date.today().isoformat()}
        cards.append(card); write_json(path, cards); return f"Added flashcard {card['id']}."
    if action in ("due", "list"):
        selected = cards if action == "list" else [c for c in cards if c.get("due", "") <= date.today().isoformat()]
        return "No cards due." if not selected else "Cards: " + "; ".join(f"{c['id']}: {c['front']}" for c in selected[:30])
    if action == "grade":
        card = next((c for c in cards if str(c.get("id")) == str(p.get("card_id", ""))), None)
        if card is None: return "Card not found."
        q = p.get("quality")
        if not isinstance(q, int) or not 0 <= q <= 5: return "Quality must be an integer from 0 to 5."
        reps, ease = int(card.get("repetitions", 0)), float(card.get("ease", 2.5))
        if q < 3: reps, interval = 0, 1
        else:
            interval = 1 if reps == 0 else 6 if reps == 1 else round(float(card.get("interval", 6)) * ease)
            reps += 1
            ease = max(1.3, ease + (0.1 - (5-q)*(0.08 + (5-q)*0.02)))
        card.update(repetitions=reps, interval=interval, ease=ease, due=(date.today()+timedelta(days=interval)).isoformat())
        write_json(path, cards); return f"Recorded grade; card {card['id']} is due {card['due']}."
    return "Choose add, due, list, or grade."

run = safe_run(_run)
