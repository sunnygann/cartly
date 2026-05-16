from playwright.async_api import async_playwright, Browser

_playwright = None
_browser: Browser | None = None


async def get_browser() -> Browser:
    global _playwright, _browser
    if _browser and _browser.is_connected():
        return _browser
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    print("[browser] Chromium started")
    return _browser


async def stop_browser():
    global _playwright, _browser
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
