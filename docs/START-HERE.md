# Start here (simple version)

Imagine three things:

## 1. The robot (GitHub Actions or your computer)

The **robot** opens a real web browser, goes to Dream Neighborhood **staging**, types an address, and waits to see what the page shows.

That robot **does not live on Netlify**. Netlify only shows pictures of paper; it can’t drive a browser.

## 2. The coloring book (the staging website)

The **staging site** is where the test happens.  
If the robot doesn’t have your **email + password**, the site often shows a **sign-in page** and never gets to the neighborhood widget.

So the failure is **“no key to the house”** — not “this address is special.”

## 3. The sticker chart (your Netlify site)

The **Netlify website** is only a **dashboard**: it reads a file named `results.json` and draws charts and tables.

It does **nothing** until something else (the robot) finishes and **creates** that file.

---

## What went wrong in your GitHub run

You saw:

`DREAM_NEIGHBORHOOD_EMAIL / DREAM_NEIGHBORHOOD_PASSWORD not both set`

That means: **GitHub never got your staging login.**

Then each test still runs, but the robot **waits a long time** on every address (about **2 minutes each** is normal in that situation).  
So **25 tests ≈ about 50 minutes** of mostly waiting + failing.

**Math:**  
`number of tests × ~2 minutes` when login secrets are missing (rough guess).

**With working login:** often **much faster** per test (often tens of seconds, depending on the site).

---

## What you should do (2 steps)

### Step A — Give GitHub your staging login (one time)

1. Open your repo on GitHub.  
2. **Settings** → **Secrets and variables** → **Actions**.  
3. **New repository secret** (two of them):

   - Name: `DREAM_NEIGHBORHOOD_EMAIL` → Value: your staging email  
   - Name: `DREAM_NEIGHBORHOOD_PASSWORD` → Value: your staging password  

4. Run the workflow again from the **Actions** tab.

If you skip this, runs will stay **slow and mostly useless**.

### Step B — Get results on the pretty site

After the workflow finishes:

1. Open the job → download the **`explorer-results`** zip.  
2. Take `results.json` or `dashboard/results.json` from it.  
3. Put that file into your project’s **`dashboard/`** folder (replace the old one).  
4. Commit + push → Netlify rebuilds → the **sticker chart** updates.

---

## What the website is “good for”

**One job:** show the last run in a nice table and graphs.

It is **not** for clicking “run tests” inside the browser (Netlify can’t do that).  
**Running** tests = GitHub Actions button or your PC. **Viewing** = Netlify.

---

## More detail (when you want it)

See [RUNNING.md](RUNNING.md).
