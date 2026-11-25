import json
import re
import unicodedata
from plugins._common import plugin, safe_run

PLUGIN = plugin("quiz", "Generate a spoken quiz through REM's configured model and display it in the quiz panel, with scoring and answer notes.", {
    "action": {"type": "STRING", "description": "start | stop"},
    "topic": {"type": "STRING", "description": "Quiz topic"},
    "difficulty": {"type": "STRING", "description": "easy | medium | hard"},
    "count": {"type": "INTEGER", "description": "Number of questions from 1 to 10"},
    "questions": {"type": "ARRAY", "description": "Optional questions already available in the conversation (for example from a document); when supplied, no question-generation request is made.", "items": {"type": "OBJECT", "properties": {
        "type": {"type": "STRING", "description": "multiple_choice | true_false | fill_blank | open"},
        "question": {"type": "STRING"},
        "options": {"type": "ARRAY", "items": {"type": "STRING"}},
        "answer": {"type": "STRING"},
        "note": {"type": "STRING"},
    }, "required": ["question", "answer"]}},
}, [])

_MAX_QUESTIONS = 10
_MAX_TEXT = 1200

def _norm(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return " ".join(text.split())

def _clean_questions(raw):
    if not isinstance(raw, list): return []
    out = []
    for item in raw[:_MAX_QUESTIONS]:
        if not isinstance(item, dict): continue
        question = str(item.get("question") or "").strip()[:_MAX_TEXT]
        answer = str(item.get("answer") or "").strip()[:_MAX_TEXT]
        if not question or not answer: continue
        options, seen = [], set()
        for value in item.get("options") or []:
            option = str(value or "").strip()[:300]
            norm = _norm(option)
            if option and norm not in seen:
                seen.add(norm); options.append(option)
        qtype = str(item.get("type") or ("multiple_choice" if options else "open"))
        if qtype not in {"multiple_choice", "true_false", "fill_blank", "open"}:
            qtype = "multiple_choice" if options else "open"
        if qtype == "multiple_choice" and len(options) < 2: qtype = "open"
        if qtype == "true_false" and len(options) != 2: qtype, options = "open", []
        if qtype not in {"multiple_choice", "true_false"}: options = []
        if options and _norm(answer) not in {_norm(o) for o in options}:
            token = _norm(answer)
            index = ord(token) - ord("a") if len(token) == 1 and "a" <= token <= "z" else (int(token) - 1 if token.isdigit() else -1)
            if 0 <= index < len(options): answer = options[index]
        out.append({"type": qtype, "question": question, "options": options,
                    "answer": answer, "note": str(item.get("note") or "").strip()[:_MAX_TEXT]})
    return out

def _grade(question, given):
    given = str(given or "").strip()
    answer = str(question.get("answer") or "")
    if not given: return False
    if question.get("type") in {"open", "fill_blank"}: return None
    if _norm(given) == _norm(answer): return True
    if question.get("type") == "true_false":
        truth = {"true": True, "t": True, "1": True, "yes": True,
                 "false": False, "f": False, "0": False, "no": False}
        a, b = truth.get(_norm(given)), truth.get(_norm(answer))
        return a == b if a is not None and b is not None else False
    return False

def _run(p, player, memory):
    topic = str(p.get("topic", "")).strip()
    action = str(p.get("action", "start")).strip().lower()
    if action == "stop":
        hide = getattr(player, "hide_quiz", None) if player is not None else None
        if callable(hide): hide()
        return "Quiz cleared."
    if action != "start": return "Choose start or stop."
    count, difficulty = p.get("count", 5), str(p.get("difficulty", "medium")).lower()
    provided = p.get("questions")
    if provided is not None:
        questions = _clean_questions(provided)
        if not questions: return "The supplied questions were not usable; no quiz was started."
        topic = topic or "Quiz"
    else:
        if not topic: return "Tell me the quiz topic or provide questions already in the conversation."
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10: return "Question count must be from 1 to 10."
        if difficulty not in ("easy", "medium", "hard"): return "Difficulty must be easy, medium, or hard."
        from core import gemini
        data = gemini.as_json(f"Create {count} {difficulty} quiz questions about {topic!r}. Return JSON array of objects with question, options (3-4 strings), answer (one exact option), and note (short explanation).", tier=gemini.FAST)
        questions = _clean_questions(data)
        if not questions: return "The configured model could not generate a valid quiz."
    show = getattr(player, "show_quiz", None) if player is not None else None
    if not callable(show): return json.dumps(questions, ensure_ascii=False)
    show(topic, questions, _grade)
    return f"Started a {len(questions)} question quiz about {topic} at {difficulty} difficulty."

run = safe_run(_run)
