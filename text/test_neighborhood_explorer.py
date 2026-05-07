#!/usr/bin/env python3
"""
Dream Neighborhood Explorer — staging load test (Playwright, sync API)

================================================================================
INSTALLATION
================================================================================
  python -m venv .venv
  .venv\\Scripts\\activate          # Windows
  pip install -r requirements.txt   # repo root
  playwright install chromium

  Optional login (staging shows a sign-in wall for anonymous visitors):

    PowerShell (same terminal before running):
      $env:DREAM_NEIGHBORHOOD_EMAIL = "you@example.com"
      $env:DREAM_NEIGHBORHOOD_PASSWORD = "secret"

    Or create a repo-root .env file (see .env.example). Existing environment
    variables always win; .env is not committed (.gitignore).

================================================================================
DISCOVER SELECTORS (do this before a full 1000-run)
================================================================================
  playwright codegen https://staging.dreamneighborhood.com/a/drea-neighborhood-treasure-coast/core/explore-neighborhoods/

  Paste working selectors into the CONFIG section below (IFRAME_SELECTOR,
  ADDRESS_INPUT_SELECTOR, etc.). The defaults are guesses and WILL need tuning.

================================================================================
ADDRESS COVERAGE (50 states)
================================================================================
  The random-address dataset may not include every US state; when a state is
  missing, the script falls back to Faker so all 50 states stay balanced
  (~count/50 each).

================================================================================
USAGE EXAMPLES
================================================================================
  python text/test_neighborhood_explorer.py --headed

  If you omit --count, you are prompted in the terminal (1-1000). CI/GitHub must pass --count.
  python text/test_neighborhood_explorer.py --count 1000 --output my_results.json --delay 0.5

  --count is 1-1000. Omit --count in a terminal to be prompted. Non-TTY uses 2.
  The dashboard can use ?count=N or the smoke-size control to copy the same number.

  Live dashboard (local): serves dashboard/ on http://127.0.0.1:<port> and
  updates live_state.json after every address. Open:
    http://127.0.0.1:8765/?live=1
  (Netlify stays static; live mode is only while the Python process runs.)

================================================================================
WAITING FOR RESULTS
================================================================================
  After submit, the script waits for an error banner, a non-empty neighborhood
  title, or meaningful metric rows — polling until RESULT_TIMEOUT_MS. Optional
  LOADING_SELECTOR lets the runner wait for spinners to finish first.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from faker import Faker
from random_address import real_random_address, real_random_address_by_state
from tqdm import tqdm

from playwright.sync_api import FrameLocator, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ExplorerCtx = Union[Page, FrameLocator, Locator]

# ---------------------------------------------------------------------------
# Tunable selectors & timing — update after running Playwright codegen
# ---------------------------------------------------------------------------
STAGING_URL = (
    "https://staging.dreamneighborhood.com/a/drea-neighborhood-treasure-coast/core/explore-neighborhoods/"
)
LOGIN_URL = "https://staging.dreamneighborhood.com/accounts/login/"

# Set to None to use the top-level page (Dream Neighborhood hosts the map search in <main>,
# not in an iframe — a stray first <iframe> on the page caused address fills to time out).
IFRAME_SELECTOR: Optional[str] = None
# When not using an iframe, scope locators to app content (avoids unrelated header/footer inputs).
WIDGET_HOST_SELECTOR: Optional[str] = "main"

# Locators are resolved against Page, FrameLocator, or a scoped Locator (e.g. <main>).
ADDRESS_INPUT_SELECTOR = (
    'input[placeholder*="address" i], input[placeholder*="search" i], '
    'input[type="search"], input[name="address"], input[name="q"], '
    'input[aria-label*="address" i], input[aria-label*="search" i], textarea'
)
SUBMIT_BUTTON_SELECTOR = (
    'button[type="submit"], button:has-text("Search"), button:has-text("Explore"), '
    'button:has-text("Go"), button:has-text("View Neighborhood Data"), '
    '[role="button"]:has-text("Search"), [role="button"]:has-text("View Neighborhood Data")'
)
CLEAR_BUTTON_SELECTOR = 'button:has-text("Clear"), button[aria-label*="clear" i]'

RESULT_READY_SELECTOR = (
    '[class*="result" i], [class*="neighborhood" i], [data-testid*="result" i], '
    'main article, [role="article"]'
)
NEIGHBORHOOD_NAME_SELECTOR = "h1, h2, h3, [class*='neighborhood' i], [class*='title' i]"
METRIC_ROW_SELECTOR = '[class*="score" i], [class*="metric" i], li:has-text("/100"), dd, dt'
ERROR_SELECTOR = '[class*="error" i], [class*="alert" i], [role="alert"], .text-danger'
# If the widget shows a spinner / aria-busy while fetching, tune this (set to "" to disable).
LOADING_SELECTOR = '[class*="loading" i], [class*="spinner" i], [aria-busy="true"], [data-loading="true"]'

NAVIGATION_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 25_000
RESULT_TIMEOUT_MS = 60_000
RETRIES_PER_ADDRESS = 3
RETRY_BASE_SLEEP_SEC = 0.75
POST_SUBMIT_STABILITY_MS = 400
# Minimum time after click before we start polling (lets the UI request start).
MIN_POST_SUBMIT_WAIT_MS = 350
RESULT_SETTLE_POLL_MS = 250
# After success/error is detected, brief pause so late-bound text/metrics can render.
STABILIZE_AFTER_OUTCOME_MS = 900

# US state codes (50 states). ~count/20 per state when count=1000.
ALL_US_STATE_CODES: List[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
MIN_ADDRESS_COUNT = 1
MAX_ADDRESS_COUNT = 1000
DEFAULT_ADDRESS_COUNT = 2
_DEFAULT_ARTIFACTS = _REPO_ROOT / "artifacts" / "screenshots"
# Repository slug for help links in exported JSON (override in CI with GITHUB_REPOSITORY).
GITHUB_REPO_PATH = os.environ.get("GITHUB_REPOSITORY", "motormouthvis/automated-UI-test")


def _load_repo_dotenv() -> None:
    """Load repo-root .env into os.environ if present. Does not override existing vars."""
    path = _REPO_ROOT / ".env"
    if not path.is_file():
        return
    # UTF-8; tolerate BOM on Windows editors
    text = path.read_text(encoding="utf-8-sig")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = rest.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def _line_buffer_stdio() -> None:
    """Best-effort: flush print/tqdm.write sooner when stdout/stderr are pipes (IDE runs)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(line_buffering=True)
            except Exception:
                pass


def _run_meta_extras(login_env_configured: bool) -> Dict[str, Any]:
    root = f"https://github.com/{GITHUB_REPO_PATH}"
    return {
        "login_env_configured": login_env_configured,
        "docs_start_here": f"{root}/blob/main/docs/START-HERE.md",
        "docs_running": f"{root}/blob/main/docs/RUNNING.md",
        "github_actions_workflow": f"{root}/actions/workflows/run-explorer-tests.yml",
        "note_netlify_static": (
            "Netlify serves this dashboard as static files only. It cannot start Playwright from your browser. "
            "Use local Python (see docs) or GitHub Actions to produce results.json."
        ),
    }


@dataclass
class TestRow:
    id: int
    address: str
    state: str
    success: bool
    duration_ms: float
    neighborhood_name: Optional[str]
    metrics: Dict[str, str]
    error_message: Optional[str]
    screenshot_path: Optional[str]
    extracted_raw: str
    retry_count: int = 0
    address_source: str = "random-address"
    outcome_code: str = ""
    diagnosis: str = ""
    page_url: str = ""
    used_iframe: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_from_random_address_row(row: Dict[str, Any]) -> str:
    parts = [
        str(row.get("address1") or "").strip(),
        str(row.get("address2") or "").strip(),
        str(row.get("city") or "").strip(),
        str(row.get("state") or "").strip(),
        str(row.get("postalCode") or "").strip(),
    ]
    line1 = ", ".join(p for p in parts[:2] if p)
    line2 = " ".join(p for p in parts[2:] if p)
    return ", ".join(x for x in (line1, line2) if x)


def _generate_address_for_state(state: str, faker: Faker) -> tuple[Dict[str, Any], str]:
    """Returns (address_dict, source_tag)."""
    try:
        raw = real_random_address_by_state(state)
        if raw and raw.get("address1"):
            return raw, "random-address"
    except Exception:
        pass
    try:
        # Library global pool is still useful when a state file is missing.
        raw = real_random_address()
        if raw and raw.get("address1"):
            raw = dict(raw)
            raw["state"] = state
            try:
                raw["postalCode"] = faker.zipcode_in_state(state)
            except Exception:
                raw["postalCode"] = faker.zipcode()
            return raw, "random-address+faker-zip"
    except Exception:
        pass
    return {
        "address1": faker.street_address(),
        "address2": "",
        "city": faker.city(),
        "state": state,
        "postalCode": faker.zipcode_in_state(state),
        "coordinates": None,
    }, "faker-fallback"


def build_address_runlist(total: int) -> List[tuple[str, str, str]]:
    """
    List of (state, full_address_string, source_tag), shuffled, covering all states.
    """
    if total < 1:
        return []
    n_states = len(ALL_US_STATE_CODES)
    base, extra = divmod(total, n_states)
    out: List[tuple[str, str, str]] = []
    faker = Faker("en_US")
    for i, state in enumerate(ALL_US_STATE_CODES):
        n = base + (1 if i < extra else 0)
        for _ in range(n):
            row, source = _generate_address_for_state(state, faker)
            out.append((state, _format_from_random_address_row(row), source))
    random.shuffle(out)
    return out


def _login_form(page: Page):
    """
    The sign-in <form> only. Using page-wide ``input[type=email]`` matches
    unrelated fields (header/footer) that appear earlier in the DOM, which
    breaks allauth's ``name=login`` field.
    """
    return page.locator("form").filter(has=page.locator('input[type="password"]'))


def _maybe_login(page: Page) -> None:
    email = os.environ.get("DREAM_NEIGHBORHOOD_EMAIL", "").strip()
    password = os.environ.get("DREAM_NEIGHBORHOOD_PASSWORD", "").strip()
    if not email or not password:
        return
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    form = _login_form(page).first
    form.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
    # Django allauth — prefer id_login / name=login (not a generic type=email elsewhere on page).
    form.locator('input#id_login, input[name="login"]').first.fill(email, timeout=ACTION_TIMEOUT_MS)
    form.locator('input#id_password, input[name="password"]').first.fill(
        password, timeout=ACTION_TIMEOUT_MS
    )
    form.locator('button[type="submit"], input[type="submit"]').first.click(timeout=ACTION_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)


def _widget_host(page: Page) -> ExplorerCtx:
    """Prefer <main> for explorer controls; fall back to full page if layout differs."""
    if not WIDGET_HOST_SELECTOR:
        return page
    host = page.locator(WIDGET_HOST_SELECTOR).first
    try:
        host.wait_for(state="visible", timeout=12_000)
        return host
    except PlaywrightTimeoutError:
        return page


def _get_context(page: Page) -> tuple[ExplorerCtx, bool]:
    if IFRAME_SELECTOR:
        try:
            page.wait_for_selector(IFRAME_SELECTOR, timeout=12_000)
        except PlaywrightTimeoutError:
            return _widget_host(page), False
        if page.locator(IFRAME_SELECTOR).count() < 1:
            return _widget_host(page), False
        return page.frame_locator(IFRAME_SELECTOR), True
    return _widget_host(page), False


def _locate(ctx: ExplorerCtx, selector: str):
    return ctx.locator(selector).first


def _safe_inner_text(locator) -> str:
    try:
        if locator.count() == 0:
            return ""
        return (locator.inner_text(timeout=3_000) or "").strip()
    except Exception:
        return ""


def _extract_neighborhood_name(ctx: ExplorerCtx) -> Optional[str]:
    txt = _safe_inner_text(ctx.locator(NEIGHBORHOOD_NAME_SELECTOR).first)
    return txt or None


def _extract_metrics(ctx: ExplorerCtx) -> Dict[str, str]:
    metrics: Dict[str, str] = {}
    try:
        loc = ctx.locator(METRIC_ROW_SELECTOR)
        n = min(loc.count(), 40)
        for i in range(n):
            t = _safe_inner_text(loc.nth(i))
            if t and len(t) < 400:
                key = f"metric_{i+1}"
                metrics[key] = t
    except Exception:
        pass
    return metrics


def _extract_error(ctx: ExplorerCtx) -> Optional[str]:
    err = _safe_inner_text(ctx.locator(ERROR_SELECTOR).first)
    return err or None


def _metrics_non_trivial(metrics: Dict[str, str]) -> bool:
    for v in metrics.values():
        s = (v or "").strip()
        if len(s) > 3 and not s.isspace():
            return True
    return False


def _wait_for_loading_done(ctx: ExplorerCtx, page: Page, overall_deadline: float) -> None:
    if not LOADING_SELECTOR:
        return
    loader = ctx.locator(LOADING_SELECTOR).first
    try:
        loader.wait_for(state="visible", timeout=min(3_000, max(0, int((overall_deadline - time.monotonic()) * 1000))))
    except PlaywrightTimeoutError:
        return
    remaining_ms = max(500, int((overall_deadline - time.monotonic()) * 1000))
    try:
        loader.wait_for(state="hidden", timeout=remaining_ms)
    except PlaywrightTimeoutError:
        pass


def _wait_for_explorer_outcome(ctx: ExplorerCtx, page: Page) -> None:
    """
    Poll until we see an error, a neighborhood title, non-trivial metrics, or time out.
    Does not assume RESULT_READY_SELECTOR is correct — uses observable text/metrics.
    """
    deadline = time.monotonic() + RESULT_TIMEOUT_MS / 1000.0
    page.wait_for_timeout(MIN_POST_SUBMIT_WAIT_MS)
    _wait_for_loading_done(ctx, page, deadline)

    while time.monotonic() < deadline:
        err = _extract_error(ctx)
        if err:
            break
        name = _extract_neighborhood_name(ctx)
        if name and len(name.strip()) > 2:
            break
        metrics = _extract_metrics(ctx)
        if _metrics_non_trivial(metrics):
            break
        try:
            ctx.locator(RESULT_READY_SELECTOR).first.wait_for(
                state="visible",
                timeout=min(RESULT_SETTLE_POLL_MS * 2, int(max(100, (deadline - time.monotonic()) * 1000))),
            )
        except PlaywrightTimeoutError:
            pass
        _wait_for_loading_done(ctx, page, deadline)
        page.wait_for_timeout(RESULT_SETTLE_POLL_MS)

    page.wait_for_timeout(STABILIZE_AFTER_OUTCOME_MS)


def _build_outcome(
    *,
    login_wall: bool,
    had_login_config: bool,
    err_ui: Optional[str],
    name: Optional[str],
    metrics: Dict[str, str],
    raw_blob: str,
    error_msg: Optional[str],
    success: bool,
    page_url: str,
    used_iframe: bool,
) -> tuple[str, str, Dict[str, Any]]:
    evidence: Dict[str, Any] = {
        "page_url": page_url,
        "used_iframe": used_iframe,
        "login_wall_text_detected": login_wall,
        "error_banner_text": ((err_ui or "")[:800] if err_ui else None),
        "neighborhood_candidate": name,
        "metric_count": len(metrics),
        "trimmed_body_chars": len(raw_blob or ""),
        "trimmed_body_words": len((raw_blob or "").split()),
        "login_env_was_configured_at_run_start": had_login_config,
    }
    if success:
        return (
            "success",
            "After submit, the runner observed a neighborhood title (and no blocking error UI) or meaningful metric lines. "
            "This is an automation pass from selectors heuristics — visually confirm with Playwright trace/screenshots if needed.",
            evidence,
        )
    if login_wall:
        d = (
            "The captured DOM included Dream Neighborhood’s sign-in experience (e.g. ‘Sign in to your account’). "
            "That is a session/authentication outcome — it is not caused by the street address itself. "
            "Common causes: missing staging credentials, expired session cookie, failed auto-login, or a redirect to /accounts/login/."
        )
        if not had_login_config:
            d += " This run had no DREAM_NEIGHBORHOOD_EMAIL / DREAM_NEIGHBORHOOD_PASSWORD in the environment."
        else:
            d += " Credentials were present at run start; the session may have expired later or navigation dropped auth."
        return "login_wall", d, evidence
    if err_ui:
        return (
            "widget_error",
            "Matched an error/alert region in the widget. See error_banner_text in evidence for the visible message.",
            {**evidence, "error_banner_text": (err_ui or "")[:800]},
        )
    if error_msg and "submit_click" in error_msg:
        return (
            "submit_failed",
            f"The submit control or Enter-key path failed. Technical detail: {error_msg}",
            evidence,
        )
    return (
        "unclear_result",
        "After the full wait window, no neighborhood title, widget error, or useful metrics appeared. "
        "Most often IFRAME_SELECTOR, ADDRESS_INPUT_SELECTOR, or SUBMIT_BUTTON_SELECTOR are wrong for the current UI — "
        "re-record with `playwright codegen` on the staging explorer URL and update the CONFIG block in the script.",
        evidence,
    )


def _clear_address_field(ctx: ExplorerCtx) -> None:
    try:
        inp = _locate(ctx, ADDRESS_INPUT_SELECTOR)
        inp.click(timeout=3_000)
        inp.fill("")
        if CLEAR_BUTTON_SELECTOR:
            btn = ctx.locator(CLEAR_BUTTON_SELECTOR).first
            if btn.count() > 0:
                btn.click(timeout=2_000)
    except Exception:
        pass


def _submit_and_collect(
    page: Page,
    ctx: ExplorerCtx,
    address: str,
    artifacts_dir: Path,
    case_id: int,
    used_iframe: bool,
    had_login_config: bool,
) -> tuple[
    bool,
    Optional[str],
    Dict[str, str],
    Optional[str],
    str,
    Optional[str],
    str,
    str,
    str,
    bool,
    Dict[str, Any],
]:
    error_msg: Optional[str] = None
    _clear_address_field(ctx)
    inp = _locate(ctx, ADDRESS_INPUT_SELECTOR)
    inp.fill(address, timeout=ACTION_TIMEOUT_MS)
    page.wait_for_timeout(POST_SUBMIT_STABILITY_MS)

    try:
        sub = _locate(ctx, SUBMIT_BUTTON_SELECTOR)
        sub.click(timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:
        error_msg = f"submit_click_failed: {exc}"
        try:
            inp.press("Enter", timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass

    page.wait_for_timeout(POST_SUBMIT_STABILITY_MS)

    _wait_for_explorer_outcome(ctx, page)

    err = _extract_error(ctx)
    name = _extract_neighborhood_name(ctx)
    metrics = _extract_metrics(ctx)
    raw_blob = ""
    try:
        raw_blob = _safe_inner_text(ctx.locator("body").first)
    except Exception:
        try:
            raw_blob = _safe_inner_text(page.locator("body").first)
        except Exception:
            raw_blob = ""

    login_wall = "sign in to your account" in (raw_blob or "").lower()
    if login_wall:
        success = False
        error_msg = error_msg or "login_wall_detected_in_dom"
    else:
        success = bool(name) and not err
        if err:
            success = False
            error_msg = error_msg or err
        if not name and not err:
            if len((raw_blob or "").split()) < 8:
                success = False
                error_msg = error_msg or "no_clear_result"

    page_url = page.url
    outcome_code, diagnosis, evidence = _build_outcome(
        login_wall=login_wall,
        had_login_config=had_login_config,
        err_ui=err,
        name=name,
        metrics=metrics,
        raw_blob=raw_blob,
        error_msg=error_msg,
        success=success,
        page_url=page_url,
        used_iframe=used_iframe,
    )

    shot: Optional[str] = None
    if not success:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        shot = str(artifacts_dir / f"fail_{case_id:05d}.png")
        try:
            page.screenshot(path=shot, full_page=True)
        except Exception:
            shot = None

    return success, name, metrics, error_msg, raw_blob[:12_000], shot, outcome_code, diagnosis, page_url, used_iframe, evidence


def run_one(
    page: Page,
    case_id: int,
    state: str,
    address: str,
    source_tag: str,
    artifacts_dir: Path,
    had_login_config: bool,
) -> TestRow:
    started = time.perf_counter()
    last_exc: Optional[str] = None
    last: Optional[TestRow] = None
    for attempt in range(1, RETRIES_PER_ADDRESS + 1):
        try:
            page.goto(STAGING_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            ctx, used_iframe = _get_context(page)
            (
                ok,
                name,
                metrics,
                err,
                raw,
                shot,
                outcome_code,
                diagnosis,
                page_url,
                used_iframe_flag,
                evidence,
            ) = _submit_and_collect(
                page,
                ctx,
                address,
                artifacts_dir,
                case_id,
                used_iframe,
                had_login_config,
            )
            dur_ms = (time.perf_counter() - started) * 1000.0
            row = TestRow(
                id=case_id,
                address=address,
                state=state,
                success=ok,
                duration_ms=round(dur_ms, 2),
                neighborhood_name=name,
                metrics=metrics,
                error_message=err,
                screenshot_path=None if ok else shot,
                extracted_raw=raw,
                retry_count=attempt - 1,
                address_source=source_tag,
                outcome_code=outcome_code,
                diagnosis=diagnosis,
                page_url=page_url,
                used_iframe=used_iframe_flag,
                evidence=evidence,
            )
            if ok:
                return row
            last = row
            last_exc = err or "unsuccessful_result"
        except Exception as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
        time.sleep(RETRY_BASE_SLEEP_SEC * attempt)

    dur_ms = (time.perf_counter() - started) * 1000.0
    shot_path: Optional[str] = None
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        shot_path = str(artifacts_dir / f"fail_{case_id:05d}_exception.png")
        page.screenshot(path=shot_path, full_page=True)
    except Exception:
        shot_path = None
    if last is not None:
        return TestRow(
            id=case_id,
            address=address,
            state=state,
            success=False,
            duration_ms=round(dur_ms, 2),
            neighborhood_name=last.neighborhood_name,
            metrics=last.metrics,
            error_message=last_exc or last.error_message,
            screenshot_path=last.screenshot_path or shot_path,
            extracted_raw=last.extracted_raw,
            retry_count=RETRIES_PER_ADDRESS,
            address_source=source_tag,
            outcome_code="retry_exhausted",
            diagnosis=(
                f"Still failing after {RETRIES_PER_ADDRESS} full attempts. Last error: {last_exc or last.error_message}. "
                f"Previous diagnosis: {last.diagnosis}"
            ),
            page_url=last.page_url,
            used_iframe=last.used_iframe,
            evidence={**last.evidence, "retry_exhausted": True, "last_error": last_exc},
        )
    return TestRow(
        id=case_id,
        address=address,
        state=state,
        success=False,
        duration_ms=round(dur_ms, 2),
        neighborhood_name=None,
        metrics={},
        error_message=last_exc or "unknown_failure",
        screenshot_path=shot_path,
        extracted_raw=traceback.format_exc()[-12_000:],
        retry_count=RETRIES_PER_ADDRESS,
        address_source=source_tag,
        outcome_code="exception",
        diagnosis=f"A Playwright exception escaped the inner retry loop: {last_exc}",
        page_url=getattr(page, "url", "") or "",
        used_iframe=False,
        evidence={"exception": last_exc or "unknown"},
    )


def compute_meta(
    rows: List[TestRow],
    staging_url: str,
    *,
    run_status: str = "complete",
    planned_total: Optional[int] = None,
    started_at: Optional[str] = None,
    current: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    success_n = sum(1 for r in rows if r.success)
    fail_n = len(rows) - success_n
    dur_avg = (sum(r.duration_ms for r in rows) / len(rows)) if rows else 0.0
    states_cov = len({r.state for r in rows})
    planned = planned_total if planned_total is not None else len(rows)
    meta: Dict[str, Any] = {
        "generated_at": _utc_now_iso() if run_status == "complete" else (started_at or _utc_now_iso()),
        "staging_url": staging_url,
        "total_tests": len(rows),
        "planned_total": planned,
        "completed": len(rows),
        "run_status": run_status,
        "success_count": success_n,
        "failure_count": fail_n,
        "success_rate": round((success_n / len(rows)) * 100, 3) if rows else 0.0,
        "avg_duration_ms": round(dur_avg, 2),
        "states_covered": states_cov,
    }
    if started_at:
        meta["started_at"] = started_at
    if current is not None:
        meta["current"] = current
    if extra:
        meta.update(extra)
    return meta


def write_outputs(
    rows: List[TestRow],
    staging_url: str,
    out_path: Path,
    *,
    login_env_configured: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "meta": compute_meta(
            rows,
            staging_url,
            run_status="complete",
            planned_total=len(rows),
            extra=_run_meta_extras(login_env_configured),
        ),
        "results": [asdict(r) for r in rows],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_live_state_file(
    rows: List[TestRow],
    staging_url: str,
    out_path: Path,
    *,
    run_status: str,
    planned_total: int,
    started_at: str,
    current: Optional[Dict[str, Any]] = None,
    meta_extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "meta": compute_meta(
            rows,
            staging_url,
            run_status=run_status,
            planned_total=planned_total,
            started_at=started_at,
            current=current,
            extra=meta_extra,
        ),
        "results": [asdict(r) for r in rows],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def start_dashboard_server(dashboard_dir: Path, port: int) -> ThreadingHTTPServer:
    root = dashboard_dir.resolve()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def summarize_and_print(meta: Dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"  Total tests:      {meta['total_tests']}")
    print(f"  Success:          {meta['success_count']}  ({meta['success_rate']}%)")
    print(f"  Failures:         {meta['failure_count']}")
    print(f"  Avg duration:     {meta['avg_duration_ms']} ms")
    print(f"  States in run:    {meta['states_covered']}")
    print("=" * 72 + "\n")


def _prompt_address_count() -> int:
    print()
    while True:
        raw = input(
            f"How many addresses to test? ({MIN_ADDRESS_COUNT}-{MAX_ADDRESS_COUNT}) "
            f"[press Enter for {DEFAULT_ADDRESS_COUNT}]: "
        ).strip()
        if not raw:
            return DEFAULT_ADDRESS_COUNT
        try:
            return _parse_address_count(raw)
        except argparse.ArgumentTypeError as e:
            print(f"  {e}")


def _report_address_result(case_id: int, state: str, address: str, row: TestRow) -> str:
    lines = [
        "",
        "=" * 72,
        f"  RESULT #{case_id}  |  state={state}  |  {row.duration_ms} ms  |  success={row.success}",
        "=" * 72,
        f"  Address:     {address}",
        f"  Outcome:     {row.outcome_code}",
        f"  Neighborhood:{row.neighborhood_name or '—'}",
    ]
    if row.metrics:
        lines.append("  Metrics:")
        for k, v in list(row.metrics.items())[:12]:
            lines.append(f"    · {v}")
        if len(row.metrics) > 12:
            lines.append(f"    · … (+{len(row.metrics) - 12} more)")
    else:
        lines.append("  Metrics:     (none extracted)")
    err = row.error_message or "—"
    if len(err) > 240:
        err = err[:237] + "…"
    lines.append(f"  Error text:  {err}")
    diag = row.diagnosis or "—"
    if len(diag) > 320:
        diag = diag[:317] + "…"
    lines.append(f"  Diagnosis:   {diag}")
    lines.append(f"  Page URL:    {row.page_url or '—'}")
    lines.append(f"  Iframe:      {'yes' if row.used_iframe else 'no'}")
    if row.screenshot_path:
        lines.append(f"  Screenshot:  {row.screenshot_path}")
    lines.append("=" * 72)
    return "\n".join(lines)


def _str2bool(value: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("1", "true", "t", "yes", "y"):
        return True
    if v in ("0", "false", "f", "no", "n"):
        return False
    raise argparse.ArgumentTypeError("expected true/false for --headless")


def _parse_address_count(value: str) -> int:
    try:
        n = int(str(value).strip(), 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--count must be an integer") from exc
    if n < MIN_ADDRESS_COUNT or n > MAX_ADDRESS_COUNT:
        raise argparse.ArgumentTypeError(
            f"--count must be between {MIN_ADDRESS_COUNT} and {MAX_ADDRESS_COUNT} (got {n})"
        )
    return n


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dream Neighborhood Explorer load test")
    p.add_argument(
        "--count",
        type=_parse_address_count,
        default=None,
        metavar="N",
        help=(
            f"How many addresses ({MIN_ADDRESS_COUNT}-{MAX_ADDRESS_COUNT}). "
            f"Omit this flag in a normal terminal to be prompted. "
            f"Non-interactive stdin defaults to {DEFAULT_ADDRESS_COUNT}."
        ),
    )
    p.add_argument(
        "--headless",
        type=_str2bool,
        default=False,
        nargs="?",
        const=True,
        help="true|false (default: false — browser visible; CI passes --headless true)",
    )
    p.add_argument("--output", type=str, default=str(_REPO_ROOT / "results.json"))
    p.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between tests")
    p.add_argument("--headed", action="store_true", help="Run headed (shows the browser). Overrides --headless.")
    p.add_argument(
        "--artifacts-dir",
        type=str,
        default=str(_DEFAULT_ARTIFACTS),
        help="Directory for failure screenshots",
    )
    p.add_argument(
        "--live-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Serve dashboard/ on 127.0.0.1:PORT and refresh live_state.json after each test. Open /?live=1",
    )
    args = p.parse_args()
    if args.headed:
        args.headless = False
    return args


def main() -> int:
    _load_repo_dotenv()
    _line_buffer_stdio()
    args = parse_args()
    if args.count is None:
        if sys.stdin.isatty():
            args.count = _prompt_address_count()
        else:
            args.count = DEFAULT_ADDRESS_COUNT
    out_path = Path(args.output).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    dashboard_dir = (_REPO_ROOT / "dashboard").resolve()
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    live_path = dashboard_dir / "live_state.json"

    runlist = build_address_runlist(min(args.count, MAX_ADDRESS_COUNT))
    if len(runlist) > args.count:
        runlist = runlist[: args.count]

    had_login_config = bool(
        os.environ.get("DREAM_NEIGHBORHOOD_EMAIL", "").strip()
        and os.environ.get("DREAM_NEIGHBORHOOD_PASSWORD", "").strip()
    )
    if not had_login_config:
        print(
            "\n[!] DREAM_NEIGHBORHOOD_EMAIL / DREAM_NEIGHBORHOOD_PASSWORD not both set — "
            "staging may return a sign-in page (reported as outcome_code=login_wall). "
            "See docs/RUNNING.md.\n"
        )
        if os.environ.get("GITHUB_ACTIONS") == "true":
            est_min = max(1, int(len(runlist) * 2))
            print(
                "[!] You are in GitHub Actions WITHOUT login secrets.\n"
                f"    Expect on the order of ~2 minutes × {len(runlist)} tests ≈ {est_min}+ minutes of wall time.\n"
                "    Add repo secrets DREAM_NEIGHBORHOOD_EMAIL and DREAM_NEIGHBORHOOD_PASSWORD, or read docs/START-HERE.md.\n"
            )

    meta_x = _run_meta_extras(had_login_config)

    rows: List[TestRow] = []
    started_at = _utc_now_iso()
    live_server: Optional[ThreadingHTTPServer] = None
    if args.live_port is not None and args.live_port > 0:
        live_server = start_dashboard_server(dashboard_dir, args.live_port)
        print(f"\nLive dashboard: http://127.0.0.1:{args.live_port}/?live=1\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(args.headless))
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()

        _maybe_login(page)

        if args.live_port is not None and args.live_port > 0:
            write_live_state_file(
                rows,
                STAGING_URL,
                live_path,
                run_status="running",
                planned_total=len(runlist),
                started_at=started_at,
                current={"phase": "starting", "message": "Starting test loop…"},
                meta_extra=meta_x,
            )

        for i, (state, address, src) in enumerate(
            tqdm(runlist, desc="Neighborhood tests", unit="addr"),
            start=1,
        ):
            if args.live_port is not None and args.live_port > 0:
                write_live_state_file(
                    rows,
                    STAGING_URL,
                    live_path,
                    run_status="running",
                    planned_total=len(runlist),
                    started_at=started_at,
                    current={"id": i, "state": state, "address": address, "phase": "running"},
                    meta_extra=meta_x,
                )
            row = run_one(page, i, state, address, src, artifacts_dir, had_login_config)
            rows.append(row)
            tqdm.write(_report_address_result(i, state, address, row))
            sys.stdout.flush()
            sys.stderr.flush()
            if args.live_port is not None and args.live_port > 0:
                write_live_state_file(
                    rows,
                    STAGING_URL,
                    live_path,
                    run_status="running",
                    planned_total=len(runlist),
                    started_at=started_at,
                    current=None,
                    meta_extra=meta_x,
                )
            if args.delay > 0:
                time.sleep(float(args.delay))

        context.close()
        browser.close()

    if args.live_port is not None and args.live_port > 0:
        write_live_state_file(
            rows,
            STAGING_URL,
            live_path,
            run_status="complete",
            planned_total=len(runlist),
            started_at=started_at,
            current=None,
            meta_extra=meta_x,
        )

    meta = write_outputs(rows, STAGING_URL, out_path, login_env_configured=had_login_config)["meta"]
    summarize_and_print(meta)

    dash_json = dashboard_dir / "results.json"
    shutil.copyfile(out_path, dash_json)

    print("\n✅ Testing complete!\n")
    print(f"   Wrote machine JSON: {out_path}")
    print(f"   Dashboard data:     {dash_json}")
    if args.live_port is not None and args.live_port > 0:
        print(f"   Live snapshot:      {live_path} (final)")
        print("   The local server has exited; reopen live mode on the next run with --live-port.\n")
    print()
    print("✅ Dashboard ready at: dashboard/index.html")
    print()
    print("To deploy to Netlify:")
    print("  1. Drag the entire 'dashboard' folder to https://app.netlify.com/drop")
    print("     or")
    print("  2. Push the repo to GitHub and connect the 'dashboard' directory in Netlify")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
