# Automated UI test — Dream Neighborhood Explorer

**New here? Read [docs/START-HERE.md](docs/START-HERE.md) first** (very short, plain English).

Python + [Playwright](https://playwright.dev) drives staging in a **real browser**. The Netlify site only **displays** saved results (`results.json`); it does not run tests.

## Quick links

- **Simple explanation + “why is GitHub slow?”:** [docs/START-HERE.md](docs/START-HERE.md)
- **Full how-to (local + Actions):** [docs/RUNNING.md](docs/RUNNING.md)
- **Run in GitHub:** [Actions → Run workflow](https://github.com/motormouthvis/automated-UI-test/actions/workflows/run-explorer-tests.yml)

## What this is not

- The Netlify site **displays** results; it does **not** execute Playwright in the browser. See [docs/RUNNING.md](docs/RUNNING.md) for why.

## Repo layout

| Path | Purpose |
| ---- | ------- |
| `text/test_neighborhood_explorer.py` | Playwright test runner |
| `dashboard/` | Static UI + `results.json` for Netlify |
| `requirements.txt` | Python dependencies |
| `.github/workflows/run-explorer-tests.yml` | Optional GitHub Actions job |
