import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


PRICE_LIMIT = float(os.getenv("PRICE_LIMIT", "2000"))
SEARCH_TERM = os.getenv("SEARCH_TERM", "Crocs")
STATE_FILE = Path(os.getenv("STATE_FILE", "data/prices.json"))
MAX_PRODUCTS_PER_SITE = int(os.getenv("MAX_PRODUCTS_PER_SITE", "40"))

AMAZON_URL = (
    "https://www.amazon.in/s?"
    + "k=" + quote_plus(SEARCH_TERM)
    + "&rh=p_36%3A100-200000"
)
FLIPKART_URL = "https://www.flipkart.com/search?q=" + quote_plus(SEARCH_TERM)


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


def normalise_url(url):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.amazon.in" + url
    return url


def is_crocs_title(title):
    return bool(title and re.search(r"\bcrocs?\b", title, re.I))


def parse_amazon(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for card in soup.select('[data-component-type="s-search-result"]'):
        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

        title_el = card.select_one("h2 a span")
        if not title_el:
            title_el = card.select_one("h2 span")

        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not is_crocs_title(title):
            continue

        price_el = (
            card.select_one(".a-price .a-offscreen")
            or card.select_one(".a-price-whole")
        )
        price = clean_price(price_el.get_text(" ", strip=True) if price_el else "")
        link_el = card.select_one("h2 a")
        url = link_el.get("href", "") if link_el else ""
        url = normalise_url(url)

        if price is not None and url:
            products.append({
                "site": "Amazon",
                "title": title,
                "price": price,
                "url": url,
            })

    return products


def parse_flipkart(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen = set()

    # Flipkart's class names change frequently, so the parser deliberately
    # looks for links/cards containing a Crocs title and nearby ₹ price.
    for link in soup.select("a[href]"):
        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

        text = link.get_text(" ", strip=True)
        if not is_crocs_title(text):
            continue

        href = link.get("href", "")
        if not href or href.startswith("#"):
            continue

        card = link
        for _ in range(4):
            if card.parent:
                card = card.parent

        card_text = card.get_text(" ", strip=True)
        prices = re.findall(r"₹\s*[\d,]+", card_text)
        if not prices:
            continue

        price = clean_price(prices[0])
        if price is None:
            continue

        url = href
        if url.startswith("/"):
            url = "https://www.flipkart.com" + url

        key = url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)

        # Keep the title compact. If the link itself contains lots of metadata,
        # use the first sentence-like chunk.
        title = text[:250]

        products.append({
            "site": "Flipkart",
            "title": title,
            "price": price,
            "url": url,
        })

    return products


def fetch_pages():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
        )

        results = []

        for site, url, parser in [
            ("Amazon", AMAZON_URL, parse_amazon),
            ("Flipkart", FLIPKART_URL, parse_flipkart),
        ]:
            page = context.new_page()
            try:
                print(f"Checking {site}: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                html = page.content()

                items = parser(html)
                print(f"{site}: found {len(items)} Crocs listings")

                results.extend(items)

            except PlaywrightTimeoutError:
                print(f"{site}: page timed out", file=sys.stderr)
            except Exception as exc:
                print(f"{site}: {type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                page.close()

        browser.close()
        return results


def make_key(item):
    # URL is preferred because titles can change.
    return f"{item['site']}|{item['url'].split('?')[0]}"


def update_and_find_alerts(items, state):
    alerts = []
    checked = now_iso()

    for item in items:
        key = make_key(item)
        old = state.get(key, {})
        old_price = old.get("price")

        item["checked_at"] = checked

        should_alert = (
            item["price"] <= PRICE_LIMIT
            and (
                old_price is None
                or float(old_price) > PRICE_LIMIT
                or item["price"] < float(old_price)
            )
        )

        if should_alert:
            alerts.append({
                **item,
                "previous_price": old_price,
            })

        state[key] = {
            "site": item["site"],
            "title": item["title"],
            "price": item["price"],
            "url": item["url"],
            "last_checked": checked,
            "lowest_seen": min(
                float(old.get("lowest_seen", item["price"])),
                item["price"],
            ),
        }

    return alerts


def send_email(alerts):
    username = os.getenv("GMAIL_USERNAME")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("ALERT_TO")

    if not username or not app_password or not recipient:
        raise RuntimeError(
            "Missing GMAIL_USERNAME, GMAIL_APP_PASSWORD or ALERT_TO."
        )

    msg = EmailMessage()
    msg["Subject"] = f"🔥 Crocs price alert: {len(alerts)} deal(s) ≤ ₹{PRICE_LIMIT:g}"
    msg["From"] = username
    msg["To"] = recipient

    lines = [
        f"Crocs price alert — products at or below ₹{PRICE_LIMIT:g}",
        "",
    ]

    for item in alerts:
        previous = item.get("previous_price")
        previous_text = (
            f"Previous: ₹{previous:,.0f}"
            if previous is not None
            else "First time seen"
        )

        lines.extend([
            f"🛍️ {item['title']}",
            f"Store: {item['site']}",
            f"Price: ₹{item['price']:,.0f}",
            previous_text,
            f"Open: {item['url']}",
            "",
        ])

    lines.append(
        "This alert was generated automatically by the Crocs price monitor."
    )

    msg.set_content("\n".join(lines))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(username, app_password)
        smtp.send_message(msg)

    print(f"Email sent to {recipient}")


def main():
    print(f"Crocs monitor started. Threshold: ₹{PRICE_LIMIT:g}")

    state = load_state()
    items = fetch_pages()

    if not items:
        print(
            "No listings were parsed. State will not be changed.",
            file=sys.stderr,
        )
        return 2

    alerts = update_and_find_alerts(items, state)
    save_state(state)

    print(f"Parsed listings: {len(items)}")
    print(f"Alerts: {len(alerts)}")

    for item in alerts:
        print(
            f"ALERT: {item['site']} | "
            f"₹{item['price']:,.0f} | {item['title']}"
        )

    if alerts:
        send_email(alerts)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
