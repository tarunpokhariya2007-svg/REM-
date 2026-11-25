"""Real Chrome/CDP acceptance against a controlled local page and temp profile."""

from __future__ import annotations



import json

import os

import subprocess

import tempfile

import threading

import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path



import requests



PROJECT_ROOT = Path(__file__).resolve().parents[2]

import sys

sys.path.insert(0, str(PROJECT_ROOT))

from actions import browser_control



CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")



HTML = b"""<!doctype html><html><head><title>REM local browser acceptance</title></head>

<body><h1>REM disposable browser test</h1>

<form role="search" action="/results" method="get">

 <label for="query">Test query</label><input id="query" name="query" type="search">

 <button type="submit">Run local search</button></form>

<button type="button">Ambiguous action</button><button type="button">Ambiguous action</button>

<button type="button" disabled>Disabled test control</button>

<main id="results"><h2>Local results</h2>

<a href="/result/1">First result</a>

<a href="/result/2">Second result</a>

<a href="/result/3">Third result</a></main>

<p id="status">Ready</p></body></html>"""





class Handler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path.startswith("/result/"):

            ordinal = self.path.rsplit("/", 1)[-1]

            body = (f"<!doctype html><title>REM result {ordinal}</title>"

                    f"<h1>Result {ordinal} opened</h1><a href='/'>Back to results</a>").encode()

        elif self.path.startswith("/results"):

            body = HTML.replace(b"Local results", b"Local search results")

        else:

            body = HTML

        self.send_response(200)

        self.send_header("Content-Type", "text/html; charset=utf-8")

        self.send_header("Content-Length", str(len(body)))

        self.end_headers()

        self.wfile.write(body)



    def log_message(self, *_args):

        return





def start_chrome(profile: Path):

    profile.mkdir(parents=True, exist_ok=True)

    active = profile / "DevToolsActivePort"

    proc = subprocess.Popen([

        str(CHROME), "--remote-debugging-port=0",

        "--remote-debugging-address=127.0.0.1",

        f"--user-data-dir={profile}", "--no-first-run",

        "--no-default-browser-check", "--disable-sync",

        "--disable-background-networking", "--disable-extensions",

        "--disable-crash-reporter", "--disable-breakpad",

        "about:blank",

    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    deadline = time.monotonic() + 20

    try:

        while time.monotonic() < deadline and proc.poll() is None:

            try:

                port = int(active.read_text(encoding="ascii").splitlines()[0])

                endpoint = f"http://127.0.0.1:{port}"

                requests.get(endpoint + "/json/version", timeout=0.5).raise_for_status()

                return proc, endpoint

            except Exception:

                time.sleep(0.1)

        return_code = proc.poll()

        stderr = b""

        if return_code is not None:

            try:

                _stdout, stderr = proc.communicate(timeout=1)

            except Exception:

                pass

        detail = stderr.decode("utf-8", errors="replace")[:1200]

        detail = detail.replace(str(profile), "<disposable-profile>")

        raise RuntimeError(

            "disposable Chrome did not expose its loopback CDP endpoint "

            f"(exit={return_code}; diagnostic={detail or 'no startup detail'})")

    except BaseException:

        # This helper owns the process until it returns. A failed probe must not

        # leave an untracked browser running against its temporary profile.

        stop_chrome(proc)

        raise





def stop_chrome(proc):

    if proc and proc.poll() is None:

        subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],

                       capture_output=True, check=False)

        try:

            proc.wait(timeout=5)

        except subprocess.TimeoutExpired:

            proc.kill()

            proc.wait(timeout=5)





def inspect_chrome_windows(proc, restore=False):

    """Inspect process ownership and restore only this disposable Chrome window."""

    import psutil

    import win32con

    import win32gui

    import win32process



    owned_pids = {proc.pid}

    try:

        owned_pids.update(child.pid for child in psutil.Process(proc.pid).children(recursive=True))

    except psutil.Error:

        pass

    windows = []

    def visit(hwnd, _):

        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)

        if pid in owned_pids:

            try:

                rect = win32gui.GetWindowRect(hwnd)

                windows.append({"hwnd": int(hwnd), "pid": pid,

                    "title": win32gui.GetWindowText(hwnd),

                    "class": win32gui.GetClassName(hwnd),

                    "visible": bool(win32gui.IsWindowVisible(hwnd)),

                    "iconic": bool(win32gui.IsIconic(hwnd)), "rect": list(rect)})

            except Exception:

                pass

        return True

    win32gui.EnumWindows(visit, None)

    foreground = win32gui.GetForegroundWindow()

    _thread, foreground_pid = win32process.GetWindowThreadProcessId(foreground)

    target_windows = [row for row in windows if row["class"] == "Chrome_WidgetWin_1"]

    if restore and target_windows:

        for row in target_windows:

            win32gui.ShowWindow(row["hwnd"], win32con.SW_RESTORE)

    return {"root_pid": proc.pid, "owned_pids": sorted(owned_pids),

            "foreground_hwnd": int(foreground), "foreground_pid": foreground_pid,

            "chrome_windows": windows}





def main():

    if not CHROME.is_file():

        raise RuntimeError("Chrome executable not found at the inspected system path")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    threading.Thread(target=server.serve_forever, daemon=True).start()

    local_url = f"http://127.0.0.1:{server.server_port}/"

    report = {"environment": "real Windows Chrome; disposable temp profile; local HTTP page",

              "network_scope": "loopback only", "steps": []}

    session = None

    chrome_procs = []

    try:

        with tempfile.TemporaryDirectory(prefix="rem-cdp-local-accept-") as temp:

            root = Path(temp)

            proc, endpoint = start_chrome(root / "profile-one")

            chrome_procs.append(proc)

            os.environ["REM_CHROME_CDP_URL"] = endpoint

            session = browser_control._BrowserSession("chrome")

            session.start()

            session.run(session._launch(), timeout=20)

            report["win32_before_restore"] = inspect_chrome_windows(proc)

            report["win32_restore_attempt"] = inspect_chrome_windows(proc, restore=True)

            report["cdp_attached"] = session.profile_kind == "attached"



            nav = session.run(session.go_to(local_url), timeout=35)

            page = session.run(session._get_page(), timeout=10)

            # CDP pages opened in a separate disposable Chrome process may be

            # background tabs. Give the controlled page a real foreground

            # window before exercising keyboard and hit-testing behavior.

            session.run(page.bring_to_front(), timeout=5)

            page_info = session.run(page.evaluate("""() => ({title: document.title,

                links: [...document.querySelectorAll('a')].map(a => a.innerText),

                queryInputs: document.querySelectorAll('input[type=search]').length})"""), timeout=10)

            report["navigation_and_dom_discovery"] = {

                "navigation_result": str(nav), "title": page_info["title"],

                "query_inputs": page_info["queryInputs"],

                "result_links": page_info["links"],

                "passed": page_info["title"] == "REM local browser acceptance"

                and page_info["queryInputs"] == 1 and len(page_info["links"]) == 3}



            search_type = session.run(session.smart_type(

                "search bar", "REM_SEARCHBAR_IDENTIFIER"), timeout=12)

            search_value = session.run(page.locator("#query").input_value(), timeout=8)

            report["generic_search_bar_identification"] = {

                "result": search_type,

                "value_readback_matches": search_value == "REM_SEARCHBAR_IDENTIFIER",

                "passed": ("status=done" in search_type

                           and "verified=true" in search_type

                           and search_value == "REM_SEARCHBAR_IDENTIFIER"),

            }



            fill = session.run(session.fill_form({"#query": "REM_LOCAL_CDP_TEST"}), timeout=12)

            report["form_fill_readback"] = {"result": fill,

                "passed": "verified: '#query'" in fill}

            report["search_input_state_before_enter"] = session.run(page.evaluate("""() => {

                const el = document.activeElement, r = el?.getBoundingClientRect();

                return {tag:el?.tagName, id:el?.id, type:el?.type,

                  value:el?.value, focused:document.hasFocus(),

                  rect:r ? [r.x,r.y,r.width,r.height] : null,

                  visibility:document.visibilityState, ready:document.readyState};

            }"""), timeout=8)

            target_session = session.run(page.context.new_cdp_session(page), timeout=5)

            target_identity = session.run(target_session.send("Target.getTargetInfo"), timeout=5)

            report["cdp_page_identity"] = {

                "target_id": target_identity.get("targetInfo", {}).get("targetId"),

                "url": target_identity.get("targetInfo", {}).get("url"),

                "context_page_count": len(session._context.pages),

                "document_visibility": report["search_input_state_before_enter"]["visibility"],

            }

            session.run(target_session.detach(), timeout=5)

            # The focused input is type=search, so Enter is a safe local form

            # submission under REM's browser-control guard.

            click = session.run(session.press("Enter"), timeout=15)

            submitted_page = session._page

            if submitted_page:

                try:

                    session.run(submitted_page.wait_for_url(

                        "**/results?query=REM_LOCAL_CDP_TEST", timeout=5_000), timeout=8)

                except Exception:

                    pass

            current_url = session.run(session.get_url(), timeout=8)

            report["local_form_submission"] = {
                "result": click,
                "url_contains_test_query": "query=REM_LOCAL_CDP_TEST" in current_url,
                "passed": (
                    "verified=true" in click
                    and "navigation_confirmed=true" in click
                    and "query=REM_LOCAL_CDP_TEST" in current_url
                ),
            }

            ordinal_rows = []

            for ordinal, number in (("First", 1), ("Second", 2), ("Third", 3)):

                session.run(session.go_to(local_url), timeout=35)

                session.run(page.bring_to_front(), timeout=5)

                link_state = session.run(page.locator(f"a[href='/result/{number}']").evaluate("""el => {

                    const r=el.getBoundingClientRect(), s=getComputedStyle(el);

                    return {text:el.innerText, rect:[r.x,r.y,r.width,r.height],

                      display:s.display, visibility:s.visibility, opacity:s.opacity,

                      documentFocused:document.hasFocus(), visibilityState:document.visibilityState};

                }"""), timeout=8)

                result = session.run(session.smart_click(f"{ordinal} result"), timeout=15)

                result_url = session.run(session.get_url(), timeout=8)

                body = session.run(session.get_text(), timeout=8)

                ordinal_rows.append({"requested": ordinal.lower(),

                                     "result": result,

                                     "url_matches": result_url.endswith(f"/result/{number}"),

                                     "content_matches": f"Result {number} opened" in body,

                                     "element_state_before": link_state,

                                     "passed": "verified page state changed" in result

                                     and result_url.endswith(f"/result/{number}")

                                     and f"Result {number} opened" in body})

            report["ordinal_selection"] = ordinal_rows



            session.run(session.go_to(local_url), timeout=35)

            missing = session.run(session.smart_click("Missing result"), timeout=12)

            missing_url = session.run(session.get_url(), timeout=8)

            report["ambiguous_or_missing_target"] = {

                "result": missing, "url_unchanged": missing_url == local_url,

                "passed": "Could not find element" in missing and missing_url == local_url}



            ambiguous = session.run(session.smart_click("Ambiguous action"), timeout=12)

            disabled = session.run(session.smart_click("Disabled test control"), timeout=12)

            report["ambiguous_and_disabled_target_refusal"] = {

                "ambiguous_result": ambiguous,

                "ambiguous_refused": "Multiple" in ambiguous,

                "disabled_result": disabled,

                "disabled_refused": "not visible and enabled" in disabled,

            }

            report["ambiguous_and_disabled_target_refusal"]["passed"] = (

                report["ambiguous_and_disabled_target_refusal"]["ambiguous_refused"]

                and report["ambiguous_and_disabled_target_refusal"]["disabled_refused"])



            session.run(session.new_tab(local_url), timeout=15)

            new_tab_url = session.run(session.get_url(), timeout=8)

            closed_tab = session.run(session.close_tab(), timeout=10)

            active_after_close = session.run(session.get_url(), timeout=8)

            report["tab_close_recovery"] = {

                "new_tab_opened": new_tab_url == local_url,

                "close_result": closed_tab,

                "active_page_recovered": active_after_close.startswith(local_url),

                "passed": new_tab_url == local_url and active_after_close.startswith(local_url)}



            # Close the selected tab out-of-band, then exercise REM's own page recovery.

            session.run(session._page.close(), timeout=10)

            recovered_text = session.run(session.get_text(), timeout=10)

            report["closed_active_tab_recovery"] = {

                "recovered": session.run(session.get_url(), timeout=8) == "about:blank",

                "read_text_result": recovered_text[:80]}

            report["closed_active_tab_recovery"]["passed"] = report["closed_active_tab_recovery"]["recovered"]



            # Simulate a browser/context loss and reconnect the same REM session

            # to a fresh disposable Chrome endpoint.

            stop_chrome(proc)

            proc, endpoint = start_chrome(root / "profile-two")

            chrome_procs.append(proc)

            os.environ["REM_CHROME_CDP_URL"] = endpoint

            nav = session.run(session.go_to(local_url), timeout=35)

            report["browser_context_disconnect_reconnect"] = {

                "result": str(nav), "reattached": session.profile_kind == "attached",

                "url_matches": session.run(session.get_url(), timeout=8) == local_url,

                "passed": session.profile_kind == "attached"

                and session.run(session.get_url(), timeout=8) == local_url}

            # Tear down the CDP connection and every owned Chrome process before

            # TemporaryDirectory attempts to remove the disposable profile.

            session.close()

            session = None

            for owned_proc in chrome_procs:

                stop_chrome(owned_proc)

            chrome_procs.clear()

        report["all_passed"] = (report["cdp_attached"]

            and report["navigation_and_dom_discovery"]["passed"]

            and report["generic_search_bar_identification"]["passed"]

            and report["form_fill_readback"]["passed"]

            and report["local_form_submission"]["passed"]

            and all(row["passed"] for row in report["ordinal_selection"])

            and report["ambiguous_or_missing_target"]["passed"]

            and report["ambiguous_and_disabled_target_refusal"]["passed"]

            and report["tab_close_recovery"]["passed"]

            and report["closed_active_tab_recovery"]["passed"]

            and report["browser_context_disconnect_reconnect"]["passed"])

    finally:

        if session:

            try:

                session.close()

            except Exception:

                pass

        for proc in chrome_procs:

            stop_chrome(proc)

        os.environ.pop("REM_CHROME_CDP_URL", None)

        server.shutdown()

        server.server_close()

    print(json.dumps(report, indent=2))

    return report





if __name__ == "__main__":

    result = main()

    raise SystemExit(0 if result.get("all_passed") else 1)
