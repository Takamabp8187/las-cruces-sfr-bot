"""
Las Cruces, NM — Daily SFR Listing Report
==========================================
Scrapes Zillow, Redfin, and Realtor.com for single-family homes
in Las Cruces, NM under $180,000 with ≥2 BD, ≥1 BA, ≥1,000 sq ft.

Day 1: Sends all active qualifying listings.
Day 2+: Sends only new listings and price changes since the previous run.

Setup:
  pip install requests beautifulsoup4 lxml python-dotenv

Usage:
  python search_listings.py

Environment variables (in .env file):
  GMAIL_USER       — your Gmail address
  GMAIL_APP_PASS   — Gmail App Password (not your regular password)
  RECIPIENT_EMAIL  — where to send the report
  STATE_FILE       — path to JSON file that tracks seen listings (default: state.json)
"""

import os
import json
import time
import datetime
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────

GMAIL_USER      = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS  = os.getenv("GMAIL_APP_PASS", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "bartljordan@gmail.com")
STATE_FILE      = os.getenv("STATE_FILE", "state.json")

SEARCH_PARAMS = {
    "city":      "Las Cruces",
    "state":     "NM",
    "max_price": 180_000,
    "min_beds":  2,
    "min_baths": 1,
    "min_sqft":  1_000,
    "type":      "single-family",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ─── State management ────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load previously seen listings from disk."""
    p = Path(STATE_FILE)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Persist listing state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Scrapers ────────────────────────────────────────────────────────────────

def fetch_zillow() -> list[dict]:
    """
    Fetch listings from Zillow.
    Note: Zillow aggressively blocks scrapers. This uses their
    public search URL. If blocked, rotate User-Agent or use
    ScraperAPI / BrightData proxy (see README).
    """
    url = (
        "https://www.zillow.com/las-cruces-nm/houses/"
        "?searchQueryState=%7B%22filterState%22%3A%7B"
        "%22price%22%3A%7B%22max%22%3A180000%7D%2C"
        "%22beds%22%3A%7B%22min%22%3A2%7D%2C"
        "%22baths%22%3A%7B%22min%22%3A1%7D%2C"
        "%22sqft%22%3A%7B%22min%22%3A1000%7D%2C"
        "%22con%22%3A%7B%22value%22%3Afalse%7D%2C"
        "%22apa%22%3A%7B%22value%22%3Afalse%7D%2C"
        "%22mf%22%3A%7B%22value%22%3Afalse%7D%2C"
        "%22land%22%3A%7B%22value%22%3Afalse%7D%7D%7D"
    )
    listings = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Zillow embeds listing data in a <script> tag as JSON
        import re
        pattern = re.compile(r'"zpid":(\d+).*?"price":(\d+).*?"beds":(\d+).*?"baths":([\d.]+).*?"livingArea":(\d+).*?"streetAddress":"([^"]+)".*?"city":"([^"]+)".*?"state":"([^"]+)".*?"zipcode":"([^"]+)"', re.DOTALL)
        # Fallback: parse visible listing cards
        cards = soup.select("article.list-card, [data-test='property-card']")
        for card in cards:
            try:
                price_el  = card.select_one("[data-test='property-card-price'], .list-card-price")
                addr_el   = card.select_one("address, .list-card-addr")
                detail_el = card.select_one(".list-card-details, [data-test='property-card-details']")
                link_el   = card.select_one("a[href]")

                if not (price_el and addr_el):
                    continue

                price_str = price_el.get_text(strip=True).replace("$", "").replace(",", "").replace("+", "")
                price     = int("".join(filter(str.isdigit, price_str))) if price_str else 0

                if price == 0 or price > SEARCH_PARAMS["max_price"]:
                    continue

                addr = addr_el.get_text(strip=True)
                href = link_el["href"] if link_el else ""
                if href and not href.startswith("http"):
                    href = "https://www.zillow.com" + href

                details = detail_el.get_text(" ", strip=True) if detail_el else ""

                listings.append({
                    "id":      f"zillow:{addr.lower().replace(' ', '-')}",
                    "source":  "Zillow",
                    "address": addr,
                    "price":   price,
                    "details": details,
                    "url":     href,
                })
            except Exception as e:
                log.debug(f"Zillow card parse error: {e}")

        log.info(f"Zillow: found {len(listings)} qualifying cards")
    except Exception as e:
        log.warning(f"Zillow fetch error: {e}")

    return listings


def fetch_redfin() -> list[dict]:
    """Fetch listings from Redfin's GIS API (more scraper-friendly)."""
    # Redfin GIS search endpoint
    url = (
        "https://www.redfin.com/stingray/api/gis?al=1"
        "&market=las-cruces"
        "&max_price=180000"
        "&min_beds=2"
        "&min_baths=1"
        "&min_sqft=1000"
        "&property_type=1"  # 1 = single family
        "&status=1"         # active
        "&region_id=17185"  # Las Cruces, NM
        "&region_type=6"
        "&num_homes=350"
    )
    listings = []
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=15)
        # Redfin prefixes JSON with ")]}'\n" to prevent CSRF
        text = resp.text
        if text.startswith(")]}"):
            text = text[text.index("\n") + 1:]
        data = json.loads(text)

        homes = data.get("payload", {}).get("homes", [])
        for h in homes:
            price = h.get("price", {}).get("value", 0)
            beds  = h.get("beds", 0)
            baths = h.get("baths", 0)
            sqft  = h.get("sqFt", {}).get("value", 0)
            if price > SEARCH_PARAMS["max_price"]:
                continue
            if beds < SEARCH_PARAMS["min_beds"]:
                continue
            if baths < SEARCH_PARAMS["min_baths"]:
                continue
            if sqft < SEARCH_PARAMS["min_sqft"]:
                continue

            addr = h.get("streetLine", {}).get("value", "")
            city = h.get("city", "")
            state = h.get("state", "")
            zipcode = h.get("zip", "")
            full_addr = f"{addr}, {city}, {state} {zipcode}"
            url_path = h.get("url", "")
            full_url = f"https://www.redfin.com{url_path}" if url_path else ""

            listings.append({
                "id":      f"redfin:{h.get('mlsId', {}).get('value', full_addr)}",
                "source":  "Redfin",
                "address": full_addr,
                "price":   price,
                "details": f"{beds} BD / {baths} BA · {sqft:,} sq ft",
                "url":     full_url,
            })

        log.info(f"Redfin: found {len(listings)} qualifying homes")
    except Exception as e:
        log.warning(f"Redfin fetch error: {e}")

    return listings


def fetch_realtor() -> list[dict]:
    """Fetch from Realtor.com using their public search page."""
    url = (
        "https://www.realtor.com/realestateandhomes-search/Las-Cruces_NM"
        "/type-single-family-home/price-na-180000/beds-2/sqft-1000"
    )
    listings = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        import re, json as _json
        # Realtor.com embeds data in __NEXT_DATA__
        script = soup.find("script", {"id": "__NEXT_DATA__"})
        if script:
            data = _json.loads(script.string)
            props = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("properties", [])
            )
            for p in props:
                price = p.get("list_price", 0) or 0
                if price > SEARCH_PARAMS["max_price"]:
                    continue
                beds  = p.get("description", {}).get("beds", 0) or 0
                baths = p.get("description", {}).get("baths", 0) or 0
                sqft  = p.get("description", {}).get("sqft", 0) or 0
                if beds < 2 or baths < 1 or sqft < 1000:
                    continue

                addr = p.get("location", {}).get("address", {})
                full_addr = f"{addr.get('line', '')}, {addr.get('city', '')}, {addr.get('state_code', '')} {addr.get('postal_code', '')}"
                slug = p.get("permalink", "")
                full_url = f"https://www.realtor.com/realestateandhomes-detail/{slug}" if slug else ""

                listings.append({
                    "id":      f"realtor:{p.get('property_id', full_addr)}",
                    "source":  "Realtor.com",
                    "address": full_addr.strip(),
                    "price":   price,
                    "details": f"{beds} BD / {baths} BA · {sqft:,} sq ft",
                    "url":     full_url,
                })

        log.info(f"Realtor.com: found {len(listings)} qualifying homes")
    except Exception as e:
        log.warning(f"Realtor.com fetch error: {e}")

    return listings


# ─── Deduplication ───────────────────────────────────────────────────────────

def deduplicate(listings: list[dict]) -> list[dict]:
    """Remove cross-source duplicates by normalizing addresses."""
    seen_addrs = set()
    unique = []
    for l in listings:
        key = l["address"].lower().strip().split(",")[0].strip()
        if key not in seen_addrs:
            seen_addrs.add(key)
            unique.append(l)
    return unique


# ─── Diff against previous state ─────────────────────────────────────────────

def compute_diff(listings: list[dict], state: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns:
        new_listings    — IDs not seen before
        price_changes   — same ID but price changed
        all_listings    — full list (for Day 1)
    """
    new_listings   = []
    price_changes  = []

    for l in listings:
        lid = l["id"]
        if lid not in state:
            new_listings.append(l)
        elif state[lid]["price"] != l["price"]:
            l["old_price"] = state[lid]["price"]
            price_changes.append(l)

    return new_listings, price_changes, listings


# ─── Email builder ───────────────────────────────────────────────────────────

def build_html_report(
    new_listings: list[dict],
    price_changes: list[dict],
    all_listings: list[dict],
    is_day_one: bool,
    run_date: str,
) -> str:
    def listing_rows(items, show_old_price=False):
        if not items:
            return "<tr><td colspan='5' style='color:#888;padding:12px'>None today.</td></tr>"
        rows = ""
        for l in items:
            price_cell = f"<strong style='color:#1a6b1a'>${l['price']:,}</strong>"
            if show_old_price and "old_price" in l:
                diff = l["price"] - l["old_price"]
                arrow = "▼" if diff < 0 else "▲"
                color = "#1a6b1a" if diff < 0 else "#b71c1c"
                price_cell += (
                    f"<br><span style='color:{color};font-size:11px'>"
                    f"{arrow} ${abs(diff):,} (was ${l['old_price']:,})</span>"
                )
            rows += f"""
            <tr>
              <td><a href="{l['url']}" style="color:#2c5f8a;font-weight:bold">{l['address']}</a></td>
              <td>{price_cell}</td>
              <td>{l.get('details','')}</td>
              <td>{l['source']}</td>
            </tr>"""
        return rows

    if is_day_one:
        content_section = f"""
        <h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:6px">
          📋 All Active Listings — Day 1 Baseline ({len(all_listings)} found)
        </h2>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#2c5f8a;color:white">
            <th style="padding:10px;text-align:left">Address</th>
            <th style="padding:10px;text-align:left">Price</th>
            <th style="padding:10px;text-align:left">Details</th>
            <th style="padding:10px;text-align:left">Source</th>
          </tr>
          {listing_rows(all_listings)}
        </table>"""
    else:
        content_section = f"""
        <h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:6px">
          🆕 New Listings ({len(new_listings)})
        </h2>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#2c5f8a;color:white">
            <th style="padding:10px;text-align:left">Address</th>
            <th style="padding:10px;text-align:left">Price</th>
            <th style="padding:10px;text-align:left">Details</th>
            <th style="padding:10px;text-align:left">Source</th>
          </tr>
          {listing_rows(new_listings)}
        </table>

        <h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:6px;margin-top:28px">
          💰 Price Changes ({len(price_changes)})
        </h2>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#2c5f8a;color:white">
            <th style="padding:10px;text-align:left">Address</th>
            <th style="padding:10px;text-align:left">New Price</th>
            <th style="padding:10px;text-align:left">Details</th>
            <th style="padding:10px;text-align:left">Source</th>
          </tr>
          {listing_rows(price_changes, show_old_price=True)}
        </table>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;max-width:720px;margin:0 auto;padding:16px">
  <div style="background:#2c5f8a;color:white;padding:16px 20px;border-radius:6px">
    <h1 style="margin:0;font-size:20px">🏠 Las Cruces SFR Daily Report</h1>
    <p style="margin:4px 0 0;font-size:14px;opacity:.85">{run_date} &nbsp;|&nbsp; Max $180K · 2+ BD · 1+ BA · 1,000+ sq ft · Single-Family</p>
  </div>

  <div style="background:#f0f6ff;border-left:4px solid #2c5f8a;padding:12px 16px;margin:16px 0;border-radius:4px;font-size:13px">
    <strong>Sources searched:</strong> Zillow, Redfin, Realtor.com &nbsp;·&nbsp;
    <strong>Total qualifying:</strong> {len(all_listings)} active listings &nbsp;·&nbsp;
    <strong>{"New + price changes" if not is_day_one else "Day 1 — full baseline"}</strong>
  </div>

  {content_section}

  <div style="margin-top:28px;font-size:12px;color:#888;border-top:1px solid #ddd;padding-top:12px">
    <p>🔗 Search directly:
      <a href="https://www.zillow.com/las-cruces-nm/houses/?searchQueryState=%7B%22filterState%22%3A%7B%22price%22%3A%7B%22max%22%3A180000%7D%2C%22beds%22%3A%7B%22min%22%3A2%7D%2C%22baths%22%3A%7B%22min%22%3A1%7D%2C%22sqft%22%3A%7B%22min%22%3A1000%7D%7D%7D">Zillow</a> ·
      <a href="https://www.redfin.com/city/10005/NM/Las-Cruces/filter/max-price=180000,min-beds=2,min-baths=1,min-sqft=1000,property-type=house">Redfin</a> ·
      <a href="https://www.realtor.com/realestateandhomes-search/Las-Cruces_NM/type-single-family-home/price-na-180000/beds-2/sqft-1000">Realtor.com</a>
    </p>
    <p>This report is generated automatically. Data sourced from public listing sites and may have slight delays.</p>
  </div>
</body>
</html>"""


# ─── Email sender ─────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASS)
        smtp.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())

    log.info(f"Email sent to {RECIPIENT_EMAIL}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    run_date   = datetime.date.today().strftime("%B %d, %Y")
    state      = load_state()
    is_day_one = len(state) == 0

    log.info("Fetching listings from all sources...")
    raw = fetch_zillow() + fetch_redfin() + fetch_realtor()
    listings   = deduplicate(raw)

    log.info(f"Total unique qualifying listings: {len(listings)}")

    new_l, price_ch, all_l = compute_diff(listings, state)

    if is_day_one:
        subject = f"🏠 Las Cruces SFR — Day 1 Baseline: {len(all_l)} Active Listings | {run_date}"
    else:
        subject = (
            f"🏠 Las Cruces SFR — {len(new_l)} New · {len(price_ch)} Price Changes | {run_date}"
        )

    html = build_html_report(new_l, price_ch, all_l, is_day_one, run_date)

    send_email(subject, html)

    # Update state
    new_state = {}
    for l in listings:
        new_state[l["id"]] = {"price": l["price"], "address": l["address"]}
    save_state(new_state)

    log.info("Done. State saved.")


if __name__ == "__main__":
    main()
