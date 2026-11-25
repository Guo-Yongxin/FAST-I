# FAST-I availability and public wake-up check

This maintenance-only workflow checks `https://fast-1.streamlit.app/` using a
short-lived headless Chromium session. It does not modify or import the FAST-I
application, install its requirements, fill clinical fields, submit the form,
or call a prediction function. No patient data, API keys, or extra server are 
required.

## Behaviour and success criteria

- `.github/workflows/keep_alive.yml` runs on `ubuntu-latest` at
  `17 */4 * * *`: 00:17, 04:17, 08:17, 12:17, 16:17 and 20:17 **UTC**.
  `workflow_dispatch` also permits a manual run.
- A single Chromium process uses a fresh, non-persistent context on each attempt.
  The script follows the normal browser page and its nested iframes. It never
  calls an undocumented Streamlit wake endpoint.
- When the exact public `Yes, get this app back up!` button is visible, the
  script clicks it at most once per attempt and waits for the real app.
  `wake_requests` increases immediately before a click is attempted;
  `wake_clicks` increases only when the awaited click returns normally. The
  per-attempt `clicked` flag is set only then. A failed or timed-out click ends
  that attempt, so it cannot cause another click within the same attempt.
  Both counters persist across retries and appear in the final state log.
- Success requires the same visible frame to contain `#risk-form`, an enabled
  and visible `#calculate-risk`, and the footer's `FAST-I M2 RSF` model version.
  The existing `FASTI.modelData.version`, `predictFastiOsFromRaw` and
  `calculateRisk` functions must also be loaded. The script checks their
  existence only: it does not execute them. Two consecutive checks must agree.
- An HTTP 200, generic Streamlit shell, disabled button, partial form, missing
  JavaScript, sleeping page or error page alone cannot pass. Visible application
  exceptions, uncaught JavaScript errors, HTTP failures and navigation/network
  errors fail the attempt. A sleep or error page takes precedence over a
  ready-looking form elsewhere in the frame tree.
- There are at most three attempts, each with a 120-second readiness timeout,
  with 5- and 10-second retry delays. Navigation has a 30-second timeout. The job
  has a separate 12-minute hard timeout, including dependency installation and
  tests. A named concurrency group allows no overlapping running checks.
- English logs distinguish `VISIT`, `SLEEP_DETECTED`, `WAKE_REQUESTED`,
  `WAKE_CLICKED`, `ATTEMPT_FAILED` and `RETRY`. `WAKE_CLICKED` confirms only
  that the browser click completed, not that the application recovered.
- After the full FAST-I readiness check passes, the final state is `HEALTHY`
  when no wake click was attempted; `WAKE_SUCCESS` when at least one click
  completed; or `RECOVERED_UNCONFIRMED` when clicks were attempted but none
  completed normally. All three states mean verified application readiness
  and return zero. These labels record the observed sequence; they do not
  establish that a click caused recovery.
- If all bounded attempts finish without verified readiness, the final state
  is `FAILED`, regardless of the click counters, and the script exits nonzero.
  Browser startup errors also exit nonzero.

The only token permission is `contents: read`; checkout does not persist
credentials. The workflow installs pinned maintenance dependencies in the
runner, not in the Streamlit application. Existing model files, UI, deployment
settings and `requirements.txt` are unchanged.

## Manual execution

1. Open **Actions → FAST-I availability check → Run workflow**, select `main`
   and run it. Enable GitHub Actions first if the repository has disabled it.
2. Read the final **Visit FAST-I and wake it if necessary** step. A successful
   job ends with `[HEALTHY]`, `[WAKE_SUCCESS]` or `[RECOVERED_UNCONFIRMED]`,
   interpreted as defined above. A red job requires investigation; it must
   not be treated as application recovery.

## Tests

Manual workflow runs validate YAML and run offline browser tests before the
production visit; scheduled runs skip the regression suite to reduce runner
work. All fixture requests are intercepted inside Playwright and
use invented HTML, not a deployed app or patient data. Tests cover nested
iframes, healthy and sleeping pages, wake success/failure, single-click bounds,
request/completed-click counters, unconfirmed recovery, HTTP/network/navigation
failures, timeout, retry exhaustion and false positives.

To reproduce in an isolated Python 3.12 environment:

```sh
python -m pip install playwright==1.65.2 PyYAML==6.0.3
python -m playwright install --with-deps --only-shell chromium
python -m unittest discover -s .github/maintenance -p 'test_*.py' -v
python .github/maintenance/keep_alive.py
```

For local testing with an already installed Chrome, set
`FASTI_TEST_BROWSER_CHANNEL=chrome` for unittest and pass
`--browser-channel chrome` to the probe. The production job always uses the
Playwright-installed Chromium.

## Platform limits and policy review

Reviewed on **2026-11-25** against the linked primary documentation:

- [Streamlit hibernation](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation)
  documents sleep after 12 hours without traffic and the public wake-up button.
  This is a best-effort browser availability check, not an officially guaranteed
  always-awake mechanism, uptime SLA, or approval of automated traffic.
- [Streamlit terms](https://streamlit.io/terms-of-use) apply, including restrictions
  on interference, security circumvention and sensitive health/personal data.
  This probe neither submits patient data nor bypasses authentication, CAPTCHA,
  quota/resource-limit pages or any protection. Its scope does not establish
  compliance of the underlying clinical calculator; the owner must separately
  assess that use. Stop the workflow if platform guidance changes or it causes
  load problems.
- [GitHub Actions terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
  restrict disproportionate load and unrelated use. This bounded check tests the
  deployed software from this repository; it is not an unrelated compute service.
- [Scheduled workflow restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule):
  schedules run only on the default branch and can be delayed or dropped.
  **In a public repository, 60 days without repository activity may automatically
  disable scheduled workflows. Running this job is not a guarantee against that
  restriction.** No empty/false commits or automated writes are used to evade it.
  Review the Actions page periodically and [manually enable the workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)
  when necessary.

## Ongoing maintenance

Keep Actions failure notifications enabled for the maintainer. Review this job
after genuine app changes and periodically check whether GitHub disabled its
schedule. If model labels, stable DOM IDs, iframe layout, or the public wake-up
button change, update this independent probe and its fixtures together; do not
weaken it to accept HTTP 200. Update pinned actions/dependencies through reviewed
PRs and rerun tests. Use **Actions → workflow menu → Disable workflow** to stop it.

A live smoke test can prove the currently served state, but cannot safely force
production to hibernate. Wake-up transitions and failures are exercised in
offline fixtures. Frozen prediction accuracy is deliberately outside this check.
