import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# CONFIGURATION
# ============================================================
PRICE_LIMIT = float(os.getenv("PRICE_LIMIT", "10000"))
SEARCH_TERM = os.getenv("SEARCH_TERM", "Crocs")
STATE_FILE = Path(os.getenv("STATE_FILE", "data/prices.json"))
MAX_PRODUCTS_PER_SITE = int(os.getenv("MAX_PRODUCTS_PER_SITE", "40"))

AMAZON_URL = "https://www.amazon.in/s?k=" + quote_plus(SEARCH_TERM) + "&rh=p_36%3A100-200000"
FLIPKART_URL = "https://www.flipkart.com/search?q=" + quote_plus(SEARCH_TERM)

# ============================================================
# GENERAL HELPERS
# ============================================================
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def clean_price(value):
    if value is None:
        return None
    text = str(value).replace(",", "")
    match = re.search(r"(?:₹|Rs\.?|INR)?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(STATE_FILE)

# ============================================================
# URL / TITLE HELPERS
# ============================================================
def normalise_url(url, site="Amazon"):
    if not url:
        return ""
    if site == "Amazon":
        if not url.startswith("http"):
            url = "https://www.amazon.in" + url
        match = re.search(r"/(dp|gp/product)/([A-Z0-9]{10})", url)
        if match:
            return f"https://www.amazon.in/dp/{match.group(2)}"
    elif site == "Flipkart":
        if not url.startswith("http"):
            url = "https://www.flipkart.com" + url
        url = url.split("?")[0]
    return url

def clean_title(title):
    if not title:
        return ""
    return re.sub(r"\s+", " ", title).strip()

# ============================================================
# PARSERS
# ============================================================
def parse_amazon(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    cards = soup.select('div[data-component-type="s-search-result"]')
    for card in cards:
        title_el = card.select_one("h2 a span")
        link_el = card.select_one("h2 a")
        price_el = card.select_one(".a-price .a-offscreen")
        if not (title_el and link_el and price_el):
            continue
        title = clean_title(title_el.get_text())
        url = normalise_url(link_el.get("href", ""), site="Amazon")
        price = clean_price(price_el.get_text())
        if title and url and price:
            items.append({"site": "Amazon", "title": title, "url": url, "price": price})
            if len(items) >= MAX_PRODUCTS_PER_SITE:
                break
    return items

def parse_flipkart(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    cards = soup.select("a[href*='/p/']")
    seen_urls = set()
    for card in cards:
        url = normalise_url(card.get("href", ""), site="Flipkart")
        if not url or url in seen_urls:
            continue
        price_el = card.select_one("div[class*='Nx9qWa'], div[class*='_30jeq3']")
        title_el = card.select_one("div[class*='_2WkLfr'], div[class*='WlsL3V']")
        if not price_el:
            parent = card.parent
            if parent:
                price_el = parent.select_one("div[class*='Nx9qWa'], div[class*='_30jeq3']")
                title_el = title_el or parent.select_one("div[class*='_2WkLfr'], div[class*='WlsL3V']")
        if not (title_el and price_el):
            continue
        title = clean_title(title_el.get_text())
        price = clean_price(price_el.get_text())
        if title and price:
            seen_urls.add(url)
            items.append({"site": "Flipkart", "title": title, "url": url, "price": price})
            if len(items) >= MAX_PRODUCTS_PER_SITE:
                break
    return items

# ============================================================
# PAGE FETCHING WITH SCRAPERAPI PROXY
# ============================================================
def fetch_pages():
    results = {}
    sites = [
        ("Amazon", AMAZON_URL, parse_amazon),
        ("Flipkart", FLIPKART_URL, parse_flipkart),
    ]

    scraperapi_key = os.getenv("SCRAPERAPI_KEY")

    with sync_playwright() as p:
        context_kwargs = {
            "locale": "en-IN",
            "timezone_id": "Asia/Kolkata",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080},
        }

        # Route through ScraperAPI proxy if secret is available
        if scraperapi_key:
            context_kwargs["proxy"] = {
                "server": f"http://scraperapi:{scraperapi_key}@proxy-server.scraperapi.com:8001"
            }
            context_kwargs["ignore_https_errors"] = True

        browser = p.chromium.launch(headless=True)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.on("download", lambda download: download.cancel())

        for site, url, parser in sites:
            try:
                print(f"Fetching {site}...")
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                content = page.content()
                parsed = parser(content)
                results[site] = parsed
                print(f"{site}: Parsed {len(parsed)} items.")
            except PlaywrightTimeoutError:
                print(f"{site}: Timeout error while loading page.", file=sys.stderr)
                results[site] = []
            except Exception as e:
                print(f"{site}: Unexpected error: {e}", file=sys.stderr)
                results[site] = []

        browser.close()

    return results

# ============================================================
# EMAIL NOTIFICATIONS
# ============================================================
def send_email(alerts):
    user = os.getenv("GMAIL_USERNAME")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("ALERT_TO", user)

    if not (user and password):
        print("Email credentials missing; skipping email notification.", file=sys.stderr)
        return

    msg = EmailMessage()
    msg["Subject"] = f"Crocs Price Alert! ({len(alerts)} items)"
    msg["From"] = user
    msg["To"] = recipient

    body = "Price Alert Triggered:\n\n"
    for item in alerts:
        body += f"[{item['site']}] {item['title']}\n"
        body += f"Price: ₹{item['price']}\n"
        body += f"URL: {item['url']}\n\n"

    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        print("Alert email sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}", file=sys.stderr)

# ============================================================
# MAIN ORCHESTRATION
# ============================================================
def main():
    state = load_state()
    fetched = fetch_pages()
    
    all_items = []
    for site, items in fetched.items():
        all_items.extend(items)

    print(f"Total parsed listings across sites: {len(all_items)}")

    if not all_items:
        print("No listings were parsed. State will not be changed.")
        return 0  # Exit cleanly with code 0 to prevent GitHub Action failure mark

    alerts = []
    updated_state = state.copy()

    for item in all_items:
        key = item["url"]
        price = item["price"]
        prev_price = state.get(key, {}).get("price")

        # Trigger if price dropped or price is under threshold
        if (prev_price is not None and price < prev_price) or (price <= PRICE_LIMIT):
            alerts.append(item)

        updated_state[key] = {
            "title": item["title"],
            "site": item["site"],
            "price": price,
            "updated_at": now_iso(),
        }

    save_state(updated_state)

    if alerts:
        print(f"Alerts triggered: {len(alerts)}")
        send_email(alerts)
    else:
        print("No price alerts triggered in this run.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
