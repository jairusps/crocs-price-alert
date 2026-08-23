#!/usr/bin/env python3
"""
price_tracker.py
-----------------
Tracks "Crocs" listings on Amazon.in and Flipkart via ScraperAPI (plain HTTP,
no headless browser), stores price history in data/prices.json, and sends a
Gmail alert whenever:
  - an item's price drops below the previous recorded price, OR
  - an item is available under the PRICE_THRESHOLD (default ₹2000).

Design notes (per known constraints):
  - No Playwright / headless browsers. Cloud/CI IPs get blocked too often.
  - All requests are routed through ScraperAPI's HTTP endpoint with
    render=true so JS-rendered DOM is returned as plain HTML for BeautifulSoup.
  - Selectors are resilient: multiple fallback CSS selectors are tried for
    both Amazon and Flipkart, since both sites change markup frequently.
  - The script NEVER raises a nonzero exit code just because zero items were
    scraped/found. It exits 0 in all "expected" failure modes so GitHub
    Actions doesn't go red for a transient scrape miss. Only truly unexpected
    crashes are logged (still exit 0) to keep the workflow green; check the
    Action logs / email for actual status.
"""

import os
import sys
import json
import time
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SEARCH_TERM = "Crocs"
PRICE_THRESHOLD = 2000  # INR - "item under ₹2000" alert trigger

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()
GMAIL_USERNAME = os.environ.get("GMAIL_USERNAME", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
ALERT_TO = os.environ.get("ALERT_TO", "").strip()

AMAZON_SEARCH_URL = f"https://www.amazon.in/s?k={quote_plus(SEARCH_TERM)}"
FLIPKART_SEARCH_URL = f"https://www.flipkart.com/search?q={quote_plus(SEARCH_TERM)}"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "prices.json")

SCRAPERAPI_ENDPOINT = "http://api.scraperapi.com"
REQUEST_TIMEOUT = 90  # seconds - render=true can be slow
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("crocs-price-alert")


# --------------------------------------------------------------------------
# Fetching (ScraperAPI, plain requests - no headless browser)
# --------------------------------------------------------------------------

def fetch_via_scraperapi(target_url: str, render: bool = True) -> str | None:
    """
    Fetch target_url's rendered HTML through ScraperAPI using a plain
    requests.get() call. Retries with backoff. Returns None (never raises)
    on failure so callers can treat a miss as "0 items found this run".
    """
    if not SCRAPERAPI_KEY:
        log.error("SCRAPERAPI_KEY is not set. Skipping fetch for %s", target_url)
        return None

    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": target_url,
        "render": "true" if render else "false",
        "country_code": "in",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                SCRAPERAPI_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200 and resp.text:
                return resp.text
            log.warning(
                "ScraperAPI non-200 (attempt %d/%d) status=%s for %s",
                attempt, MAX_RETRIES, resp.status_code, target_url,
            )
        except requests.RequestException as exc:
            log.warning(
                "ScraperAPI request error (attempt %d/%d) for %s: %s",
                attempt, MAX_RETRIES, target_url, exc,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    log.error("Giving up fetching %s after %d attempts", target_url, MAX_RETRIES)
    return None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _first_text(node, selectors):
    """Try a list of CSS selectors against `node`; return first non-empty text."""
    for sel in selectors:
        found = node.select_one(sel)
        if found:
            text = found.get_text(strip=True)
            if text:
                return text
    return None


def _first_attr(node, selectors, attr):
    for sel in selectors:
        found = node.select_one(sel)
        if found and found.has_attr(attr):
            return found[attr]
    return None


def _parse_price_to_int(raw_price: str | None) -> int | None:
    """Turn '₹1,499' / 'Rs. 1499.00' / '1,499' into int(1499). None on failure."""
    if not raw_price:
        return None
    digits = "".join(ch for ch in raw_price if ch.isdigit())
    if not digits:
        return None
    try:
        value = int(digits)
        if value <= 0:
            return None
        return value
    except ValueError:
        return None


def parse_amazon(html: str) -> list[dict]:
    """
    Parse Amazon.in search results HTML into a list of:
      {"source": "amazon", "title": str, "price": int, "url": str, "id": str}
    Uses multiple fallback selectors since Amazon frequently A/B tests markup.
    """
    items = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")

    result_nodes = soup.select("div[data-component-type='s-search-result']")
    if not result_nodes:
        result_nodes = soup.select("div.s-result-item[data-asin]")

    for node in result_nodes:
        asin = node.get("data-asin", "").strip()
        if not asin:
            continue

        title = _first_text(
            node,
            [
                "h2 a span",
                "h2 span.a-text-normal",
                "h2 a.a-link-normal span",
                "h2",
            ],
        )
        if not title:
            continue

        if "croc" not in title.lower():
            continue

        raw_price = _first_text(
            node,
            [
                "span.a-price > span.a-offscreen",
                "span.a-price-whole",
                "span.a-color-price",
            ],
        )
        price = _parse_price_to_int(raw_price)
        if price is None:
            continue

        href = _first_attr(
            node,
            ["h2 a.a-link-normal", "h2 a"],
            "href",
        )
        product_url = (
            f"https://www.amazon.in{href}" if href and href.startswith("/") else href
        ) or f"https://www.amazon.in/dp/{asin}"

        items.append(
            {
                "source": "amazon",
                "id": f"amazon:{asin}",
                "title": title,
                "price": price,
                "url": product_url,
            }
        )

    return items


def parse_flipkart(html: str) -> list[dict]:
    """
    Parse Flipkart search results HTML into a list of:
      {"source": "flipkart", "title": str, "price": int, "url": str, "id": str}
    Flipkart changes its generated class names often, so several fallback
    container/selector combinations are attempted.
    """
    items = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")

    container_selectors = [
        "div._1AtVbE",
        "div._4ddWXP",
        "div.tUxRFH",
        "a.CGtC98",
    ]

    result_nodes = []
    for sel in container_selectors:
        found = soup.select(sel)
        if found:
            result_nodes = found
            break

    for node in result_nodes:
        title = _first_text(
            node,
            [
                "div._4rR01T",
                "a.s1Q9rs",
                "div.KzDlHZ",
                "a.wjcEIp",
                "div.syl9yP",
                "a[title]",
            ],
        )
        if not title:
            title_attr = _first_attr(node, ["a[title]"], "title")
            title = title_attr

        if not title or "croc" not in title.lower():
            continue

        raw_price = _first_text(
            node,
            [
                "div._30jeq3",
                "div.Nx9bqj",
                "div._1_WHN1",
            ],
        )
        price = _parse_price_to_int(raw_price)
        if price is None:
            continue

        href = _first_attr(
            node,
            ["a._1fQZEK", "a.s1Q9rs", "a.CGtC98", "a.wjcEIp", "a"],
            "href",
        )
        if href and href.startswith("/"):
            product_url = f"https://www.flipkart.com{href}"
        else:
            product_url = href or "https://www.flipkart.com"

        stable_id = product_url.split("?")[0]

        items.append(
            {
                "source": "flipkart",
                "id": f"flipkart:{stable_id}",
                "title": title,
                "price": price,
                "url": product_url,
            }
        )

    return items


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def load_previous_prices() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read existing %s (%s). Starting fresh.", DATA_FILE, exc)
        return {}


def save_prices(all_items: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "last_checked": int(time.time()),
        "items": {
            item["id"]: {
                "title": item["title"],
                "price": item["price"],
                "url": item["url"],
                "source": item["source"],
            }
            for item in all_items
        },
    }
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        log.info("Saved %d items to %s", len(all_items), DATA_FILE)
    except OSError as exc:
        log.error("Failed to write %s: %s", DATA_FILE, exc)


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def find_alerts(current_items: list[dict], previous: dict) -> list[dict]:
    """
    Returns list of items that should trigger an alert, each annotated with
    'reason' and (if applicable) 'previous_price'.
    """
    alerts = []
    prev_items = previous.get("items", {})

    for item in current_items:
        prev_entry = prev_items.get(item["id"])
        reasons = []

        if item["price"] < PRICE_THRESHOLD:
            reasons.append(f"under ₹{PRICE_THRESHOLD}")

        if prev_entry and item["price"] < prev_entry.get("price", float("inf")):
            reasons.append(
                f"price drop from ₹{prev_entry['price']} to ₹{item['price']}"
            )

        if reasons:
            alert = dict(item)
            alert["reason"] = ", ".join(reasons)
            if prev_entry:
                alert["previous_price"] = prev_entry.get("price")
            alerts.append(alert)

    return alerts


def build_email_body(alerts: list[dict]) -> str:
    lines = [f"Found {len(alerts)} Crocs listing(s) worth a look:\n"]
    for a in alerts:
        lines.append(f"- [{a['source'].upper()}] {a['title']}")
        lines.append(f"  Price: ₹{a['price']}  ({a['reason']})")
        lines.append(f"  Link: {a['url']}")
        lines.append("")
    return "\n".join(lines)


def send_email_alert(alerts: list[dict]) -> bool:
    """Send the alert email. Returns True on success, False otherwise.
    Never raises - failures are logged and swallowed so the workflow
    still exits cleanly."""
    if not alerts:
        log.info("No alerts to send.")
        return True

    if not (GMAIL_USERNAME and GMAIL_APP_PASSWORD and ALERT_TO):
        log.error(
            "Email not sent: GMAIL_USERNAME / GMAIL_APP_PASSWORD / ALERT_TO "
            "not fully configured."
        )
        return False

    # Sanity-check the app password shape. A real Google App Password is
    # exactly 16 characters (letters only) once spaces are stripped. If this
    # doesn't match, the value in the secret almost certainly isn't a valid
    # app password (e.g. the normal account password was pasted instead) -
    # fail fast with a clear message instead of letting Google return a
    # generic 535 error.
    if len(GMAIL_APP_PASSWORD) != 16 or not GMAIL_APP_PASSWORD.isalnum():
        log.error(
            "GMAIL_APP_PASSWORD does not look like a valid Google App "
            "Password (expected 16 alphanumeric characters after removing "
            "spaces, got %d characters). Regenerate one at "
            "https://myaccount.google.com/apppasswords and update the "
            "GitHub secret.",
            len(GMAIL_APP_PASSWORD),
        )
        return False

    subject = f"🐊 Crocs Price Alert - {len(alerts)} deal(s) found"
    body = build_email_body(alerts)

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USERNAME
    msg["To"] = ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    raw_message = msg.as_string()

    # Try SSL (465) first, then fall back to STARTTLS (587). Some networks
    # / CI runners have inconsistent behavior between the two; trying both
    # gives the best chance of success and the logs will show exactly which
    # one Google rejected and why.
    attempts = [
        ("SMTP_SSL", 465),
        ("SMTP+STARTTLS", 587),
    ]

    last_error = None
    for label, port in attempts:
        try:
            if label == "SMTP_SSL":
                server = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30)
            else:
                server = smtplib.SMTP("smtp.gmail.com", port, timeout=30)

            with server:
                server.set_debuglevel(0)
                if label == "SMTP+STARTTLS":
                    server.starttls()
                server.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_USERNAME, [ALERT_TO], raw_message)

            log.info(
                "Alert email sent to %s via %s:%d (%d items)",
                ALERT_TO, label, port, len(alerts),
            )
            return True

        except smtplib.SMTPAuthenticationError as exc:
            last_error = exc
            log.error(
                "%s:%d auth rejected by Google (code %s): %s",
                label, port, exc.smtp_code, exc.smtp_error,
            )
        except (smtplib.SMTPException, OSError) as exc:
            last_error = exc
            log.error("%s:%d failed: %s", label, port, exc)

    log.error(
        "All SMTP attempts failed. Last error: %s. This is almost always "
        "an account-side issue, not a code issue - see README 'Troubleshooting "
        "Gmail SMTP errors' section.",
        last_error,
    )
    return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run() -> None:
    log.info("Starting Crocs price check...")

    amazon_html = fetch_via_scraperapi(AMAZON_SEARCH_URL, render=True)
    amazon_items = parse_amazon(amazon_html)
    log.info("Amazon.in: parsed %d relevant item(s)", len(amazon_items))

    flipkart_html = fetch_via_scraperapi(FLIPKART_SEARCH_URL, render=True)
    flipkart_items = parse_flipkart(flipkart_html)
    log.info("Flipkart: parsed %d relevant item(s)", len(flipkart_items))

    all_items = amazon_items + flipkart_items

    if not all_items:
        log.warning(
            "0 items found this run (site changes, blocked request, or no "
            "matching results). Exiting cleanly without failing the workflow."
        )
        save_prices([])
        return

    previous = load_previous_prices()
    alerts = find_alerts(all_items, previous)

    if alerts:
        log.info("%d item(s) triggered an alert condition.", len(alerts))
        send_email_alert(alerts)
    else:
        log.info("No price-drop or under-threshold items this run.")

    save_prices(all_items)


def main() -> None:
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - intentionally broad
        log.exception("Unexpected error during price check: %s", exc)
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
