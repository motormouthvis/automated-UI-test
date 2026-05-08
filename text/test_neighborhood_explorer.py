#!/usr/bin/env python3
"""
Dream Neighborhood Explorer — staging load test (Playwright, sync API)

This script drives **only** the **Map and Summary** experience (default map tab + right-hand
summary panel). It does **not** open Demographics, Schools, Commutes, or other tabs.

Flow per address:
  1. Ensure **Map and Summary** is active.
  2. Enter the address (Places), choose a suggestion, click **View Neighborhood Data**.
  3. Assert the **summary panel** shows the expected headings and **numeric** KPIs (see CONFIG).
     **Not available** (or any missing dollar / percent where expected) is a **failed** test.
  4. Confirm the panel still reflects the entered place (token alignment on the title line).

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

  Less chatty local logs (one line per address; full detail still in results JSON):
  python text/test_neighborhood_explorer.py --headed -q

  If you omit --count, you are prompted in the terminal (1-1000). CI/GitHub must pass --count.
  python text/test_neighborhood_explorer.py --count 1000 --output my_results.json --delay 0.5

  --count is 1-1000. Omit --count in a terminal to be prompted. Non-TTY uses 2.
  The dashboard can use ?count=N or the smoke-size control to copy the same number.

  Live dashboard (local): serves dashboard/ on http://127.0.0.1:<port> and
  updates live_state.json after every address. Open:
    http://127.0.0.1:8765/?live=1
  (Netlify stays static; live mode is only while the Python process runs.)

================================================================================
SPEED (multi-address runs)
================================================================================
  Explorer navigations use ``wait_until=\"commit\"`` and ``EXPLORER_GOTO_TIMEOUT_MS`` (login still uses
  ``NAVIGATION_TIMEOUT_MS``). Test 1 skips ``goto`` when credentials already opened the explorer;
  tests 2…N always ``goto`` so the iframe is not stuck on the prior lookup.

================================================================================
WAITING FOR RESULTS
================================================================================
  After submit, the script waits for an error banner, a non-empty neighborhood
  title, or meaningful metric rows — first waiting on the widget heading line when
  practical, then polling until RESULT_TIMEOUT_MS. Optional LOADING_SELECTOR uses
  short probes; see ``_wait_for_loading_done``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from faker import Faker
from random_address import real_random_address, real_random_address_by_state
from tqdm import tqdm

from playwright.sync_api import Frame, FrameLocator, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ExplorerCtx = Union[Page, Frame, FrameLocator, Locator]

# ---------------------------------------------------------------------------
# Tunable selectors & timing — update after running Playwright codegen
# ---------------------------------------------------------------------------
STAGING_URL = (
    "https://staging.dreamneighborhood.com/a/drea-neighborhood-treasure-coast/core/explore-neighborhoods/"
)
LOGIN_URL = "https://staging.dreamneighborhood.com/accounts/login/"

# Optional explicit iframe CSS (e.g. "iframe#widget"). If None, the runner scans child frames for the map
# search / Places input (…/explore-neighborhoods embeds the control in a frame, not in <main> DOM).
IFRAME_SELECTOR: Optional[str] = None
# When not using an iframe, scope "read" locators to app content (right-hand panel). Interaction may still use a child frame.
WIDGET_HOST_SELECTOR: Optional[str] = "main"
# Time to wait for the embedded explorer frame to attach and render the search input.
EXPLORER_IFRAME_PROBE_MS = 8_000
# Staging loads the explorer in a same-origin iframe: …/widget/?partner=… — map search is ``#location-input``
# (not Google ``pac-target-input``; ``:visible`` can fail while layout settles — use ``attached`` + force-click).
EXPLORER_WIDGET_IFRAME_SELECTOR = 'iframe[src*="/widget/"]'
WIDGET_MAP_SEARCH_INPUT_SELECTOR = "#location-input"
# ``#location-input`` may report 0×0 rect while still focusable; cap probes — happy path resolves quickly.
ATTACH_INPUT_TIMEOUT_MS = 8_000

# Locators are resolved against Page, FrameLocator, or a scoped Locator (e.g. <main>).
# Google Places Autocomplete adds .pac-target-input; Dream Neighborhood labels the map search
# "Current location" (often without placeholder=address).
ADDRESS_INPUT_SELECTOR = (
    "#location-input, "
    "input#location-input, "
    'input[placeholder*="Enter a location" i], '
    "input.pac-target-input, "
    'input[aria-label*="current location" i], input[placeholder*="location" i], '
    'input[placeholder*="address" i], input[placeholder*="search" i], '
    'input[type="search"], input[name="address"], input[name="q"], '
    'input[aria-label*="address" i], input[aria-label*="search" i], textarea'
)
PLACES_SUGGESTION_FALLBACK_SELECTORS: List[str] = [
    ".pac-container .pac-item",
    '[role="listbox"] [role="option"]',
    '[role="listbox"] li',
    "div.pac-item",
]
# Keystrokes trigger Google's debounced fetch; delay=0 is fastest once the map search is revealed.
ADDRESS_TYPE_DELAY_MS = 0
# After typing / paste, wait at least this long then poll for Places UI (caps total spinner vs fixed sleep).
PLACES_MIN_WAIT_MS = 40
PLACES_MAX_WAIT_MS = 320
SUBMIT_BUTTON_SELECTOR = (
    'button:has-text("View Neighborhood Data"), '
    '[role="button"]:has-text("View Neighborhood Data"), '
    'button[type="submit"], button:has-text("Search"), button:has-text("Explore"), '
    'button:has-text("Go"), [role="button"]:has-text("Search")'
)
# Full-width CTA can be slow to paint / not \"visible\" to actionability yet.
SUBMIT_CLICK_TIMEOUT_MS = 5_000
CLEAR_BUTTON_SELECTOR = 'button:has-text("Clear"), button[aria-label*="clear" i]'
# Staging shows the seed street in ``#current-location-text`` while ``#location-input`` sits under ``.hidden`` until opened.
MAP_SEARCH_REVEAL_SELECTORS: Tuple[str, ...] = ("#current-location-text", "#location-content")

RESULT_READY_SELECTOR = (
    '[class*="result" i], [class*="neighborhood" i], [data-testid*="result" i], '
    'main article, [role="article"]'
)
NEIGHBORHOOD_NAME_SELECTOR = "h1, h2, h3, [class*='neighborhood' i], [class*='title' i]"
# Cards often use “stat”/“insight”/grid cells rather than dd/dt; keep broad enough for the widget panel.
METRIC_ROW_SELECTOR = (
    '[class*="score" i], [class*="metric" i], [class*="stat" i], [class*="insight" i], '
    '[class*="kpi" i], [class*="card" i] p, [class*="card" i] div, '
    'li:has-text("/100"), dd, dt'
)
ERROR_SELECTOR = '[class*="error" i], [class*="alert" i], [role="alert"], .text-danger'
# If the widget shows a spinner / aria-busy while fetching, tune this (set to "" to disable).
LOADING_SELECTOR = '[class*="loading" i], [class*="spinner" i], [aria-busy="true"], [data-loading="true"]'

# ---- Map + Summary panel (right of map): required copy and sanity ranges ----
# Widget ``innerText`` often interleaves **value then label** (e.g. ``$412,000`` on the line
# before ``Median Home Price``). Money helpers must scan *before* the label as well as after.
SUMMARY_REQUIRED_PHRASES: Tuple[str, ...] = (
    "summary for selected area",
    "about the data",
    "household income",
    "median home price",
    "occupied by owners",
    "has college degree",
    "finished high school",
    "employed",
)
# Some areas show **Median Age** instead of **Median Rent** in the summary stack.
SUMMARY_RENT_OR_AGE_PHRASES: Tuple[str, ...] = ("median rent", "median age")
SUMMARY_INCOME_MIN = 5_000
SUMMARY_INCOME_MAX = 3_000_000
SUMMARY_HOME_PRICE_MIN = 10_000
SUMMARY_HOME_PRICE_MAX = 80_000_000
SUMMARY_RENT_MIN = 50
SUMMARY_RENT_MAX = 25_000
SUMMARY_MEDIAN_AGE_MIN = 1
SUMMARY_MEDIAN_AGE_MAX = 120
SUMMARY_PCT_MIN = 0
SUMMARY_PCT_MAX = 100

NAVIGATION_TIMEOUT_MS = 60_000
# ``run_one`` only — avoid waiting the full minute on a hung embed when the shell HTML is already up.
EXPLORER_GOTO_TIMEOUT_MS = 18_000
# Login form can be slow; keep this separate from fast explorer interactions.
LOGIN_ACTION_TIMEOUT_MS = 25_000
# Explorer: fail fast when selectors/autocomplete are wrong (user preference ~3s per action).
ACTION_TIMEOUT_MS = 2_000
# Summary KPIs usually render within a few seconds; cap wait so each address stays ~5–10s excluding cold navigation.
RESULT_TIMEOUT_MS = 6_000
RETRIES_PER_ADDRESS = 1
RETRY_BASE_SLEEP_SEC = 0.25
POST_SUBMIT_STABILITY_MS = 15
# Short pause after CTA so the request starts — summary copy lives in the /widget/ iframe.
MIN_POST_SUBMIT_WAIT_MS = 20
# Light polling interval while waiting for summary copy (avoid heavy DOM walks each tick).
RESULT_POLL_MS = 90
# After success/error is detected, brief pause so late-bound text/metrics can render.
STABILIZE_AFTER_OUTCOME_MS = 100

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
    form.wait_for(state="visible", timeout=LOGIN_ACTION_TIMEOUT_MS)
    # Django allauth — prefer id_login / name=login (not a generic type=email elsewhere on page).
    form.locator('input#id_login, input[name="login"]').first.fill(email, timeout=LOGIN_ACTION_TIMEOUT_MS)
    form.locator('input#id_password, input[name="password"]').first.fill(
        password, timeout=LOGIN_ACTION_TIMEOUT_MS
    )
    form.locator('button[type="submit"], input[type="submit"]').first.click(timeout=LOGIN_ACTION_TIMEOUT_MS)
    # Login often redirects to …/core/ (hub). SPA sites also keep the network busy, so
    # wait_for_load_state("networkidle") commonly times out after "load" already fired.
    try:
        page.wait_for_load_state("load", timeout=LOGIN_ACTION_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    # Always open the explorer deep link (embed + map); do not rely on post-login default route.
    page.goto(STAGING_URL, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)


def _interaction_is_iframe_surface(ctx: ExplorerCtx) -> bool:
    """True when typing runs in a Frame/FrameLocator (embedded document), not the top-level Page."""
    return isinstance(ctx, (Frame, FrameLocator))


def _widget_host(page: Page) -> ExplorerCtx:
    """Prefer <main> for dashboard / right-hand panel reads."""
    if not WIDGET_HOST_SELECTOR:
        return page
    host = page.locator(WIDGET_HOST_SELECTOR).first
    try:
        host.wait_for(state="visible", timeout=5_000)
        return host
    except PlaywrightTimeoutError:
        return page


def _explorer_surfaces(page: Page) -> tuple[ExplorerCtx, ExplorerCtx, bool]:
    """
    Return (interaction_context, read_context, used_iframe).

    Staging embeds the map at ``…/widget/?…`` in ``iframe[src*="/widget/"]``. The address field is
    ``#location-input`` inside that frame (confirmed via DOM probe — not ``<main>``, not ``pac-target``).
    """
    read = _widget_host(page)
    try:
        page.wait_for_selector(EXPLORER_WIDGET_IFRAME_SELECTOR, timeout=min(EXPLORER_IFRAME_PROBE_MS, 8_000))
        fl = page.frame_locator(EXPLORER_WIDGET_IFRAME_SELECTOR)
        try:
            fl.locator("#current-location-text").first.wait_for(state="attached", timeout=min(EXPLORER_IFRAME_PROBE_MS, 5_000))
        except PlaywrightTimeoutError:
            pass
        return fl, read, True
    except PlaywrightTimeoutError:
        pass

    if IFRAME_SELECTOR:
        try:
            page.wait_for_selector(IFRAME_SELECTOR, timeout=min(EXPLORER_IFRAME_PROBE_MS, 12_000))
        except PlaywrightTimeoutError:
            pass
        if page.locator(IFRAME_SELECTOR).count() >= 1:
            fl = page.frame_locator(IFRAME_SELECTOR)
            return fl, read, _interaction_is_iframe_surface(fl)

    deadline = time.monotonic() + EXPLORER_IFRAME_PROBE_MS / 1000.0
    # Any visible text field inside a child frame (Places/input attrs vary by build).
    loose = (
        'input:visible:not([type="hidden"]):not([type="file"]):not([type="checkbox"])'
        ':not([type="radio"]):not([type="button"]):not([type="submit"])'
    )
    child_selectors: Tuple[str, ...] = (
        ADDRESS_INPUT_SELECTOR,
        'input[type="search"]',
        'input[autocomplete="off"]',
        'input[aria-autocomplete="list"]',
        loose,
    )
    while time.monotonic() < deadline:
        for frame in [f for f in list(page.frames) if f != page.main_frame]:
            for sel in child_selectors:
                loc = frame.locator(sel).first
                try:
                    loc.wait_for(state="visible", timeout=900)
                    return frame, read, True
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue
        page.wait_for_timeout(250)

    # Light DOM on the top-level page (next to map iframe — not necessarily under <main>).
    page_try: List[tuple[str, Any]] = [
        ("pac-target-input", page.locator("input.pac-target-input").first),
        ("role=searchbox", page.get_by_role("searchbox").first),
        ("role=combobox", page.get_by_role("combobox").first),
        ("type=search", page.locator('input[type="search"]').first),
        ("address_selector", page.locator(ADDRESS_INPUT_SELECTOR).first),
    ]
    for _name, probe in page_try:
        try:
            probe.wait_for(state="visible", timeout=3_000)
            return page, read, False
        except PlaywrightTimeoutError:
            continue

    # Last resort: same selectors scoped to <main> (older layouts).
    try:
        main_scope = page.locator("main").first
        main_scope.wait_for(state="visible", timeout=3_000)
        loc = main_scope.locator(ADDRESS_INPUT_SELECTOR).first
        loc.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
        return main_scope, read, False
    except PlaywrightTimeoutError:
        pass

    return read, read, False


def _locate(ctx: ExplorerCtx, selector: str):
    return ctx.locator(selector).first


def _resolve_address_input(ctx: ExplorerCtx):
    """Find the map search field — staging uses ``#location-input`` in the /widget/ iframe."""
    builders = [
        lambda: ctx.locator(WIDGET_MAP_SEARCH_INPUT_SELECTOR).first,
        lambda: ctx.locator("input#location-input").first,
        lambda: ctx.locator(ADDRESS_INPUT_SELECTOR).first,
        lambda: ctx.locator("input.pac-target-input").first,
        lambda: ctx.get_by_role("searchbox").first,
        lambda: ctx.get_by_role("combobox").first,
        lambda: ctx.locator('input[type="search"]').first,
        lambda: ctx.get_by_label(re.compile(r"current\s+location", re.I)).first,
        lambda: ctx.get_by_placeholder(re.compile(r"search|address|location|Enter a location", re.I)).first,
    ]
    last: Optional[Exception] = None
    for mk in builders:
        try:
            loc = mk()
            loc.wait_for(state="attached", timeout=ATTACH_INPUT_TIMEOUT_MS)
            return loc
        except PlaywrightTimeoutError as exc:
            last = exc
        except Exception as exc:
            last = exc
    raise PlaywrightTimeoutError(
        f"Could not find explorer address control after {len(builders)} strategies: {last!r}"
    ) from last


def _focus_typeable_input(loc: Locator) -> None:
    """Focus without a click — ``#location-input`` can be ``display:inline-block`` with 0×0 box (not \"visible\" to Playwright)."""
    loc.wait_for(state="attached", timeout=ATTACH_INPUT_TIMEOUT_MS)
    try:
        loc.scroll_into_view_if_needed(timeout=ATTACH_INPUT_TIMEOUT_MS)
    except Exception:
        pass
    loc.evaluate("el => { el.focus(); if (typeof el.select === 'function') el.select(); }")


def _clear_input_value(loc: Locator) -> None:
    try:
        loc.fill("", force=True)
    except Exception:
        loc.evaluate(
            """(el) => {
            el.value = '';
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
        )


def _select_first_places_suggestion(
    page: Page,
    interact_ctx: ExplorerCtx,
    address_input: Optional[Locator] = None,
) -> bool:
    """
    Pick the first Google Places (or ARIA listbox) suggestion.
    The list is often portaled to the *parent* document while ``#location-input`` lives in ``/widget/``.
    """
    roots: List[ExplorerCtx] = []
    if not isinstance(interact_ctx, Page):
        roots.append(page)
    roots.append(interact_ctx)
    for root in roots:
        for mk in (
            lambda r=root: r.get_by_role("option").first,
            lambda r=root: r.locator('[role="option"]').first,
            lambda r=root: r.locator('[role="listbox"] li').first,
        ):
            try:
                item = mk()
                item.wait_for(state="visible", timeout=450)
                item.click(timeout=ACTION_TIMEOUT_MS, force=True)
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        for sel in PLACES_SUGGESTION_FALLBACK_SELECTORS:
            item = root.locator(sel).first
            try:
                item.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
                item.click(timeout=ACTION_TIMEOUT_MS, force=True)
                return True
            except PlaywrightTimeoutError:
                continue
    # Prefer keystrokes on the map search field so they stay in the ``/widget/`` frame.
    if address_input is not None:
        try:
            address_input.press("ArrowDown")
            page.wait_for_timeout(90)
            address_input.press("Enter")
            return True
        except Exception:
            pass
    try:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(90)
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _address_match_tokens(address: str) -> List[str]:
    """Tokens to match in a Places row (city, state, ZIP, long street tokens)."""
    raw = [p.strip(",.") for p in address.split() if p.strip(",.")]
    out: List[str] = []
    for p in raw:
        if len(p) < 2:
            continue
        if p.isdigit() and len(p) >= 5:
            out.append(p)
        elif len(p) >= 4 or (len(p) == 2 and p.isalpha()):
            out.append(p)
    return out[-6:]


def _select_best_places_suggestion(
    page: Page,
    interact_ctx: ExplorerCtx,
    address: str,
    address_input: Optional[Locator] = None,
) -> int:
    """
    Pick a Places / list row. Returns:
      2 — clicked an option whose text matched city/ZIP tokens
      1 — clicked a generic first suggestion / pac row
      0 — only keyboard fallback
    """
    want = _address_match_tokens(address)
    want_sorted = sorted({w for w in want if len(w) >= 4 or (w.isdigit() and len(w) >= 5)}, key=len, reverse=True)
    roots: List[ExplorerCtx] = []
    if not isinstance(interact_ctx, Page):
        roots.append(page)
    roots.append(interact_ctx)
    for root in roots:
        try:
            opts = root.locator('[role="option"]')
            for i in range(15):
                try:
                    txt = (opts.nth(i).inner_text(timeout=450) or "").strip()
                except Exception:
                    break
                if txt and want and any(w.lower() in txt.lower() for w in want):
                    opts.nth(i).click(timeout=ACTION_TIMEOUT_MS, force=True)
                    return 2
        except Exception:
            continue
        for tok in want_sorted:
            try:
                hit = root.get_by_text(tok, exact=False).first
                hit.wait_for(state="attached", timeout=500)
                hit.scroll_into_view_if_needed(timeout=1_000)
                hit.click(timeout=ACTION_TIMEOUT_MS, force=True)
                return 2
            except Exception:
                continue
    if _select_first_places_suggestion(page, interact_ctx, address_input):
        return 1
    return 0


# Words that appear in almost every US postal line — useless for stale-panel detection.
_GENERIC_STREET_TOKENS: frozenset[str] = frozenset(
    x.lower()
    for x in (
        "Street",
        "St",
        "Avenue",
        "Ave",
        "Road",
        "Rd",
        "Drive",
        "Dr",
        "Lane",
        "Ln",
        "Boulevard",
        "Blvd",
        "Circle",
        "Cir",
        "Court",
        "Ct",
        "Way",
        "Place",
        "Pl",
        "Parkway",
        "Pkwy",
        "Highway",
        "Hwy",
        "Route",
        "Terrace",
        "Ter",
        "Trail",
        "Trl",
        "North",
        "South",
        "East",
        "West",
        "Northeast",
        "Northwest",
        "Southeast",
        "Southwest",
    )
)


def _strong_address_tokens(address: str) -> List[str]:
    """ZIP and tokens with length ≥ 4 — drop generic street terms that match every address line."""
    return [
        t.lower()
        for t in _address_match_tokens(address)
        if ((t.isdigit() and len(t) >= 5) or len(t) >= 4) and t.lower() not in _GENERIC_STREET_TOKENS
    ]


def _result_aligns_with_target(
    *,
    name: Optional[str],
    metrics: Dict[str, str],
    raw_blob: str,
    input_value: str,
    address: str,
) -> bool:
    """True when the visible outcome still reflects the seed address (not the default Nashville demo)."""
    blob = " ".join([name or "", raw_blob or "", input_value or "", " ".join(metrics.values())]).lower()
    want = _strong_address_tokens(address)
    if not want:
        return True
    zips = [t for t in want if t.isdigit() and len(t) >= 5]
    nonzip = [t for t in want if t not in zips]
    if nonzip:
        return any(t in blob for t in nonzip)
    return any(t in blob for t in zips)


def _is_default_nashville_ui(text: Optional[str]) -> bool:
    """Product ships with Nashville seed in the explorer; don't count it as a real result."""
    if not text:
        return False
    lowx = text.lower()
    return "201" in lowx and "6th" in lowx and "nashville" in lowx


def _react_fill_input(loc: Locator, value: str) -> None:
    """Set controlled React ``<input>`` value so Places / listbox actually fires (value + input event)."""
    loc.evaluate(
        """(el, val) => {
      const proto = window.HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) {
        desc.set.call(el, val);
      } else {
        el.value = val;
      }
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste', data: val }));
    }""",
        value,
    )


def _suggestion_roots(page: Page, interact_ctx: ExplorerCtx) -> List[ExplorerCtx]:
    roots: List[ExplorerCtx] = []
    if not isinstance(interact_ctx, Page):
        roots.append(page)
    roots.append(interact_ctx)
    return roots


def _places_suggestions_visible(page: Page, interact_ctx: ExplorerCtx) -> bool:
    for root in _suggestion_roots(page, interact_ctx):
        for sel in (
            ".pac-container .pac-item",
            "div.pac-item",
            '[role="listbox"] [role="option"]',
            '[role="option"]',
        ):
            try:
                loc = root.locator(sel).first
                if loc.is_visible():
                    return True
            except Exception:
                continue
    return False


def _wait_for_places_suggestions_ready(page: Page, interact_ctx: ExplorerCtx) -> None:
    """
    Wait for Google Places / listbox rows instead of a fixed multi-second sleep.
    Always waits at least ``PLACES_MIN_WAIT_MS`` (debounce), then polls until visible or ``PLACES_MAX_WAIT_MS`` total.
    """
    page.wait_for_timeout(PLACES_MIN_WAIT_MS)
    deadline = time.monotonic() + PLACES_MAX_WAIT_MS / 1000.0
    while time.monotonic() < deadline:
        if _places_suggestions_visible(page, interact_ctx):
            return
        page.wait_for_timeout(35)


def _safe_inner_text(locator, *, timeout_ms: int = 450) -> str:
    try:
        return (locator.inner_text(timeout=timeout_ms) or "").strip()
    except Exception:
        return ""


def _chrome_heading_text(name: Optional[str]) -> bool:
    if not name:
        return True
    n = name.strip().lower()
    return n in ("neighborhood explorer", "dream neighborhood", "map and summary")


def _extract_widget_heading_line(interact_ctx: ExplorerCtx) -> Optional[str]:
    """Prefer the map chrome line — single node, avoids scanning dozens of headings."""
    try:
        t = _safe_inner_text(interact_ctx.locator("#current-location-text").first, timeout_ms=280)
        if t and not _chrome_heading_text(t) and not _is_default_nashville_ui(t):
            return t
    except Exception:
        pass
    return None


def _extract_neighborhood_name(
    ctx: ExplorerCtx,
    *,
    max_elements: int = 10,
    text_timeout_ms: int = 180,
) -> Optional[str]:
    """
    Do not use ``.first`` — in the widget, the first h2 is often the active tab (“Map and Summary”),
    which we treat as chrome. Scan headings until we find a real place title.
    """
    try:
        loc = ctx.locator(NEIGHBORHOOD_NAME_SELECTOR)
        for i in range(max_elements):
            try:
                txt = _safe_inner_text(loc.nth(i), timeout_ms=text_timeout_ms)
            except Exception:
                break
            if not txt:
                continue
            if _chrome_heading_text(txt) or _is_default_nashville_ui(txt):
                continue
            if len(txt) < 2:
                continue
            return txt
    except Exception:
        pass
    return None


def _extract_metrics(ctx: ExplorerCtx) -> Dict[str, str]:
    metrics: Dict[str, str] = {}
    try:
        loc = ctx.locator(METRIC_ROW_SELECTOR)
        for i in range(16):
            try:
                t = _safe_inner_text(loc.nth(i), timeout_ms=180)
            except Exception:
                break
            if t and len(t) < 400:
                key = f"metric_{i+1}"
                metrics[key] = t
    except Exception:
        pass
    return metrics


def _ensure_map_and_summary_tab(interact_ctx: ExplorerCtx, page: Page) -> None:
    """Keep the explorer on Map + Summary; does not open Demographics / Schools / etc."""
    roots: List[ExplorerCtx] = [interact_ctx]
    if not isinstance(interact_ctx, Page):
        roots.append(page)
    seen_ids: set[int] = set()
    uniq: List[ExplorerCtx] = []
    for r in roots:
        rid = id(r)
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        uniq.append(r)
    for root in uniq:
        builders = (
            lambda r=root: r.get_by_role("tab", name=re.compile(r"map\s*(and|&)\s*summary", re.I)).first,
            lambda r=root: r.locator('[role="tab"]:has-text("Map and Summary")').first,
        )
        for mk in builders:
            try:
                loc = mk()
                if not loc.is_visible():
                    continue
                if loc.get_attribute("aria-selected") == "true":
                    return
                loc.click(timeout=ACTION_TIMEOUT_MS, force=True)
                page.wait_for_timeout(90)
                return
            except Exception:
                continue


def _slice_after_phrase(text: str, phrase: str, *, max_after: int = 320) -> str:
    """
    Text *after* the first case-insensitive occurrence of ``phrase``.

    Used when slicing after a label alone is not enough (e.g. ``_dollar_near_label``).
    """
    if not text or not phrase:
        return ""
    low = text.lower()
    p = phrase.lower()
    i = low.find(p)
    if i < 0:
        return ""
    start = i + len(p)
    end = min(len(text), start + max_after)
    return text[start:end]


def _first_dollar_in_chunk(chunk: str) -> Optional[int]:
    if not chunk or re.search(r"not\s+available", chunk, re.I):
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", chunk)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return int(round(float(raw)))
    except ValueError:
        return None


def _last_dollar_in_chunk(chunk: str) -> Optional[int]:
    if not chunk or re.search(r"not\s+available", chunk, re.I):
        return None
    last: Optional[int] = None
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d+)?)", chunk):
        raw = m.group(1).replace(",", "")
        try:
            last = int(round(float(raw)))
        except ValueError:
            continue
    return last


def _widget_summary_panel_text(raw: str) -> str:
    """
    Limit KPI parsing to the right-hand **Map + Summary** block.

    Full ``body`` ``innerText`` includes nav and marketing; ``household income`` can appear there and
    interact oddly with **Not available** in unrelated copy.
    """
    if not raw:
        return ""
    low = raw.lower()
    key = "summary for selected area"
    i = low.find(key)
    if i < 0:
        return raw
    chunk = raw[i:]
    low2 = chunk.lower()
    for stop in ("explorer id:", "powered by"):
        j = low2.find(stop)
        if j > 0:
            return chunk[:j].rstrip()
    return chunk


def _phantom_not_available_after_label(
    raw: str,
    label: str,
    *,
    before: int = 200,
    after: int = 80,
    prefer_last_label: bool = False,
    dollar_min: int,
    dollar_max: int,
) -> bool:
    """
    True when **Not available** appears just *after* ``label`` but the last ``$`` *before* the label
    is already a plausible KPI value. Widget ``innerText`` often sandwiches a stray NA line between
    stacked rows (e.g. income value is present; NA belongs visually to the next metric).
    """
    low = raw.lower()
    lab = label.lower()
    i = low.rfind(lab) if prefer_last_label else low.find(lab)
    if i < 0:
        return False
    before_s = raw[max(0, i - before) : i]
    after_s = raw[i + len(lab) : i + len(lab) + after]
    if not re.search(r"not\s+available", after_s, re.I):
        return False
    lb = _last_dollar_in_chunk(before_s)
    return lb is not None and dollar_min <= lb <= dollar_max


def _kpi_shows_not_available(
    raw: str,
    label: str,
    *,
    before: int = 200,
    after: int = 80,
    prefer_last_label: bool = False,
) -> bool:
    """True when **Not available** appears next to this heading in the widget ``innerText``."""
    low = raw.lower()
    lab = label.lower()
    i = low.rfind(lab) if prefer_last_label else low.find(lab)
    if i < 0:
        return False
    before_s = raw[max(0, i - before) : i]
    after_s = raw[i + len(lab) : i + len(lab) + after]
    na = re.compile(r"not\s+available", re.I)
    return bool(na.search(before_s) or na.search(after_s))


def _dollar_near_label(
    raw: str,
    label: str,
    *,
    before: int = 200,
    after: int = 120,
) -> Optional[int]:
    """Money associated with a KPI label. The widget often emits the dollar line *above* the label."""
    low = raw.lower()
    lab = label.lower()
    i = low.find(lab)
    if i < 0:
        return None
    before_s = raw[max(0, i - before) : i]
    after_s = raw[i + len(lab) : i + len(lab) + after]
    v = _last_dollar_in_chunk(before_s)
    if v is not None:
        return v
    return _first_dollar_in_chunk(after_s)


def _median_age_near_label(raw: str) -> Optional[float]:
    low = raw.lower()
    i = low.find("median age")
    if i < 0:
        return None
    before_s = raw[max(0, i - 120) : i]
    after_s = raw[i + len("median age") : i + len("median age") + 100]
    for chunk in (after_s, before_s):
        if re.search(r"not\s+available", chunk, re.I):
            continue
        m = re.search(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)(?![\d.])", chunk.strip())
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _first_money_in_chunk(chunk: str) -> Optional[int]:
    if not chunk:
        return None
    if re.search(r"not\s+available", chunk, re.I):
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", chunk)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            return int(round(float(raw)))
        except ValueError:
            return None
    # Some builds omit "$" on stat lines — require 4+ digit values to avoid years like 2024.
    m = re.search(r"(?<![\d.\-,])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\d.\-])", chunk)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None


def _first_pct_in_chunk(chunk: str) -> Optional[int]:
    if not chunk:
        return None
    if re.search(r"not\s+available", chunk, re.I):
        return None
    m = re.search(r"(\d+)\s*%", chunk)
    if not m:
        return None
    return int(m.group(1))


def _last_pct_in_chunk(chunk: str) -> Optional[int]:
    if not chunk:
        return None
    if re.search(r"not\s+available", chunk, re.I):
        return None
    last: Optional[int] = None
    for m in re.finditer(r"(\d+)\s*%", chunk):
        last = int(m.group(1))
    return last


def _pct_near_label(
    raw: str,
    label: str,
    *,
    before: int = 200,
    after: int = 120,
    last_pct_before: bool = False,
) -> Optional[int]:
    """Widget often places ``NN%`` immediately *before* ``Occupied by owners`` but *after* degree rows."""
    low = raw.lower()
    lab = label.lower()
    i = low.find(lab)
    if i < 0:
        return None
    before_s = raw[max(0, i - before) : i]
    after_s = raw[i + len(lab) : i + len(lab) + after]
    if last_pct_before:
        v = _last_pct_in_chunk(before_s)
        if v is not None:
            return v
        return _first_pct_in_chunk(after_s)
    v = _first_pct_in_chunk(after_s)
    if v is not None:
        return v
    return _last_pct_in_chunk(before_s)


def _validate_map_summary_from_text(raw: str) -> Tuple[bool, Optional[str], Dict[str, str]]:
    """
    Validate the right-hand summary KPI block.

    ``raw`` must include the embedded ``/widget/`` iframe document text (see
    ``_collect_raw_blob_for_summary``) — the host page ``body`` alone does not contain these strings.

    **Not available** on a required row (or a missing numeric where one is expected) is a failure.
    """
    snap: Dict[str, str] = {}
    blob = _widget_summary_panel_text(raw or "")
    low = blob.lower()
    missing = [p for p in SUMMARY_REQUIRED_PHRASES if p not in low]
    if missing:
        return False, f"summary_missing:{','.join(missing[:6])}", snap
    if not any(p in low for p in SUMMARY_RENT_OR_AGE_PHRASES):
        return False, "summary_missing:median rent or median age", snap

    if _kpi_shows_not_available(blob, "household income") and not _phantom_not_available_after_label(
        blob,
        "household income",
        dollar_min=SUMMARY_INCOME_MIN,
        dollar_max=SUMMARY_INCOME_MAX,
    ):
        return False, "summary_household_income_not_available", snap

    inc = _dollar_near_label(blob, "household income")
    if inc is None:
        inc = _first_money_in_chunk(_slice_after_phrase(blob, "household income"))
    if inc is None:
        return False, "summary_household_income_not_numeric", snap
    if not (SUMMARY_INCOME_MIN <= inc <= SUMMARY_INCOME_MAX):
        return False, f"summary_household_income_out_of_range:{inc}", {**snap, "household_income": str(inc)}
    snap["household_income"] = str(inc)

    if _kpi_shows_not_available(blob, "median home price") and not _phantom_not_available_after_label(
        blob,
        "median home price",
        dollar_min=SUMMARY_HOME_PRICE_MIN,
        dollar_max=SUMMARY_HOME_PRICE_MAX,
    ):
        return False, "summary_median_home_price_not_available", snap
    home = _dollar_near_label(blob, "median home price")
    if home is None:
        home = _first_money_in_chunk(_slice_after_phrase(blob, "median home price"))
    if home is None:
        return False, "summary_median_home_price_not_numeric", snap
    if not (SUMMARY_HOME_PRICE_MIN <= home <= SUMMARY_HOME_PRICE_MAX):
        return False, f"summary_median_home_price_out_of_range:{home}", {**snap, "median_home_price": str(home)}
    snap["median_home_price"] = str(home)

    if "median rent" in low:
        if _kpi_shows_not_available(blob, "median rent") and not _phantom_not_available_after_label(
            blob,
            "median rent",
            dollar_min=SUMMARY_RENT_MIN,
            dollar_max=SUMMARY_RENT_MAX,
        ):
            return False, "summary_median_rent_not_available", snap
        rent = _dollar_near_label(blob, "median rent")
        if rent is None:
            rent = _first_money_in_chunk(_slice_after_phrase(blob, "median rent"))
        if rent is None:
            return False, "summary_median_rent_not_numeric", snap
        if not (SUMMARY_RENT_MIN <= rent <= SUMMARY_RENT_MAX):
            return False, f"summary_median_rent_out_of_range:{rent}", {**snap, "median_rent": str(rent)}
        snap["median_rent"] = str(rent)
    else:
        if _kpi_shows_not_available(blob, "median age"):
            return False, "summary_median_age_not_available", snap
        age_val = _median_age_near_label(blob)
        if age_val is None:
            return False, "summary_median_age_not_numeric", snap
        if not (SUMMARY_MEDIAN_AGE_MIN <= age_val <= SUMMARY_MEDIAN_AGE_MAX):
            return False, f"summary_median_age_out_of_range:{age_val}", snap
        snap["median_age"] = str(age_val)

    if _kpi_shows_not_available(blob, "occupied by owners"):
        return False, "summary_occupied_by_owners_not_available", snap
    owners = _pct_near_label(blob, "occupied by owners", last_pct_before=True)
    if owners is None:
        return False, "summary_occupied_by_owners_pct_missing", snap
    if not (SUMMARY_PCT_MIN <= owners <= SUMMARY_PCT_MAX):
        return False, f"summary_occupied_by_owners_pct_out_of_range:{owners}", snap
    snap["occupied_by_owners_pct"] = str(owners)

    if _kpi_shows_not_available(blob, "has college degree"):
        return False, "summary_college_degree_not_available", snap
    college = _pct_near_label(blob, "has college degree")
    if college is None:
        return False, "summary_college_degree_pct_missing", snap
    if not (SUMMARY_PCT_MIN <= college <= SUMMARY_PCT_MAX):
        return False, f"summary_college_degree_pct_out_of_range:{college}", snap
    snap["college_degree_pct"] = str(college)

    if _kpi_shows_not_available(blob, "finished high school"):
        return False, "summary_high_school_not_available", snap
    hs = _pct_near_label(blob, "finished high school")
    if hs is None:
        return False, "summary_high_school_pct_missing", snap
    if not (SUMMARY_PCT_MIN <= hs <= SUMMARY_PCT_MAX):
        return False, f"summary_high_school_pct_out_of_range:{hs}", snap
    snap["finished_high_school_pct"] = str(hs)

    if _kpi_shows_not_available(blob, "employed", prefer_last_label=True):
        return False, "summary_employed_not_available", snap
    emp = _pct_near_label(blob, "employed", after=280)
    if emp is None:
        return False, "summary_employed_pct_missing", snap
    if not (SUMMARY_PCT_MIN <= emp <= SUMMARY_PCT_MAX):
        return False, f"summary_employed_pct_out_of_range:{emp}", snap
    snap["employed_pct"] = str(emp)

    return True, None, snap


def _collect_raw_blob_for_summary(
    page: Page,
    interact_ctx: ExplorerCtx,
    *,
    used_iframe: bool,
) -> str:
    """
    Text used for login-wall checks, alignment, and summary validation.

    Staging keeps the map + right-hand summary inside ``iframe[src*="/widget/"]``. The host
    ``document.body`` innerText does **not** include iframe contents, so we must merge the
    widget ``body`` text or validation always sees ``summary_missing``.
    """
    chunks: List[str] = []
    chunks.append(_safe_inner_text(page.locator("body").first, timeout_ms=900))
    if used_iframe and _interaction_is_iframe_surface(interact_ctx):
        chunks.append(_safe_inner_text(interact_ctx.locator("body").first, timeout_ms=1_200))
    return "\n".join(c for c in chunks if c)


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
    cap = max(0, int((overall_deadline - time.monotonic()) * 1000))
    if cap < 50:
        return
    try:
        loader.wait_for(state="visible", timeout=min(280, cap))
    except PlaywrightTimeoutError:
        return
    remaining_ms = max(80, int((overall_deadline - time.monotonic()) * 1000))
    try:
        # Do not spend the whole RESULT_TIMEOUT waiting on a selector that might false-match.
        loader.wait_for(state="hidden", timeout=min(1_200, remaining_ms))
    except PlaywrightTimeoutError:
        pass


def _panel_wait_tokens(address: str) -> List[str]:
    """Tokens ordered for ``#current-location-text`` — long street/city before ZIP (ZIP can match stale)."""
    raw = [p for p in _address_match_tokens(address) if p.lower() not in _GENERIC_STREET_TOKENS]
    out: List[str] = []
    longs = sorted(
        {p for p in raw if (len(p) >= 5 and not p.isdigit()) or (len(p) >= 6)},
        key=len,
        reverse=True,
    )
    for p in longs[:3]:
        out.append(p)
    for p in raw:
        if p.isdigit() and len(p) >= 5 and p not in out:
            out.append(p)
            break
    for p in raw:
        if len(p) >= 4 and p not in out:
            out.append(p)
        if len(out) >= 6:
            break
    return out[:6]


def _try_wait_panel_location_line(interact_ctx: ExplorerCtx, address: str, deadline: float) -> bool:
    """Return True once the map header shows a token from the target address (signals fetch done)."""
    for tok in _panel_wait_tokens(address):
        ms = int(max(0.0, deadline - time.monotonic()) * 1000)
        if ms < 120:
            return False
        try:
            pat = re.compile(re.escape(tok), re.I)
            # Do not spend the entire RESULT_TIMEOUT on one token (wrong/stale order).
            interact_ctx.locator("#current-location-text").filter(has_text=pat).first.wait_for(
                state="visible",
                timeout=min(ms, 900),
            )
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def _wait_for_explorer_outcome(
    read_ctx: ExplorerCtx,
    interact_ctx: ExplorerCtx,
    page: Page,
    address: str,
) -> None:
    """
    Wait only for evidence the **Map + Summary** iframe has rendered the KPI block we validate
    (headline + body text). Avoid per-tick metric/heading scans — those dominated wall time.
    """
    deadline = time.monotonic() + RESULT_TIMEOUT_MS / 1000.0
    page.wait_for_timeout(MIN_POST_SUBMIT_WAIT_MS)
    _wait_for_loading_done(read_ctx, page, deadline)
    if interact_ctx is not read_ctx:
        _wait_for_loading_done(interact_ctx, page, deadline)

    err = _extract_error(read_ctx) or _extract_error(interact_ctx)
    if err:
        page.wait_for_timeout(STABILIZE_AFTER_OUTCOME_MS)
        return

    want = _strong_address_tokens(address)

    if _interaction_is_iframe_surface(interact_ctx):
        ms = int(max(0.0, deadline - time.monotonic()) * 1000)
        if ms >= 80:
            try:
                interact_ctx.get_by_text(
                    re.compile(r"summary\s+for\s+selected\s+area", re.I)
                ).first.wait_for(state="attached", timeout=ms)
            except PlaywrightTimeoutError:
                pass

    if want and _try_wait_panel_location_line(interact_ctx, address, deadline):
        page.wait_for_timeout(20)

    while time.monotonic() < deadline:
        err = _extract_error(read_ctx) or _extract_error(interact_ctx)
        if err:
            break
        if _interaction_is_iframe_surface(interact_ctx):
            try:
                blob = _safe_inner_text(interact_ctx.locator("body").first, timeout_ms=380)
                if blob and "summary for selected area" in blob.lower():
                    break
            except Exception:
                pass
        else:
            name = _extract_widget_heading_line(interact_ctx) or _extract_neighborhood_name(interact_ctx)
            if name and len(name.strip()) > 2 and not _is_default_nashville_ui(name):
                if not want or _result_aligns_with_target(
                    name=name,
                    metrics={},
                    raw_blob="",
                    input_value="",
                    address=address,
                ):
                    break
        _wait_for_loading_done(read_ctx, page, deadline)
        if interact_ctx is not read_ctx:
            _wait_for_loading_done(interact_ctx, page, deadline)
        page.wait_for_timeout(RESULT_POLL_MS)

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


def _reveal_map_search_input(ctx: ExplorerCtx, page: Page) -> None:
    """
    Desktop layout keeps ``#location-input`` inside ``.hidden`` until the user clicks the visible
    current-location label (seed address). Typing without this step updates a field the map does not use.
    """
    for sel in MAP_SEARCH_REVEAL_SELECTORS:
        try:
            loc = ctx.locator(sel).first
            loc.wait_for(state="attached", timeout=5_000)
            loc.click(timeout=5_000, force=True)
            page.wait_for_timeout(60)
            return
        except Exception:
            continue


def _clear_address_field(ctx: ExplorerCtx, page: Page) -> None:
    _reveal_map_search_input(ctx, page)
    try:
        inp = _resolve_address_input(ctx)
        _focus_typeable_input(inp)
        try:
            inp.press("Control+a")
            inp.press("Backspace")
        except Exception:
            _clear_input_value(inp)
        if CLEAR_BUTTON_SELECTOR:
            btn = ctx.locator(CLEAR_BUTTON_SELECTOR).first
            try:
                if btn.count() > 0:
                    btn.click(timeout=ACTION_TIMEOUT_MS, force=True)
            except Exception:
                pass
    except Exception:
        pass


def _click_view_neighborhood_cta(interact_ctx: ExplorerCtx, page: Page) -> None:
    """Map footer CTA — not always a plain ``button`` matching SUBMIT_BUTTON_SELECTOR."""
    roots: List[ExplorerCtx] = [interact_ctx]
    if not isinstance(interact_ctx, Page):
        roots.append(page)
    last_exc: Optional[Exception] = None
    for root in roots:
        builders = (
            lambda r=root: r.get_by_role("button", name=re.compile(r"view\s+neighborhood\s+data", re.I)).first,
            lambda r=root: r.locator('button:has-text("View Neighborhood Data")').first,
            lambda r=root: r.locator('[role="button"]:has-text("View Neighborhood Data")').first,
            lambda r=root: r.get_by_text(re.compile(r"View\s+Neighborhood\s+Data", re.I)).first,
            lambda r=root: r.locator("text=/View\\s+Neighborhood\\s+Data/i").first,
            lambda r=root: r.locator(SUBMIT_BUTTON_SELECTOR).first,
        )
        for mk in builders:
            try:
                loc = mk()
                loc.wait_for(state="attached", timeout=5_000)
                loc.click(timeout=SUBMIT_CLICK_TIMEOUT_MS, force=True)
                return
            except Exception as exc:
                last_exc = exc
                continue
    raise PlaywrightTimeoutError(f"Could not click explorer CTA: {last_exc!r}") from last_exc


def _submit_and_collect(
    page: Page,
    interact_ctx: ExplorerCtx,
    read_ctx: ExplorerCtx,
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
    _ensure_map_and_summary_tab(interact_ctx, page)
    _clear_address_field(interact_ctx, page)
    inp = _resolve_address_input(interact_ctx)
    try:
        if bool(inp.evaluate("el => !!el.closest('.hidden')")):
            _reveal_map_search_input(interact_ctx, page)
    except Exception:
        _reveal_map_search_input(interact_ctx, page)
    _focus_typeable_input(inp)
    # Route keystrokes through the input locator so they land in the /widget/ iframe (``page.keyboard`` can miss).
    try:
        inp.press("Control+a")
        inp.press("Backspace")
    except Exception:
        try:
            _clear_input_value(inp)
        except Exception:
            pass
    page.wait_for_timeout(35)
    try:
        inp.click(timeout=5_000, force=True)
    except Exception:
        pass
    try:
        inp.fill(address, force=True)
    except Exception:
        try:
            _react_fill_input(inp, address)
        except Exception:
            pass
    _wait_for_places_suggestions_ready(page, interact_ctx)
    visible = _places_suggestions_visible(page, interact_ctx)
    try:
        got = (inp.input_value(timeout=800) or "").strip()
    except Exception:
        got = ""
    need_keystrokes = (not visible) and (
        len(got) < min(12, max(6, len(address) // 2))
    )
    if need_keystrokes:
        try:
            inp.press("Control+a")
            inp.press("Backspace")
        except Exception:
            pass
        try:
            inp.press_sequentially(address, delay=ADDRESS_TYPE_DELAY_MS)
        except Exception:
            try:
                _react_fill_input(inp, address)
            except Exception:
                pass
        _wait_for_places_suggestions_ready(page, interact_ctx)
    pick = _select_best_places_suggestion(page, interact_ctx, address, inp)
    if pick == 0:
        try:
            inp.press("Enter")
        except Exception:
            pass
    page.wait_for_timeout(POST_SUBMIT_STABILITY_MS)

    try:
        _click_view_neighborhood_cta(interact_ctx, page)
    except Exception as exc:
        error_msg = (error_msg + "; ") if error_msg else ""
        error_msg = f"{error_msg}submit_click_failed: {exc}"
        try:
            inp.press("Enter", timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass

    page.wait_for_timeout(POST_SUBMIT_STABILITY_MS)

    _wait_for_explorer_outcome(read_ctx, interact_ctx, page, address)
    _ensure_map_and_summary_tab(interact_ctx, page)

    name = (
        _extract_widget_heading_line(interact_ctx)
        or _extract_neighborhood_name(interact_ctx)
        or _extract_neighborhood_name(read_ctx)
    )
    err = _extract_error(read_ctx) or _extract_error(interact_ctx)
    m_read = _extract_metrics(read_ctx)
    m_embed = _extract_metrics(interact_ctx)
    raw_blob = _collect_raw_blob_for_summary(page, interact_ctx, used_iframe=used_iframe)
    summary_ok, summary_err, summary_snap = _validate_map_summary_from_text(raw_blob)
    metrics = {**m_read, **m_embed, **{f"summary_{k}": v for k, v in summary_snap.items()}}

    input_value = ""
    try:
        input_value = (inp.input_value(timeout=800) or "").strip()
    except Exception:
        try:
            input_value = str(inp.evaluate("e => e.value || ''") or "").strip()
        except Exception:
            input_value = ""

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
        if success:
            if not summary_ok:
                success = False
                error_msg = error_msg or summary_err or "summary_panel_invalid"
            # Require visible panel copy to match — ``input_value`` alone can update before the summary rerenders.
            panel_aligns = _result_aligns_with_target(
                name=name,
                metrics=metrics,
                raw_blob=raw_blob,
                input_value="",
                address=address,
            )
            any_aligns = _result_aligns_with_target(
                name=name,
                metrics=metrics,
                raw_blob=raw_blob,
                input_value=input_value,
                address=address,
            )
            if success and name and not panel_aligns:
                success = False
                error_msg = error_msg or "neighborhood_result_does_not_match_target_address"
            elif success and not name and not any_aligns:
                success = False
                error_msg = error_msg or "neighborhood_result_does_not_match_target_address"

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
    *,
    navigate: bool = True,
) -> TestRow:
    started = time.perf_counter()
    last_exc: Optional[str] = None
    last: Optional[TestRow] = None
    last_used_iframe = False
    for attempt in range(1, RETRIES_PER_ADDRESS + 1):
        try:
            if navigate or attempt > 1:
                page.goto(STAGING_URL, wait_until="commit", timeout=EXPLORER_GOTO_TIMEOUT_MS)
            interact_ctx, read_ctx, used_iframe = _explorer_surfaces(page)
            last_used_iframe = used_iframe
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
                interact_ctx,
                read_ctx,
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
        used_iframe=last_used_iframe,
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


def summarize_and_print(meta: Dict[str, Any], *, quiet: bool = False) -> None:
    if quiet:
        print(
            f"\nSummary: {meta['success_count']}/{meta['total_tests']} passed "
            f"({meta['success_rate']}%), failures {meta['failure_count']}, "
            f"avg {meta['avg_duration_ms']} ms, {meta['states_covered']} states\n"
        )
        return
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


def _report_address_result(
    case_id: int,
    state: str,
    address: str,
    row: TestRow,
    *,
    quiet: bool = False,
) -> str:
    if quiet:
        mark = "ok" if row.success else "FAIL"
        tail = ""
        if not row.success and row.error_message:
            em = row.error_message.replace("\n", " ")
            if len(em) > 120:
                em = em[:117] + "…"
            tail = f"  |  {em}"
        if row.screenshot_path:
            tail += f"  |  shot: {row.screenshot_path}"
        return (
            f"  #{case_id} {state} {mark}  {row.duration_ms} ms  "
            f"|  {address}{tail}"
        )
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
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Concise terminal output (one line per address, short summary). Full detail stays in results JSON.",
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
        if args.quiet:
            print("[!] No DREAM_NEIGHBORHOOD_* credentials — may hit login wall.\n")
        else:
            print(
                "\n[!] DREAM_NEIGHBORHOOD_EMAIL / DREAM_NEIGHBORHOOD_PASSWORD not both set — "
                "staging may return a sign-in page (reported as outcome_code=login_wall). "
                "See docs/RUNNING.md.\n"
            )
        if os.environ.get("GITHUB_ACTIONS") == "true" and not args.quiet:
            print(
                "[!] You are in GitHub Actions WITHOUT login secrets.\n"
                "    Expect mostly login_wall results. Add DREAM_NEIGHBORHOOD_EMAIL and "
                "DREAM_NEIGHBORHOOD_PASSWORD repo secrets, or read docs/START-HERE.md.\n"
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
            tqdm(runlist, desc="Neighborhood tests", unit="addr", disable=args.quiet),
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
            row = run_one(
                page,
                i,
                state,
                address,
                src,
                artifacts_dir,
                had_login_config,
            # ``goto`` every address except the first when login already landed on this URL (saves one navigation).
            navigate=not (i == 1 and had_login_config),
            )
            rows.append(row)
            tqdm.write(_report_address_result(i, state, address, row, quiet=args.quiet))
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
    summarize_and_print(meta, quiet=args.quiet)

    dash_json = dashboard_dir / "results.json"
    shutil.copyfile(out_path, dash_json)

    if args.quiet:
        print(f"Wrote {out_path}  ·  dashboard: {dashboard_dir / 'index.html'}")
        if args.live_port is not None and args.live_port > 0:
            print(f"Live snapshot: {live_path}")
    else:
        print("\nTesting complete.\n")
        print(f"   Wrote machine JSON: {out_path}")
        print(f"   Dashboard data:     {dash_json}")
        if args.live_port is not None and args.live_port > 0:
            print(f"   Live snapshot:      {live_path} (final)")
            print("   The local server has exited; reopen live mode on the next run with --live-port.\n")
        print()
        print("Dashboard ready at: dashboard/index.html")
        print()
        print("To deploy to Netlify:")
        print("  1. Drag the entire 'dashboard' folder to https://app.netlify.com/drop")
        print("     or")
        print("  2. Push the repo to GitHub and connect the 'dashboard' directory in Netlify")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
