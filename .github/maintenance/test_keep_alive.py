"""Offline browser fixtures only: no production traffic or patient data."""

import asyncio
import html
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

import keep_alive as probe


ENGINE = """globalThis.predictionCalls = 0;
globalThis.FASTI = {
  modelData: {version: 'FAST-I M2 RSF'},
  predictFastiOsFromRaw() { globalThis.predictionCalls++; throw Error('Do not predict'); },
  calculateRisk() { globalThis.predictionCalls++; throw Error('Do not calculate'); }
};"""
FORM = """<form id="risk-form"><h2>Patient characteristics</h2>
<button id="calculate-risk" type="button">Calculate risk</button></form>
<footer class="page-footer">Model version: <strong>FAST-I M2 RSF</strong></footer>"""
HEALTHY = FORM + "<script>" + ENGINE + "</script>"
FIXTURE_URL = "https://fasti-fixture.invalid/"


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        options = {"headless": True}
        channel = os.environ.get("FASTI_TEST_BROWSER_CHANNEL")
        if channel:
            options["channel"] = channel
        self.browser = await self.playwright.chromium.launch(**options)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def visit(self, body, *, status=200, timeout=4, state=None):
        await self.page.route("**/*", lambda route: route.fulfill(
            status=status, content_type="text/html", body=body))
        return await asyncio.wait_for(
            probe.check_page(self.page, state or probe.RunState(), url=FIXTURE_URL,
                             poll_interval=0.02), timeout=timeout)

    async def test_healthy_page_and_no_prediction(self):
        state = probe.RunState()
        with self.assertLogs(probe.LOG, level="INFO") as logs:
            await self.visit(HEALTHY, state=state)
        self.assertTrue(any("[VISIT]" in line for line in logs.output))
        self.assertEqual(await self.page.evaluate("predictionCalls"), 0)
        self.assertEqual((state.wake_requests, state.wake_clicks), (0, 0))

    async def test_nested_streamlit_iframes(self):
        inner = '<iframe srcdoc="' + html.escape(HEALTHY, quote=True) + '"></iframe>'
        outer = '<iframe srcdoc="' + html.escape(inner, quote=True) + '"></iframe>'
        await self.visit(outer)

    async def test_sleep_button_wakes_application(self):
        body = ('<section id="sleep">This app has gone to sleep due to inactivity.'
                '<button onclick="window.wakeClicks=(window.wakeClicks||0)+1; '
                "document.getElementById('app').hidden=false; "
                "document.getElementById('sleep').remove();\">"
                + probe.WAKE_BUTTON + '</button></section>'
                '<section id="app" hidden>' + HEALTHY + '</section>')
        state = probe.RunState()
        with self.assertLogs(probe.LOG, level="INFO") as logs:
            await self.visit(body, state=state)
        self.assertEqual(state.wake_requests, 1)
        self.assertEqual(state.wake_clicks, 1)
        self.assertEqual(await self.page.evaluate("wakeClicks"), 1)
        self.assertEqual(await self.page.evaluate("predictionCalls"), 0)
        self.assertTrue(any("[SLEEP_DETECTED]" in line for line in logs.output))
        self.assertTrue(any("[WAKE_REQUESTED]" in line for line in logs.output))
        self.assertTrue(any("[WAKE_CLICKED]" in line for line in logs.output))

    async def test_stuck_wake_button_is_clicked_only_once(self):
        state = probe.RunState()
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit('<button>' + probe.WAKE_BUTTON + '</button>',
                             state=state, timeout=1)
        self.assertEqual(state.wake_requests, 1)
        self.assertEqual(state.wake_clicks, 1)

    async def test_sleep_page_without_button_times_out(self):
        state = probe.RunState()
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit("This app has gone to sleep due to inactivity.",
                             state=state, timeout=1)
        self.assertEqual(state.wake_requests, 0)
        self.assertEqual(state.wake_clicks, 0)

    async def test_failed_click_records_request_without_completed_click(self):
        for failure in (BrowserError("fixture click failure"),
                        BrowserTimeout("fixture click timeout")):
            with self.subTest(error=type(failure).__name__):
                state = probe.RunState()

                async def click(**kwargs):
                    self.assertEqual((state.wake_requests, state.wake_clicks), (1, 0))
                    raise failure

                button = AsyncMock()
                button.click.side_effect = click
                with patch.object(probe, "inspect_page", new=AsyncMock(
                        return_value=(button, None, True))), self.assertLogs(
                            probe.LOG, level="INFO") as logs:
                    with self.assertRaises(BrowserError):
                        await self.visit("<p>Sleeping fixture</p>", state=state)
                button.click.assert_awaited_once_with(timeout=5000)
                self.assertEqual((state.wake_requests, state.wake_clicks), (1, 0))
                self.assertTrue(any("[WAKE_REQUESTED]" in line for line in logs.output))
                self.assertFalse(any("[WAKE_CLICKED]" in line for line in logs.output))

    async def test_http_200_generic_streamlit_is_not_success(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit('<div data-testid="stApp">Streamlit</div>', timeout=1)

    async def test_missing_engine_is_not_success(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit(FORM, timeout=1)

    async def test_wrong_model_version_is_not_success(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit(HEALTHY.replace("FAST-I M2 RSF", "Old Cox model"), timeout=1)

    async def test_missing_model_function_is_not_success(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit(HEALTHY.replace("predictFastiOsFromRaw()", "unrelated()"), timeout=1)

    async def test_disabled_calculate_is_not_success(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit(HEALTHY.replace('id="calculate-risk"', 'id="calculate-risk" disabled'),
                             timeout=1)

    async def test_hidden_form_is_not_success(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit('<div hidden>' + HEALTHY + '</div>', timeout=1)

    async def test_error_takes_precedence_over_ready_page(self):
        with self.assertRaises(probe.ProbeError):
            await self.visit(HEALTHY + '<div data-testid="stException">Failure</div>')

    async def test_streamlit_startup_error(self):
        with self.assertRaises(probe.ProbeError):
            await self.visit("Oh no. Error running app")

    async def test_hidden_error_does_not_mask_visible_error(self):
        with self.assertRaises(probe.ProbeError):
            await self.visit(HEALTHY + '<div class="stException" hidden>old</div>'
                             '<div class="stException">current failure</div>')

    async def test_resource_error(self):
        with self.assertRaises(probe.ProbeError):
            await self.visit("This app has gone over its resource limits.")

    async def test_javascript_error_is_failure(self):
        with self.assertRaises(probe.ProbeError):
            await self.visit(HEALTHY + "<script>throw new Error('fixture failure')</script>")

    async def test_wake_error_is_not_success(self):
        body = ('<button onclick="document.body.innerHTML=\'Error running app\'">'
                + probe.WAKE_BUTTON + '</button>')
        with self.assertRaises(probe.ProbeError):
            await self.visit(body)

    async def test_sleep_page_cannot_pass_with_ready_looking_form(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.visit(HEALTHY + '<button>' + probe.WAKE_BUTTON + '</button>', timeout=1)

    async def test_http_server_error_is_failure(self):
        with self.assertRaises(probe.ProbeError):
            await self.visit(HEALTHY, status=503)

    async def test_network_failure(self):
        await self.page.route("**/*", lambda route: route.abort("connectionfailed"))
        with self.assertRaises(BrowserError):
            await probe.check_page(self.page, probe.RunState(), url=FIXTURE_URL)

    async def test_retry_is_bounded_and_recovers(self):
        first = AsyncMock()
        first_page = AsyncMock()
        first_page.on = Mock()
        first_page.goto.side_effect = BrowserTimeout("fixture navigation timeout")
        first.new_page.return_value = first_page
        second = await self.browser.new_context()
        await second.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=HEALTHY))
        browser = AsyncMock()
        browser.new_context.side_effect = [first, second]
        with self.assertLogs(probe.LOG, level="INFO") as logs:
            ok = await probe.monitor(browser, url=FIXTURE_URL, attempts=2,
                                     attempt_timeout=4, retry_delay=0, poll_interval=0.02)
        self.assertTrue(ok)
        self.assertEqual(browser.new_context.await_count, 2)
        first.close.assert_awaited_once()
        self.assertTrue(any("[RETRY]" in line for line in logs.output))
        self.assertTrue(any("[HEALTHY]" in line for line in logs.output))

    async def test_all_attempts_fail_without_false_success(self):
        contexts = [AsyncMock() for _ in range(3)]
        for context in contexts:
            context.new_page.return_value.on = Mock()
            context.new_page.return_value.goto.side_effect = BrowserError("fixture network failure")
        browser = AsyncMock()
        browser.new_context.side_effect = contexts
        with self.assertLogs(probe.LOG, level="INFO") as logs:
            ok = await probe.monitor(browser, attempts=3, retry_delay=0)
        self.assertFalse(ok)
        self.assertEqual(browser.new_context.await_count, 3)
        self.assertTrue(any("[FAILED]" in line for line in logs.output))
        self.assertFalse(any("[HEALTHY]" in line or "[WAKE_SUCCESS]" in line
                             or "[RECOVERED_UNCONFIRMED]" in line for line in logs.output))
        for context in contexts:
            context.close.assert_awaited_once()

    async def test_monitor_reports_wake_success_only_after_ready(self):
        body = ('<section id="sleep"><button onclick="'
                "document.getElementById('app').hidden=false; "
                "document.getElementById('sleep').remove();\">"
                + probe.WAKE_BUTTON + '</button></section>'
                '<section id="app" hidden>' + HEALTHY + '</section>')
        context = await self.browser.new_context()
        await context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=body))
        browser = AsyncMock()
        browser.new_context.return_value = context
        state = probe.RunState()
        with patch.object(probe, "RunState", return_value=state), self.assertLogs(
                probe.LOG, level="INFO") as logs:
            ok = await probe.monitor(browser, url=FIXTURE_URL, attempts=1,
                                     attempt_timeout=4, poll_interval=0.02)
        self.assertTrue(ok)
        self.assertTrue(any("[WAKE_SUCCESS]" in line for line in logs.output))
        self.assertEqual((state.wake_requests, state.wake_clicks), (1, 1))
        self.assertFalse(any("[RECOVERED_UNCONFIRMED]" in line for line in logs.output))

    async def test_monitor_healthy_without_wake_request(self):
        context = await self.browser.new_context()
        await context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=HEALTHY))
        browser = AsyncMock()
        browser.new_context.return_value = context
        state = probe.RunState()
        with patch.object(probe, "RunState", return_value=state), self.assertLogs(
                probe.LOG, level="INFO") as logs:
            ok = await probe.monitor(browser, url=FIXTURE_URL, attempts=1,
                                     attempt_timeout=4, poll_interval=0.02)
        self.assertTrue(ok)
        self.assertEqual((state.wake_requests, state.wake_clicks), (0, 0))
        self.assertTrue(any("[HEALTHY]" in line for line in logs.output))
        self.assertFalse(any("[WAKE_SUCCESS]" in line or "[RECOVERED_UNCONFIRMED]" in line
                             for line in logs.output))

    async def test_failed_click_then_healthy_is_recovered_unconfirmed(self):
        first = await self.browser.new_context()
        await first.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body='<button>' + probe.WAKE_BUTTON + '</button>'))
        second = await self.browser.new_context()
        await second.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=HEALTHY))
        browser = AsyncMock()
        browser.new_context.side_effect = [first, second]
        button = AsyncMock()
        button.click.side_effect = BrowserTimeout("fixture click completion unconfirmed")
        original_inspect = probe.inspect_page

        async def inspect(page):
            wake, ready, sleeping = await original_inspect(page)
            return button if wake is not None else None, ready, sleeping

        state = probe.RunState()
        with (
            patch.object(probe, "RunState", return_value=state),
            patch.object(probe, "inspect_page", side_effect=inspect),
            self.assertLogs(probe.LOG, level="INFO") as logs,
        ):
            ok = await probe.monitor(browser, url=FIXTURE_URL, attempts=2,
                                     attempt_timeout=4, retry_delay=0, poll_interval=0.02)
        self.assertTrue(ok)
        self.assertEqual(browser.new_context.await_count, 2)
        button.click.assert_awaited_once_with(timeout=5000)
        self.assertEqual((state.wake_requests, state.wake_clicks), (1, 0))
        self.assertTrue(any("[RECOVERED_UNCONFIRMED]" in line for line in logs.output))
        self.assertTrue(any("wake_requests=1; wake_clicks=0" in line for line in logs.output))
        self.assertFalse(any("[WAKE_SUCCESS]" in line or "[WAKE_CLICKED]" in line
                             or "[HEALTHY]" in line for line in logs.output))

    async def test_completed_clicks_without_readiness_still_fail(self):
        contexts = [await self.browser.new_context() for _ in range(3)]
        for context in contexts:
            await context.route("**/*", lambda route: route.fulfill(
                content_type="text/html", body='<button>' + probe.WAKE_BUTTON + '</button>'))
        browser = AsyncMock()
        browser.new_context.side_effect = contexts
        state = probe.RunState()
        with patch.object(probe, "RunState", return_value=state), self.assertLogs(
                probe.LOG, level="INFO") as logs:
            ok = await probe.monitor(browser, url=FIXTURE_URL, attempts=3,
                                     attempt_timeout=1, retry_delay=0, poll_interval=0.02)
        self.assertFalse(ok)
        self.assertEqual((state.wake_requests, state.wake_clicks), (3, 3))
        self.assertEqual(sum("[WAKE_CLICKED]" in line for line in logs.output), 3)
        self.assertTrue(any("[FAILED]" in line and "wake_requests=3; wake_clicks=3" in line
                            for line in logs.output))
        self.assertFalse(any("[WAKE_SUCCESS]" in line or "[HEALTHY]" in line
                             or "[RECOVERED_UNCONFIRMED]" in line for line in logs.output))


class ConfigurationTests(unittest.TestCase):
    def test_workflow_contract(self):
        root = Path(__file__).resolve().parents[2]
        config = yaml.safe_load((root / ".github/workflows/keep_alive.yml").read_text())
        self.assertEqual(set(config["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(config["on"]["schedule"], [{"cron": "17 */4 * * *"}])
        self.assertEqual(config["permissions"], {"contents": "read"})
        self.assertFalse(config["concurrency"]["cancel-in-progress"])
        self.assertEqual(config["jobs"]["check"]["runs-on"], "ubuntu-latest")
        self.assertEqual(config["jobs"]["check"]["timeout-minutes"], 12)
        commands = "\n".join(step.get("run", "") for step in config["jobs"]["check"]["steps"])
        self.assertNotIn("-r requirements.txt", commands)
        self.assertNotIn("git commit", commands)

    def test_exit_code_is_nonzero_on_browser_failure(self):
        with patch("sys.argv", ["keep_alive.py"]), patch.object(
                probe, "run", new=AsyncMock(side_effect=BrowserError("fixture startup failure"))):
            self.assertEqual(probe.main(), 1)

    def test_exit_code_is_nonzero_on_failed_health_check(self):
        with patch("sys.argv", ["keep_alive.py"]), patch.object(
                probe, "run", new=AsyncMock(return_value=False)):
            self.assertEqual(probe.main(), 1)

    def test_exit_code_is_zero_only_on_verified_health(self):
        with patch("sys.argv", ["keep_alive.py"]), patch.object(
                probe, "run", new=AsyncMock(return_value=True)):
            self.assertEqual(probe.main(), 0)


if __name__ == "__main__":
    unittest.main()
