# Automated UI test — Dream Neighborhood Explorer

Python + [Playwright](https://playwright.dev) runner that drives the **Dream Neighborhood Explorer** on staging, plus a **static dashboard** (Tailwind + Chart.js) for `results.json`.

## Quick links

- **How to run locally or in GitHub Actions:** [docs/RUNNING.md](docs/RUNNING.md)
- **Trigger CI tests:** [Actions → Run Neighborhood Explorer tests](https://github.com/motormouthvis/automated-UI-test/actions/workflows/run-explorer-tests.yml)

## What this is not

- The Netlify site **displays** results; it does **not** execute Playwright in the browser. See [docs/RUNNING.md](docs/RUNNING.md) for why.

## Repo layout

| Path | Purpose |
| ---- | ------- |
| `text/test_neighborhood_explorer.py` | Playwright test runner |
| `dashboard/` | Static UI + `results.json` for Netlify |
| `requirements.txt` | Python dependencies |
| `.github/workflows/run-explorer-tests.yml` | Optional GitHub Actions job |
