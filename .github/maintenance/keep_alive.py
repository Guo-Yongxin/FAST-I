"""Read-only FAST-I availability probe, except for the public wake-up button."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from dataclasses import dataclass

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright


URL = "https://fast-1.streamlit.app/"
WAKE_BUTTON = "Yes, get this app back up!"
LOG = logging.getLogger("fasti.availability")
ERROR_TEXT = re.compile(
    r"Oh no\.|Error running (?:the )?app|Script execution error|"
    r"This app has gone over its resource limits|Unable to load the FAST-I|"
    r"Application error|Bad Gateway|Service Unavailable|"
    r"ModuleNotFoundError|StreamlitAPIException",
    re.IGNORECASE,
)
ENGINE_READY = """() => {
  const app = globalThis.FASTI;
  return Boolean(app && app.modelData?.version === 'FAST-I M2 RSF'
    && typeof app.predictFastiOsFromRaw === 'function'
    && typeof app.calculateRisk === 'function');
}"""


class ProbeError(RuntimeError):
    pass


@dataclass
class RunState:
    wake_requests: int = 0
    wake_clicks: int = 0


async def inspect_page(page):
    """Check all frames; exceptions take precedence over a ready-looking form."""
    wake_button = ready_frame = None
    sleeping = False
    for frame in list(page.frames):
        if frame.is_detached():
            continue
        try:
            error = frame.locator(
                '[data-testid="stException"], .stException, '
                '#status-message.status-error:not(.is-hidden)'
            )
            for element in await error.all():
                if await element.is_visible():
                    raise ProbeError("The page displays an application exception.")
            body = frame.locator("body")
            text = await body.inner_text(timeout=1000) if await body.count() else ""
            if ERROR_TEXT.search(text):
                raise ProbeError("The page displays a startup or service error.")
            button = frame.get_by_role("button", name=WAKE_BUTTON, exact=True)
            if await button.count() == 1 and await button.is_visible():
                wake_button = button
                sleeping = True
            if "This app has gone to sleep" in text:
                sleeping = True
            form = frame.locator("#risk-form")
            calculate = frame.locator("#calculate-risk")
            footer = frame.locator(".page-footer")
            if (
                await form.count() == 1
                and await form.is_visible()
                and await calculate.count() == 1
                and await calculate.is_visible()
                and await calculate.is_enabled()
                and await footer.count() == 1
                and re.search(r"Model version:\s*FAST-I M2 RSF", await footer.inner_text())
                and await frame.evaluate(ENGINE_READY)
            ):
                ready_frame = frame
        except BrowserError:
            if frame.is_detached():
                continue
            raise
    return wake_button, ready_frame, sleeping


async def check_page(page, state, *, url=URL, poll_interval=2):
    """Visit without submitting a form or executing any model prediction."""
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(error))
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if response is None or response.status >= 400:
        status = "no HTTP response" if response is None else f"HTTP {response.status}"
        raise ProbeError(f"Navigation failed: {status}.")
    LOG.info("[VISIT] Browser navigation completed; checking FAST-I, not just HTTP status.")
    clicked = sleep_logged = False
    previous_ready = None
    while True:
        if page_errors:
            raise ProbeError("An uncaught browser JavaScript error occurred.")
        button, ready_frame, sleeping = await inspect_page(page)
        if sleeping and not sleep_logged:
            LOG.info("[SLEEP_DETECTED] Streamlit is displaying its hibernation page.")
            sleep_logged = True
        if button is not None and not clicked:
            state.wake_requests += 1
            LOG.info("[WAKE_REQUESTED] Attempting the public Streamlit wake-up button once.")
            await button.click(timeout=5000)
            state.wake_clicks += 1
            clicked = True
            LOG.info("[WAKE_CLICKED] Button click completed; waiting for FAST-I readiness.")
        if not sleeping and ready_frame is not None:
            if ready_frame is previous_ready:
                if page_errors:
                    raise ProbeError("An uncaught browser JavaScript error occurred.")
                return
            previous_ready = ready_frame
        else:
            previous_ready = None
        await asyncio.sleep(poll_interval)


async def monitor(browser, *, url=URL, attempts=3, attempt_timeout=120,
                  retry_delay=5, poll_interval=2):
    """Each bounded attempt uses a fresh, non-persistent browser context."""
    state = RunState()
    for number in range(1, attempts + 1):
        LOG.info("[ATTEMPT] %d/%d; timeout=%ss", number, attempts, attempt_timeout)
        context = None
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await asyncio.wait_for(
                check_page(page, state, url=url, poll_interval=poll_interval),
                timeout=attempt_timeout,
            )
            if state.wake_clicks:
                status = "WAKE_SUCCESS"
            elif state.wake_requests:
                status = "RECOVERED_UNCONFIRMED"
            else:
                status = "HEALTHY"
            LOG.info("[%s] FAST-I M2 RSF form and JavaScript engine are ready; "
                     "wake_requests=%d; wake_clicks=%d.",
                     status, state.wake_requests, state.wake_clicks)
            return True
        except (ProbeError, BrowserError, asyncio.TimeoutError) as error:
            detail = str(error).strip() or "Application readiness timed out."
            LOG.warning("[ATTEMPT_FAILED] %d/%d: %s: %s", number, attempts,
                        type(error).__name__, detail)
        finally:
            if context is not None:
                await context.close()
        if number < attempts:
            delay = retry_delay * number
            LOG.info("[RETRY] Waiting %ss before a fresh browser session.", delay)
            await asyncio.sleep(delay)
    LOG.error("[FAILED] FAST-I was not verified healthy after %d bounded attempts; "
              "wake_requests=%d; wake_clicks=%d.",
              attempts, state.wake_requests, state.wake_clicks)
    return False


async def run(browser_channel=None):
    async with async_playwright() as playwright:
        options = {"headless": True, "timeout": 30000}
        if browser_channel:
            options["channel"] = browser_channel
        browser = await playwright.chromium.launch(**options)
        try:
            return await monitor(browser)
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-channel", choices=["chrome"], default=None,
                        help="Use an existing local Chrome for maintenance tests only.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S")
    logging.Formatter.converter = time.gmtime
    try:
        success = asyncio.run(run(args.browser_channel))
    except (ProbeError, BrowserError, asyncio.TimeoutError) as error:
        LOG.error("[FAILED] Browser or application startup failed: %s", error)
        success = False
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
