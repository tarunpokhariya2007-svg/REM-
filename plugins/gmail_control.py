from email.message import EmailMessage
import base64
from plugins._common import plugin, safe_run
from plugins._google_oauth import service

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.send"]
PLUGIN = plugin("gmail_control", "Search and read Gmail, create drafts, or send only after explicit on-screen confirmation.", {
    "action": {"type": "STRING", "description": "search | read | draft | send"},
    "query": {"type": "STRING", "description": "Gmail search query or message ID"},
    "to": {"type": "STRING", "description": "Recipient email"},
    "subject": {"type": "STRING", "description": "Email subject"},
    "body": {"type": "STRING", "description": "Email content"},
}, ["action"])
PLUGIN_SETTINGS = {"namespace": "gmail_control", "title": "Gmail OAuth", "fields": [{"key": "client_secrets_file", "label": "OAuth client JSON path", "type": "text"}, {"key": "token_file", "label": "Token cache path", "type": "text"}]}

def _run(p, player, memory):
    action = str(p.get("action", "")).lower()
    if action not in ("search", "read", "draft", "send"): return "Choose search, read, draft, or send."
    api = service("gmail_control", "gmail", "v1", _SCOPES).users()
    if action == "search":
        q = str(p.get("query", "")).strip()
        if not q: return "Provide a Gmail search query."
        result = api.messages().list(userId="me", q=q, maxResults=10).execute()
        if not result.get("messages"): return "No matching messages."
        rows=[]
        for item in result["messages"]:
            message=api.messages().get(userId="me",id=item["id"],format="metadata",metadataHeaders=["From","Subject","Date"]).execute()
            headers={h["name"].lower():h["value"] for h in message.get("payload",{}).get("headers",[])}
            rows.append(f"{item['id']}: {headers.get('from','')} — {headers.get('subject','')} ({headers.get('date','')})")
        from core import outcome
        return outcome.untrusted("gmail_metadata", "Matching emails: " + "; ".join(rows))
    if action == "read":
        mid = str(p.get("query", "")).strip()
        if not mid or len(mid) > 200: return "Provide a message ID returned by search."
        msg = api.messages().get(userId="me", id=mid, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        def text_parts(part):
            out=[]
            if part.get("mimeType")=="text/plain" and part.get("body",{}).get("data"):
                out.append(base64.urlsafe_b64decode(part["body"]["data"]+"==").decode("utf-8",errors="replace"))
            for child in part.get("parts",[]): out.extend(text_parts(child))
            return out
        body="\n".join(text_parts(msg.get("payload",{}))) or msg.get("snippet","")
        from core import outcome
        return outcome.untrusted("gmail_message", f"From: {headers.get('from','')}\nSubject: {headers.get('subject','')}\nDate: {headers.get('date','')}\n\n{body[:12000]}")
    to, subject, body = (str(p.get(k, "")).strip() for k in ("to", "subject", "body"))
    if not to or "@" not in to or not subject or not body: return "Recipient, subject, and body are required."
    email = EmailMessage(); email["To"] = to; email["Subject"] = subject; email.set_content(body)
    raw = base64.urlsafe_b64encode(email.as_bytes()).decode()
    if action == "draft":
        created = api.drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return f"Created Gmail draft {created.get('id','')}; it was not sent."
    from core import confirm
    if confirm.pending_title(): return "Another confirmation is pending; no email was sent."
    def send():
        try:
            sent = api.messages().send(userId="me", body={"raw": raw}).execute()
            return f"Gmail API accepted message {sent.get('id','')}; delivery is not independently verified."
        except Exception as exc: return f"Gmail send failed: {type(exc).__name__}: {exc}"
    return confirm.request("gmail-send", f"Send email to {to} — {subject[:70]}?", "Review the recipient and exact email contents before confirming.", send)

run = safe_run(_run)
