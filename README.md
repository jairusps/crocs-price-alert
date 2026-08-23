# crocs-price-alert

Tracks **"Crocs"** listings on **Amazon.in** and **Flipkart**, and sends a
Gmail alert whenever:

- a listing's price **drops** compared to the last recorded price, or
- a listing is available **under ₹2,000**.

Runs on a schedule via GitHub Actions — no server required.

## How it works

- `price_tracker.py` fetches search results through **ScraperAPI**
  (`http://api.scraperapi.com`, `render=true`) using plain `requests` calls —
  **no Playwright / headless browser**, since headless browsers running from
  GitHub Actions' cloud IPs get blocked or time out on Amazon/Flipkart.
- Results are parsed with **BeautifulSoup** using resilient, multi-fallback
  CSS selectors for both sites (their markup/class names change often).
- Price history is stored in [`data/prices.json`](data/prices.json), which
  the workflow commits back to the repo after every run.
- The script always exits `0`, even if zero items are found or an error
  occurs, so a bad scrape doesn't fail the scheduled workflow. Check the
  Action logs to see what actually happened on a given run.

## Files

| File | Purpose |
|---|---|
| `price_tracker.py` | Main script: scrape → parse → compare → email → save |
| `.github/workflows/crocs-price-alert.yml` | Scheduled workflow (every 6h + manual trigger) |
| `requirements.txt` | `requests`, `beautifulsoup4` |
| `data/prices.json` | Auto-generated/updated price history (created on first run) |

## Setup

### 1. Get a ScraperAPI key

Sign up at [scraperapi.com](https://www.scraperapi.com/) and copy your API
key from the dashboard. The free tier is enough to test this out.

### 2. Create a Gmail App Password

Gmail requires an **App Password** (not your normal password) for SMTP:

1. Enable 2-Step Verification on the Gmail account you want to send *from*:
   <https://myaccount.google.com/security>
2. Go to <https://myaccount.google.com/apppasswords>
3. Create an app password (choose "Mail" / "Other") and copy the 16-character
   code.

### 3. Add GitHub Secrets

In your repository: **Settings → Secrets and variables → Actions → New
repository secret**. Add each of these:

| Secret name | Value |
|---|---|
| `SCRAPERAPI_KEY` | Your ScraperAPI key |
| `GMAIL_USERNAME` | The Gmail address alerts are sent **from** (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | The 16-character Gmail App Password from step 2 |
| `ALERT_TO` | The email address alerts should be sent **to** (can be the same as `GMAIL_USERNAME`) |

### 4. Enable the workflow

The workflow is scheduled via cron (`0 */6 * * *` — every 6 hours) and can
also be run manually:

**Actions tab → "Crocs Price Alert" → Run workflow**

The first run will create `data/prices.json` and commit it back to the repo
automatically (the workflow has `contents: write` permission for this).

## Configuration

Open `price_tracker.py` to tweak:

- `SEARCH_TERM` — defaults to `"Crocs"`
- `PRICE_THRESHOLD` — defaults to `2000` (₹)
- `AMAZON_SEARCH_URL` / `FLIPKART_SEARCH_URL` — change the search query or
  point at specific category pages
- Cron schedule — edit the `cron` value in
  `.github/workflows/crocs-price-alert.yml`

## Local testing

```bash
pip install -r requirements.txt

export SCRAPERAPI_KEY="your_key"
export GMAIL_USERNAME="you@gmail.com"
export GMAIL_APP_PASSWORD="your_app_password"
export ALERT_TO="you@gmail.com"

python price_tracker.py
```

Check `data/prices.json` afterward to confirm items were captured.

## Notes / limitations

- Amazon and Flipkart change their page markup periodically. If parsing
  starts returning 0 items, check the Action logs first — the script logs
  fetch failures and item counts per site — then update the CSS selectors
  in `parse_amazon()` / `parse_flipkart()` in `price_tracker.py`.
- ScraperAPI's free tier has a limited number of monthly requests; each run
  uses 2 requests (one per site). Adjust the cron frequency to stay within
  your plan's limits.
- This project only reads publicly available search-result pages; it does
  not log in to either site or handle checkout/purchase in any way.
