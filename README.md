# Crocs India Price Alert

Automatically checks Amazon India and Flipkart for **Crocs** products and emails an alert when a product is available at or below **₹2,000**.

The checker is designed to run with GitHub Actions, so your computer does not need to stay on.

## What it does

- Searches Amazon India for `Crocs`
- Searches Flipkart for `Crocs`
- Extracts product names, URLs and visible prices
- Keeps a small price-history file in `data/prices.json`
- Sends an email only when:
  - a newly discovered product is at/below ₹2,000, or
  - a previously seen product drops to/below ₹2,000, or
  - an already-alerted product reaches a new lower price
- Can be run manually from the GitHub Actions tab
- Uses Gmail SMTP; your Gmail password is never stored in the repository

## Important limitations

Retail sites can change their HTML, use bot protection, location-dependent pricing, size-dependent pricing, or show prices that require selecting a variant. This monitor therefore treats the visible listing price as the trigger price and may occasionally miss a product.

Amazon/Flipkart may also block automated requests temporarily. The workflow will still finish without exposing your credentials.

## Setup

### 1. Create a GitHub repository

Create a repository, preferably public if you want GitHub-hosted Actions to be free.

Upload all files from this project.

### 2. Create a Gmail App Password

Your Google account needs 2-Step Verification before an App Password can be created.

In Google Account security, create a new App Password for this monitor.

**Do not put the App Password in this repository.**

### 3. Add GitHub Secrets

Open:

`Repository → Settings → Secrets and variables → Actions → New repository secret`

Create:

| Secret | Value |
|---|---|
| `GMAIL_USERNAME` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-character Gmail App Password |
| `ALERT_TO` | the email address receiving alerts |

For your setup, `ALERT_TO` can be:

`jairuspaulsamuel@gmail.com`

### 4. Enable Actions

Open the repository's **Actions** tab.

The workflow is scheduled to run every hour.

You can also choose:

`Actions → Crocs Price Alert → Run workflow`

to test it immediately.

### 5. Repository permissions

The workflow commits `data/prices.json` back to the repository so it remembers prices between runs.

Go to:

`Settings → Actions → General → Workflow permissions`

and select:

**Read and write permissions**

The workflow also explicitly requests `contents: write`.

## Changing the threshold

The default is ₹2,000.

In `.github/workflows/crocs-price-alert.yml`:

```yaml
env:
  PRICE_LIMIT: "2000"
```

Change it to another amount if you want.

## Changing the frequency

The workflow currently runs hourly.

GitHub Actions supports scheduled workflows as frequently as every 5 minutes, but hourly is a better starting point for retailer scraping.

## Testing locally

Install Python 3.11+ and:

```bash
pip install -r requirements.txt
playwright install chromium
python price_tracker.py
```

For local testing, set the same email environment variables used by the workflow.

## Security

Never commit:

- Gmail passwords
- Gmail App Passwords
- API keys
- GitHub tokens

Use GitHub Secrets instead.

## Current behavior

The monitor searches for general `Crocs`, rather than a fixed model. This means it can find clogs, sandals, slides, flip-flops, accessories, etc., whenever the retailer's search results expose them.

It does **not** guarantee that every Crocs listing on either retailer is discovered.
