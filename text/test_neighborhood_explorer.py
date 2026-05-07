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
    set DREAM_NEIGHBORHOOD_EMAIL=you@example.com
    set DREAM_NEIGHBORHOOD_PASSWORD=secret

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
  python text/test_neighborhood_explorer.py --count 10 --headed
  python text/test_neighborhood_explorer.py --count 1000 --output my_results.json --delay 0.5
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from faker import Faker
from random_address import real_random_address, real_random_address_by_state
from tqdm import tqdm

from playwright.sync_api import FrameLocator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# ---------------------------------------------------------------------------
# Tunable selectors & timing — update after running Playwright codegen
# ---------------------------------------------------------------------------
STAGING_URL = (
    "https://staging.dreamneighborhood.com/a/drea-neighborhood-treasure-coast/core/explore-neighborhoods/"
)
LOGIN_URL = "https://staging.dreamneighborhood.com/accounts/login/"

# Set to None (or "") to use the top-level page; else first matching iframe hosts the widget.
IFRAME_SELECTOR: Optional[str] = "iframe"

# Locators are resolved against either Page or FrameLocator.
ADDRESS_INPUT_SELECTOR = (
    'input[placeholder*="address" i], input[placeholder*="search" i], '
    'input[type="search"], input[name="address"], input[name="q"], textarea'
)
SUBMIT_BUTTON_SELECTOR = (
    'button[type="submit"], button:has-text("Search"), button:has-text("Explore"), '
    'button:has-text("Go"), [role="button"]:has-text("Search")'
)
CLEAR_BUTTON_SELECTOR = 'button:has-text("Clear"), button[aria-label*="clear" i]'

RESULT_READY_SELECTOR = (
    '[class*="result" i], [class*="neighborhood" i], [data-testid*="result" i], '
    'main article, [role="article"]'
)
NEIGHBORHOOD_NAME_SELECTOR = "h1, h2, h3, [class*='neighborhood' i], [class*='title' i]"
METRIC_ROW_SELECTOR = '[class*="score" i], [class*="metric" i], li:has-text("/100"), dd, dt'
ERROR_SELECTOR = '[class*="error" i], [class*="alert" i], [role="alert"], .text-danger'

LOGIN_EMAIL_SELECTOR = 'input[name="login"], input#id_login, input[type="email"], input[autocomplete="username"]'
LOGIN_PASSWORD_SELECTOR = 'input[name="password"], input#id_password, input[type="password"]'
LOGIN_SUBMIT_SELECTOR = 'button[type="submit"], button:has-text("Sign in")'

NAVIGATION_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 25_000
RESULT_TIMEOUT_MS = 45_000
RETRIES_PER_ADDRESS = 3
RETRY_BASE_SLEEP_SEC = 0.75
POST_SUBMIT_STABILITY_MS = 400

# US state codes (50 states). ~count/20 per state when count=1000.
ALL_US_STATE_CODES: List[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_ARTIFACTS = _REPO_ROOT / "artifacts" / "screenshots"


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


def _maybe_login(page: Page) -> None:
    email = os.environ.get("DREAM_NEIGHBORHOOD_EMAIL", "").strip()
    password = os.environ.get("DREAM_NEIGHBORHOOD_PASSWORD", "").strip()
    if not email or not password:
        return
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    page.locator(LOGIN_EMAIL_SELECTOR).first.fill(email, timeout=ACTION_TIMEOUT_MS)
    page.locator(LOGIN_PASSWORD_SELECTOR).first.fill(password, timeout=ACTION_TIMEOUT_MS)
    page.locator(LOGIN_SUBMIT_SELECTOR).first.click(timeout=ACTION_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT_MS)


def _get_context(page: Page) -> Union[Page, FrameLocator]:
    if not IFRAME_SELECTOR:
        return page
    try:
        page.wait_for_selector(IFRAME_SELECTOR, timeout=12_000)
    except PlaywrightTimeoutError:
        return page
    if page.locator(IFRAME_SELECTOR).count() < 1:
        return page
    return page.frame_locator(IFRAME_SELECTOR)


def _locate(ctx: Union[Page, FrameLocator], selector: str):
    return ctx.locator(selector).first


def _safe_inner_text(locator) -> str:
    try:
        if locator.count() == 0:
            return ""
        return (locator.inner_text(timeout=3_000) or "").strip()
    except Exception:
        return ""


def _extract_neighborhood_name(ctx: Union[Page, FrameLocator]) -> Optional[str]:
    txt = _safe_inner_text(ctx.locator(NEIGHBORHOOD_NAME_SELECTOR).first)
    return txt or None


def _extract_metrics(ctx: Union[Page, FrameLocator]) -> Dict[str, str]:
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


def _extract_error(ctx: Union[Page, FrameLocator]) -> Optional[str]:
    err = _safe_inner_text(ctx.locator(ERROR_SELECTOR).first)
    return err or None


def _clear_address_field(ctx: Union[Page, FrameLocator]) -> None:
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
    ctx: Union[Page, FrameLocator],
    address: str,
    artifacts_dir: Path,
    case_id: int,
) -> tuple[bool, Optional[str], Dict[str, str], Optional[str], str, Optional[str]]:
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

    # Result wait: visible container or settle on error banner.
    try:
        ctx.locator(RESULT_READY_SELECTOR).first.wait_for(state="visible", timeout=RESULT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass

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
        error_msg = error_msg or "login_required_or_session_expired"
    else:
        success = bool(name) and not err
        if err:
            success = False
            error_msg = error_msg or err
        if not name and not err:
            if len((raw_blob or "").split()) < 8:
                success = False
                error_msg = error_msg or "no_clear_result"

    shot: Optional[str] = None
    if not success:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        shot = str(artifacts_dir / f"fail_{case_id:05d}.png")
        try:
            page.screenshot(path=shot, full_page=True)
        except Exception:
            shot = None

    return success, name, metrics, error_msg, raw_blob[:12_000], shot


def run_one(
    page: Page,
    case_id: int,
    state: str,
    address: str,
    source_tag: str,
    artifacts_dir: Path,
) -> TestRow:
    started = time.perf_counter()
    last_exc: Optional[str] = None
    last: Optional[TestRow] = None
    for attempt in range(1, RETRIES_PER_ADDRESS + 1):
        try:
            page.goto(STAGING_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            ctx = _get_context(page)
            ok, name, metrics, err, raw, shot = _submit_and_collect(
                page, ctx, address, artifacts_dir, case_id
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
    )


def write_outputs(rows: List[TestRow], staging_url: str, out_path: Path) -> Dict[str, Any]:
    success_n = sum(1 for r in rows if r.success)
    fail_n = len(rows) - success_n
    dur_avg = (sum(r.duration_ms for r in rows) / len(rows)) if rows else 0.0
    states_cov = len({r.state for r in rows})
    payload: Dict[str, Any] = {
        "meta": {
            "generated_at": _utc_now_iso(),
            "staging_url": staging_url,
            "total_tests": len(rows),
            "success_count": success_n,
            "failure_count": fail_n,
            "success_rate": round((success_n / len(rows)) * 100, 3) if rows else 0.0,
            "avg_duration_ms": round(dur_avg, 2),
            "states_covered": states_cov,
        },
        "results": [asdict(r) for r in rows],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


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


def _str2bool(value: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("1", "true", "t", "yes", "y"):
        return True
    if v in ("0", "false", "f", "no", "n"):
        return False
    raise argparse.ArgumentTypeError("expected true/false for --headless")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dream Neighborhood Explorer load test")
    p.add_argument("--count", type=int, default=1000, help="Number of addresses to test")
    p.add_argument("--headless", type=_str2bool, default=True, nargs="?", const=True, help="true|false (default: true)")
    p.add_argument("--output", type=str, default=str(_REPO_ROOT / "results.json"))
    p.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between tests")
    p.add_argument("--headed", action="store_true", help="Run headed (shows the browser). Overrides --headless.")
    p.add_argument(
        "--artifacts-dir",
        type=str,
        default=str(_DEFAULT_ARTIFACTS),
        help="Directory for failure screenshots",
    )
    args = p.parse_args()
    if args.headed:
        args.headless = False
    return args


def main() -> int:
    args = parse_args()
    out_path = Path(args.output).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()

    runlist = build_address_runlist(min(args.count, 50_000))
    if len(runlist) > args.count:
        runlist = runlist[: args.count]

    rows: List[TestRow] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(args.headless))
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()

        _maybe_login(page)

        for i, (state, address, src) in enumerate(
            tqdm(runlist, desc="Neighborhood tests", unit="addr"),
            start=1,
        ):
            row = run_one(page, i, state, address, src, artifacts_dir)
            rows.append(row)
            if args.delay > 0:
                time.sleep(float(args.delay))

        context.close()
        browser.close()

    meta = write_outputs(rows, STAGING_URL, out_path)["meta"]
    summarize_and_print(meta)

    dashboard_dir = _REPO_ROOT / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    dash_json = dashboard_dir / "results.json"
    shutil.copyfile(out_path, dash_json)

    print("\n✅ Testing complete!\n")
    print(f"   Wrote machine JSON: {out_path}")
    print(f"   Dashboard data:     {dash_json}")
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
