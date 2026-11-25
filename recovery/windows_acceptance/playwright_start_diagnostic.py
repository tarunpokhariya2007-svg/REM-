"""Bounded diagnostic for the Playwright driver startup path."""
from __future__ import annotations

import asyncio
import time
print("BEFORE_PLAYWRIGHT_IMPORT", flush=True)
from playwright.async_api import async_playwright
print("PLAYWRIGHT_IMPORT_COMPLETED", flush=True)


async def main():
    started = time.monotonic()
    try:
        print("BEFORE_PLAYWRIGHT_START", flush=True)
        playwright = await asyncio.wait_for(async_playwright().start(), timeout=12)
        print({"started": True, "seconds": round(time.monotonic() - started, 2),
               "chromium_available": playwright.chromium is not None})
        await playwright.stop()
    except Exception as exc:
        print({"started": False, "seconds": round(time.monotonic() - started, 2),
               "error_type": type(exc).__name__, "error": str(exc)[:800]})


if __name__ == "__main__":
    asyncio.run(main())
