# Running Dream Neighborhood Explorer tests

Too much detail? Read **[START-HERE.md](START-HERE.md)** first (kid-friendly explanation).

Netlify hosts **static files** (HTML, JSON, JS). Playwright needs a real **Python + Chromium** process on a machine you control. Visiting your Netlify URL in a browser does **not** give that environment, so the test suite cannot start from the Netlify page by itself.

You can still use the **same dashboard UI** on Netlify to **view** results once `results.json` has been produced elsewhere.

---

## Option A — Run on your computer (recommended to debug)

From the **repository root** (`automated-UI-test/`).

### 1. One-time setup (Windows PowerShell)

```powershell
cd path\to\automated-UI-test
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

### 2. Staging login (usually required)

Staging often shows **“Sign in to your account”** if you are anonymous. That is **not** because “Florida needs a login and California doesn’t.” It means the **browser session** was unauthenticated or **your selectors** are reading a page that is actually the login screen.

Set credentials **before** running (same shell):

```powershell
$env:DREAM_NEIGHBORHOOD_EMAIL = "you@example.com"
$env:DREAM_NEIGHBORHOOD_PASSWORD = "your-password"
```

If you skip this, you should expect many `login_wall` outcomes in `results.json`.

### 3. Running inside Cursor (or VS Code)

You can treat this like any other Python project—output shows in the **integrated terminal**, and JSON opens in the editor.

1. **Python interpreter:** choose your venv in Cursor (**Python: Select Interpreter** → `.venv`).
2. **One-click run:** **Terminal → Run Task…** → **Neighborhood Explorer: prompt for count (show browser)** — it will ask how many addresses (1–1000) and print a full report after each one. Or use **smoke (2)** / **custom count** tasks. From any terminal: `python text/test_neighborhood_explorer.py --headed` (omit `--count` to be prompted).
3. **Breakpoints:** open **Run and Debug** (`Ctrl+Shift+D`) → choose **Explorer: smoke (2, headed)** (or single-address) → Start.
4. **See numbers here:** when the run finishes, open **`dashboard/results.json`** or **`results.json`** with **Quick Open** (`Ctrl+P`). The pretty charts need a browser: either open **`dashboard/index.html`** with the **Live Server** extension, or in a terminal: `cd dashboard` then `python -m http.server 8766` and visit [http://localhost:8766](http://localhost:8766).
5. **Live updating charts while tests run:** use task **Neighborhood Explorer: live dashboard + smoke**, then in a browser open [http://127.0.0.1:8765/?live=1](http://127.0.0.1:8765/?live=1).

The AI chat can read `results.json` too: run the task, then ask it to summarize the file.

### 4. Tune selectors (before a 1000-address run)

```powershell
playwright codegen https://staging.dreamneighborhood.com/a/drea-neighborhood-treasure-coast/core/explore-neighborhoods/
```

Copy working locators into the `CONFIG` block at the top of `text/test_neighborhood_explorer.py` (iframe, address field, submit, etc.).

### 5. Run a small smoke batch

```powershell
python text/test_neighborhood_explorer.py --headed
```

- Omit **`--count`** to be prompted in the terminal, or set **`--count 5`** explicitly.

Output:

- `results.json` at repo root (unless `--output` says otherwise)
- `dashboard/results.json` updated automatically for the static dashboard

### 6. Live dashboard while the run is in progress (optional)

```powershell
python text/test_neighborhood_explorer.py --count 50 --live-port 8765
```

Open **http://127.0.0.1:8765/?live=1** in your browser. This only works while the Python process is running.

---

## Option B — Run from GitHub (closest to “start from the web”)

1. In GitHub: **Actions** → **Run Neighborhood Explorer tests** → **Run workflow**.
2. Set **count** (start with `10`–`25`).
3. In the repo **Settings → Secrets and variables → Actions**, add:
   - `DREAM_NEIGHBORHOOD_EMAIL`
   - `DREAM_NEIGHBORHOOD_PASSWORD`
4. After the job finishes, open the **explorer-results** artifact and download **`results.json`** (or **`dashboard/results.json`**).
5. Put that file into your **`dashboard/`** folder in git (replace existing `results.json`), commit, and push so Netlify rebuilds.

You are not running Playwright *in* Netlify here either — GitHub’s Linux runner executes the script, then you **publish** the JSON to Netlify like any other static asset.

---

## Reading results: outcome codes

Each row includes:

| Field | Meaning |
| ----- | ------- |
| `outcome_code` | Short machine label (`success`, `login_wall`, `unclear_result`, …). |
| `diagnosis` | Plain-language explanation. |
| `evidence` | Structured hints: final `page_url`, whether an iframe was used, metric counts, etc. |
| `error_message` | Legacy/technical snippet (widget text or internal tag). |

**`login_wall`** again: the page **content** looked like a sign-in screen. The address line in the test is **what you typed into the widget** after loading the page — it does not “cause” authentication.

---

## Netlify dashboard

Configure Netlify **publish directory** to `dashboard` (see repo `netlify.toml`). After each run, ensure **`dashboard/results.json`** matches the latest `results.json` you want to show.
