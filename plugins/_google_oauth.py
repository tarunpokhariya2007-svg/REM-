"""Lazy OAuth helpers shared by Gmail, Calendar, and YouTube upload."""
from pathlib import Path


def service(namespace: str, api: str, version: str, scopes: list[str]):
    from plugins._common import config
    cfg = config(namespace)
    client_file = str(cfg.get("client_secrets_file", "")).strip()
    token_file = str(cfg.get("token_file", "")).strip()
    if not client_file or not Path(client_file).is_file():
        raise RuntimeError(f"Configure a valid OAuth client_secrets_file in {namespace} plugin settings.")
    if not token_file:
        raise RuntimeError(f"Configure a token_file path in {namespace} plugin settings.")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google API support requires google-api-python-client and google-auth-oauthlib.") from exc
    creds = None
    path = Path(token_file).expanduser()
    if path.is_file():
        creds = Credentials.from_authorized_user_file(str(path), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(client_file, scopes)
        creds = flow.run_local_server(port=0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(creds.to_json(), encoding="utf-8")
    return build(api, version, credentials=creds, cache_discovery=False)
