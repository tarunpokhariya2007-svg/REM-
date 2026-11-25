"""Run REM's YouTube ordinal selector in a disposable Chrome profile only."""
from __future__ import annotations

import json
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from actions import browser_control

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
RESULTS = "https://www.youtube.com/results?search_query=React+project+tutorial"


def run():
    if not CHROME.is_file():
        raise RuntimeError("Chrome executable not found at the inspected system path")
    with tempfile.TemporaryDirectory(prefix="rem-disposable-chrome-") as temp:
        profile = Path(temp) / "profile"
        port_file = profile / "DevToolsActivePort"
        profile.mkdir()
        proc = subprocess.Popen([
            str(CHROME), "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-sync",
            "--disable-background-networking", "--disable-extensions",
            "--force-renderer-accessibility", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        session = None
        report = {"profile_is_temporary": True, "profile_path": str(profile),
                  "steps": []}
        try:
            deadline = time.monotonic() + 20
            port = None
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    port = int(port_file.read_text(encoding="ascii").splitlines()[0])
                    requests.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5).raise_for_status()
                    break
                except Exception:
                    time.sleep(0.1)
            if not port:
                raise RuntimeError("Chrome did not expose its loopback CDP endpoint")

            endpoint = f"http://127.0.0.1:{port}"
            os.environ["REM_CHROME_CDP_URL"] = endpoint
            session = browser_control._BrowserSession("chrome")
            session.start()
            session.run(session._launch(), timeout=15)
            report["cdp_attached"] = session.profile_kind == "attached"
            report["endpoint_host"] = "127.0.0.1"

            async def navigate_results():
                page = await session._get_page()
                await page.goto(RESULTS, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.locator("ytd-video-renderer, ytd-rich-item-renderer").first.wait_for(
                        state="visible", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                return await page.title(), page.url

            title, url = session.run(navigate_results(), timeout=45)
            report["results_title"] = title
            report["results_url"] = url
            try:
                from pywinauto import Desktop
                ui_windows = Desktop(backend="uia").windows()
                chrome_window = next((w for w in ui_windows
                                      if "google chrome" in w.window_text().casefold()), None)
                uia_report = {"desktop_initialized": True,
                              "visible_windows_count": len(ui_windows),
                              "chrome_window_found": chrome_window is not None,
                              "chrome_window_title": chrome_window.window_text()
                              if chrome_window else ""}
                if chrome_window:
                    try:
                        descendants = chrome_window.descendants()
                        names = [str(element.window_text()) for element in descendants
                                 if element.window_text()]
                        uia_report["descendant_count"] = len(descendants)
                        uia_report["sample_named_elements"] = names[:20]
                    except Exception as exc:
                        uia_report["descendants_error"] = f"{type(exc).__name__}: {exc}"
                report["uia_in_chrome"] = uia_report
            except Exception as exc:
                report["uia_in_chrome"] = {"desktop_initialized": False,
                                           "error": f"{type(exc).__name__}: {exc}"}

            async def card_inventory():
                page = await session._get_page()
                return await page.locator("ytd-video-renderer, ytd-rich-item-renderer").evaluate_all("""els => {
                  const seen = new Set();
                  return els.flatMap(card => {
                    const r=card.getBoundingClientRect(), s=getComputedStyle(card);
                    if(r.width<2||r.height<2||s.display==='none'||s.visibility==='hidden'||
                       card.hasAttribute('is-ad')||card.querySelector('ytd-ad-slot-renderer,#ad-badge,[id*=ad-badge]')) return [];
                    const a=card.querySelector('a#video-title[href*=\"/watch\"]')||
                      card.querySelector('a[href*=\"/watch?v=\"]:not([aria-hidden=\"true\"])');
                    if(!a||a.getBoundingClientRect().width<2) return [];
                    const u=new URL(a.href,location.href), id=u.pathname==='/watch'?u.searchParams.get('v'):null;
                    const titleAttr=(a.getAttribute('title')||'').trim();
                    const aria=(a.getAttribute('aria-label')||'').trim();
                    const formatted=(card.querySelector('yt-formatted-string#video-title')?.innerText||'').trim();
                    const visible=(a.innerText||a.textContent||'').trim();
                    const title=titleAttr||formatted||aria||visible;
                    if(!id||!title||seen.has(id)) return []; seen.add(id);
                    return [{id,title,title_attr:titleAttr,aria_label:aria,formatted_text:formatted,visible_text:visible}];
                  });
                }""")

            initial = session.run(card_inventory(), timeout=20)
            report["initial_visible_non_ad_cards"] = initial[:5]
            report["ordinal_results"] = []
            if _args.inspect_only:
                async def inspect_card_markup():
                    page = await session._get_page()
                    return await page.locator("ytd-video-renderer, ytd-rich-item-renderer").evaluate_all("""els => {
                      const rows=[];
                      for(const card of els){
                        const r=card.getBoundingClientRect(); if(r.width<2||r.height<2) continue;
                        const links=[...card.querySelectorAll('a[href*=\"watch?v=\"]')].slice(0,8).map(a=>({
                          id:a.id,href:a.href,title:a.getAttribute('title'),aria:a.getAttribute('aria-label'),
                          text:a.innerText,textContent:a.textContent,
                          html:a.outerHTML.slice(0,700)
                        }));
                        rows.push({tag:card.tagName,className:String(card.className).slice(0,180),links});
                        if(rows.length>=3) break;
                      }
                      return rows;
                    }""")
                report["card_markup_diagnostics"] = session.run(inspect_card_markup(), timeout=20)
                report["live_selector_status"] = "inspect_only_no_click"
                return report
            if not initial:
                report["live_selector_status"] = "blocked_or_empty_results"
                report["live_failure_context"] = "No visible video cards; no click attempted."
            else:
                for ordinal in ("first", "second", "third"):
                    if len(initial) <= ("first", "second", "third").index(ordinal):
                        report["ordinal_results"].append({"requested": ordinal,
                            "selected_id": None, "resulting_url": None,
                            "verified": False, "reason": "insufficient visible cards; no click"})
                        continue
                    intent = initial[("first", "second", "third").index(ordinal)]
                    result = session.run(session.smart_click(f"click the {ordinal} video"), timeout=25)
                    # Resolve the async page URL on the owning Playwright loop.
                    resulting_url = session.run(_page_url(session), timeout=5)
                    page_url = resulting_url
                    async def selected_page_title():
                        return await (await session._get_page()).title()
                    selected_title = session.run(selected_page_title(), timeout=5)
                    from urllib.parse import parse_qs, urlsplit
                    selected_id = parse_qs(urlsplit(page_url).query).get("v", [""])[0]
                    verified = urlsplit(page_url).path == "/watch" and selected_id == intent["id"]
                    report["ordinal_results"].append({"requested": ordinal,
                        "intended_title": intent["title"], "intended_id": intent["id"],
                        "tool_result": result, "selected_page_title": selected_title,
                        "selected_id": selected_id,
                        "resulting_url": page_url, "verified": verified})
                    if not verified:
                        break
                    title, url = session.run(navigate_results(), timeout=45)
                    initial = session.run(card_inventory(), timeout=20)

            # Missing-target case on an about:blank disposable tab; this must
            # refuse without any click. Do not synthesize a click or bypass REM.
            async def blank_page():
                page = await session._get_page()
                await page.goto("about:blank")
                return page.url
            session.run(blank_page(), timeout=5)
            report["missing_target_result"] = session.run(
                session.smart_click("click the first video"), timeout=15)
            report["missing_target_no_navigation"] = session.run(
                _page_url(session), timeout=5) == "about:blank"
            return report
        finally:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            if proc.poll() is None:
                # Terminate only this harness-launched Chrome process tree,
                # identified by its exact Popen PID and unique temp profile.
                subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, check=False)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            os.environ.pop("REM_CHROME_CDP_URL", None)


async def _page_url(session):
    return (await session._get_page()).url


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-only", action="store_true",
                        help="inspect disposable live result markup without clicking")
    _args = parser.parse_args()
    print(json.dumps(run(), indent=2))
