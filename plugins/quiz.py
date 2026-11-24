import json
from plugins._common import plugin, safe_run

PLUGIN = plugin("quiz", "Generate a spoken quiz through REM's configured model and display it in the quiz panel, with scoring and answer notes.", {
    "topic": {"type": "STRING", "description": "Quiz topic"},
    "difficulty": {"type": "STRING", "description": "easy | medium | hard"},
    "count": {"type": "INTEGER", "description": "Number of questions from 1 to 10"},
}, ["topic"])

def _run(p, player, memory):
    topic = str(p.get("topic", "")).strip()
    count, difficulty = p.get("count", 5), str(p.get("difficulty", "medium")).lower()
    if not topic: return "Tell me the quiz topic."
    if not isinstance(count, int) or not 1 <= count <= 10: return "Question count must be from 1 to 10."
    if difficulty not in ("easy", "medium", "hard"): return "Difficulty must be easy, medium, or hard."
    from core import gemini
    data = gemini.as_json(f"Create {count} {difficulty} quiz questions about {topic!r}. Return JSON array of objects with question, options (3-4 strings), answer (one exact option), and note (short explanation).", tier=gemini.FAST)
    if not isinstance(data, list) or not data: return "The configured model could not generate a valid quiz."
    questions = []
    for q in data[:count]:
        if not isinstance(q, dict) or not all(k in q for k in ("question", "answer")): continue
        questions.append({"question": str(q["question"]), "options": [str(x) for x in q.get("options", [])], "answer": str(q["answer"]), "note": str(q.get("note", ""))})
    if not questions: return "The model response did not contain usable questions."
    show = getattr(player, "show_quiz", None) if player is not None else None
    if not callable(show): return json.dumps(questions, ensure_ascii=False)
    show(topic, questions)
    return f"Started a {len(questions)} question quiz about {topic} at {difficulty} difficulty."

run = safe_run(_run)
