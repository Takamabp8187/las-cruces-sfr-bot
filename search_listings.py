"""
Las Cruces, NM - Daily SFR Listing Report
==========================================
Searches Redfin and Realtor.com for single-family homes
in Las Cruces, NM under $180,000 with >=2 BD, >=1 BA, >=1,000 sq ft.

Requests are routed through ScraperAPI to bypass bot-blocking.

Day 1: Sends all active qualifying listings.
Day 2+: Sends only new listings and price changes since the previous run.

Environment variables:
  GMAIL_USER       -- your Gmail address
  GMAIL_APP_PASS   -- Gmail App Password
  RECIPIENT_EMAIL  -- where to send the report
  SCRAPER_API_KEY  -- your ScraperAPI key (scraperapi.com)
  STATE_FILE       -- path to state JSON file (default: state.json)
"""

import os
import csv
import io
import json
import datetime
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# --- Config ------------------------------------------------------------------

GMAIL_USER      = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS  = os.getenv("GMAIL_APP_PASS", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "bartljordan@gmail.com")
STATE_FILE      = os.getenv("STATE_FILE", "state.json")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

SEARCH_PARAMS = {
    "max_price": 180_000,
    "min_beds":  2,
    "min_baths": 1,
    "min_sqft":  1_000,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

scraper_status = {}


# --- ScraperAPI helper -------------------------------------------------------

def scrape(url, render_js=False, premium=False):
    """Fetch a URL via ScraperAPI to bypass bot-blocking.
    render_js=True  -- uses headless browser (5x credits, needed for JS-heavy pages)
    premium=True    -- uses residential IPs (10x credits, harder to block)
    """
    parts = ""
    if render_js:
        parts += "&render=true"
    if premium:
        parts += "&premium=true"
    api_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={quote_plus(url)}{parts}"
    resp = requests.get(api_url, timeout=120)
    resp.raise_for_status()
    return resp


# --- State management --------------------------------------------------------

def load_state():
    p = Path(STATE_FILE)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- Scrapers ----------------------------------------------------------------

def fetch_redfin():
    """Fetch Redfin listings via the JSON GIS API."""
    listings = []
    json_url = (
        "https://www.redfin.com/stingray/api/gis?"
        "al=1&region_id=10004&region_type=6"
        "&min_beds=2&min_baths=1&max_price=180000&min_sqft=1000"
        "&uipt=1&status=9&num_homes=500&sf=1,2,3,5,6,7"
    )
    try:
        resp = scrape(json_url)
        text = resp.text.strip()
        log.info(f"Redfin JSON: status={resp.status_code} length={len(text)} preview={text[:120]}")

        # Redfin prefixes its JSON responses with "{}&&"
        if text.startswith("{}&&"):
            text = text[4:]

        data = json.loads(text)
        homes = (
            data.get("payload", {}).get("homes", [])
            or data.get("payload", {}).get("searchResults", {}).get("listingResultsPage", {}).get("results", [])
        )
        log.info(f"Redfin raw results: {len(homes)}")

        for h in homes:
            try:
                price = h.get("price", {}).get("value", 0) or 0
                if price == 0 or price > SEARCH_PARAMS["max_price"]:
                    continue
                beds  = h.get("beds",  0) or 0
                baths = h.get("baths", 0) or 0
                sqft  = h.get("sqFt", {}).get("value", 0) or h.get("sqft", 0) or 0
                if beds < 2 or baths < 1 or sqft < 1000:
                    continue
                addr     = h.get("streetLine", {}).get("value", "") or h.get("address", {}).get("streetLine", "")
                city_st  = h.get("cityStateZip", {}).get("value", "") or ""
                url_path = h.get("url", "")
                full_url = f"https://www.redfin.com{url_path}" if url_path else "https://www.redfin.com"
                full_addr = f"{addr}, {city_st}".strip(", ")
                listings.append({
                    "id":      f"redfin:{addr.lower().replace(' ', '-')}",
                    "source":  "Redfin",
                    "address": full_addr,
                    "price":   price,
                    "details": f"{int(beds)} BD / {baths:.0f} BA / {int(sqft):,} sq ft",
                    "url":     full_url,
                })
            except Exception as e:
                log.debug(f"Redfin row error: {e}")

        scraper_status["Redfin"] = f"OK - {len(listings)} listings"
        log.info(f"Redfin: {len(listings)} qualifying listings")
    except json.JSONDecodeError as e:
        log.warning(f"Redfin JSON parse error: {e}")
        scraper_status["Redfin"] = f"JSON parse failed - {e}"
    except Exception as e:
        log.warning(f"Redfin error: {e}")
        scraper_status["Redfin"] = f"FAILED - {e}"
    return listings


def fetch_realtor():
    """Fetch Realtor.com listings using premium residential proxies."""
    listings = []
    url = "https://www.realtor.com/realestateandhomes-search/Las-Cruces_NM/type-single-family-home/price-na-180000/beds-2/sqft-1000"
    try:
        # Use premium residential proxies -- harder for Realtor.com to block.
        # Realtor.com's __NEXT_DATA__ is server-side rendered so we don't need render_js.
        resp = scrape(url, premium=True)
        log.info(f"Realtor.com: status={resp.status_code} length={len(resp.text)}")
        soup   = BeautifulSoup(resp.text, "lxml")
        script = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script:
            log.warning("Realtor.com: __NEXT_DATA__ not found")
            scraper_status["Realtor.com"] = "Page structure not found (no __NEXT_DATA__)"
            return listings
        data  = json.loads(script.string)
        props = []
        for path in [
            ["props", "pageProps", "properties"],
            ["props", "pageProps", "searchResults", "home_search", "results"],
            ["props", "pageProps", "searchResults", "results"],
        ]:
            try:
                node = data
                for key in path:
                    node = node[key]
                if node:
                    props = node
                    break
            except (KeyError, TypeError):
                continue
        log.info(f"Realtor.com raw results: {len(props)}")
        for p in props:
            try:
                price = p.get("list_price", 0) or p.get("price", 0) or 0
                if price > SEARCH_PARAMS["max_price"]:
                    continue
                desc  = p.get("description", {})
                beds  = desc.get("beds",  p.get("beds",  0)) or 0
                baths = desc.get("baths_consolidated", desc.get("baths", p.get("baths", 0))) or 0
                sqft  = desc.get("sqft",  p.get("sqft",  0)) or 0
                if beds < 2 or baths < 1 or sqft < 1000:
                    continue
                prop_type = (desc.get("type", p.get("sub_type", "")) or "").lower()
                if prop_type and "single" not in prop_type and "house" not in prop_type and "sfr" not in prop_type:
                    continue
                loc       = p.get("location", {}).get("address", p.get("address", {}))
                line      = loc.get("line", loc.get("street", "")) or ""
                city      = loc.get("city", "") or ""
                st        = loc.get("state_code", loc.get("state", "")) or ""
                zc        = loc.get("postal_code", loc.get("zip", "")) or ""
                full_addr = f"{line}, {city}, {st} {zc}".strip(", ")
                slug      = p.get("permalink", p.get("property_id", ""))
                full_url  = f"https://www.realtor.com/realestateandhomes-detail/{slug}" if slug else ""
                pid       = p.get("property_id", full_addr)
                listings.append({
                    "id":      f"realtor:{pid}",
                    "source":  "Realtor.com",
                    "address": full_addr,
                    "price":   price,
                    "details": f"{beds} BD / {baths} BA / {int(sqft):,} sq ft",
                    "url":     full_url,
                })
            except Exception as e:
                log.debug(f"Realtor.com row error: {e}")
        scraper_status["Realtor.com"] = f"OK - {len(listings)} listings"
        log.info(f"Realtor.com: {len(listings)} qualifying listings")
    except Exception as e:
        log.warning(f"Realtor.com error: {e}")
        scraper_status["Realtor.com"] = f"FAILED - {e}"
    return listings


# --- Deduplication -----------------------------------------------------------

def deduplicate(listings):
    seen, unique = set(), []
    for l in listings:
        key = l["address"].lower().strip().split(",")[0].strip()
        if key not in seen:
            seen.add(key)
            unique.append(l)
    return unique


# --- Diff --------------------------------------------------------------------

def compute_diff(listings, state):
    new_listings, price_changes = [], []
    for l in listings:
        lid = l["id"]
        if lid not in state:
            new_listings.append(l)
        elif state[lid]["price"] != l["price"]:
            l["old_price"] = state[lid]["price"]
            price_changes.append(l)
    return new_listings, price_changes, listings


# --- Email -------------------------------------------------------------------

def build_html_report(new_listings, price_changes, all_listings, is_day_one, run_date):
    th = "style='padding:10px;text-align:left;background:#2c5f8a;color:white'"

    def listing_rows(items, show_old_price=False):
        if not items:
            return "<tr><td colspan='4' style='color:#888;padding:12px'>None today.</td></tr>"
        rows = ""
        for l in items:
            price_cell = f"<strong style='color:#1a6b1a'>${l['price']:,}</strong>"
            if show_old_price and "old_price" in l:
                diff  = l["price"] - l["old_price"]
                arrow = "&#9660;" if diff < 0 else "&#9650;"
                color = "#1a6b1a" if diff < 0 else "#b71c1c"
                price_cell += f"<br><span style='color:{color};font-size:11px'>{arrow} ${abs(diff):,} (was ${l['old_price']:,})</span>"
            rows += f"""<tr style="border-bottom:1px solid #eee">
              <td style="padding:8px"><a href="{l['url']}" style="color:#2c5f8a;font-weight:bold">{l['address']}</a></td>
              <td style="padding:8px">{price_cell}</td>
              <td style="padding:8px">{l.get('details','')}</td>
              <td style="padding:8px;color:#666;font-size:12px">{l['source']}</td>
            </tr>"""
        return rows

    if is_day_one:
        body = f"""<h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:6px">
          All Active Listings - Day 1 Baseline ({len(all_listings)} found)</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr><th {th}>Address</th><th {th}>Price</th><th {th}>Details</th><th {th}>Source</th></tr>
          {listing_rows(all_listings)}</table>"""
    else:
        body = f"""<h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:6px">
          New Listings ({len(new_listings)})</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr><th {th}>Address</th><th {th}>Price</th><th {th}>Details</th><th {th}>Source</th></tr>
          {listing_rows(new_listings)}</table>
        <h2 style="color:#2c5f8a;border-bottom:2px solid #2c5f8a;padding-bottom:6px;margin-top:28px">
          Price Changes ({len(price_changes)})</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr><th {th}>Address</th><th {th}>New Price</th><th {th}>Details</th><th {th}>Source</th></tr>
          {listing_rows(price_changes, show_old_price=True)}</table>"""

    status_rows = "".join(
        f"<tr><td style='padding:6px 8px;font-weight:bold'>{s}</td><td style='padding:6px 8px'>{v}</td></tr>"
        for s, v in scraper_status.items()
    )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;max-width:740px;margin:0 auto;padding:16px">
  <div style="background:#2c5f8a;color:white;padding:16px 20px;border-radius:6px">
    <h1 style="margin:0;font-size:20px">Las Cruces SFR Daily Report</h1>
    <p style="margin:4px 0 0;font-size:14px;opacity:.85">{run_date} | Max $180K - 2+ BD - 1+ BA - 1,000+ sq ft</p>
  </div>
  <div style="background:#f0f6ff;border-left:4px solid #2c5f8a;padding:12px 16px;margin:16px 0;border-radius:4px;font-size:13px">
    <strong>Total listings found:</strong> {len(all_listings)} &nbsp;|&nbsp;
    <strong>{"Day 1 full baseline" if is_day_one else f"{len(new_listings)} new / {len(price_changes)} price changes"}</strong>
  </div>
  <div style="background:#fff8e1;border-left:4px solid #f9a825;padding:10px 14px;margin:16px 0;border-radius:4px;font-size:13px">
    <strong>Note on Zillow:</strong> Zillow blocks all automated tools. Use the manual link below to check Zillow.
  </div>
  {body}
  <details style="margin-top:24px;font-size:12px;color:#555;background:#f9f9f9;padding:12px;border-radius:4px">
    <summary style="cursor:pointer;font-weight:bold">Scraper Status (click to expand)</summary>
    <table style="margin-top:8px;width:100%;border-collapse:collapse">{status_rows}</table>
  </details>
  <div style="margin-top:20px;font-size:12px;color:#888;border-top:1px solid #ddd;padding-top:12px">
    <p>Search manually:
      <a href="https://www.zillow.com/las-cruces-nm/houses/?searchQueryState=%7B%22filterState%22%3A%7B%22price%22%3A%7B%22max%22%3A180000%7D%2C%22beds%22%3A%7B%22min%22%3A2%7D%2C%22baths%22%3A%7B%22min%22%3A1%7D%2C%22sqft%22%3A%7B%22min%22%3A1000%7D%7D%7D">Zillow</a> /
      <a href="https://www.redfin.com/city/10004/NM/Las-Cruces/filter/max-price=180000,min-beds=2,min-baths=1,min-sqft=1000,property-type=house">Redfin</a> /
      <a href="https://www.realtor.com/realestateandhomes-search/Las-Cruces_NM/type-single-family-home/price-na-180000/beds-2/sqft-1000">Realtor.com</a>
    </p>
  </div>
</body></html>"""


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASS)
        smtp.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
    log.info(f"Email sent to {RECIPIENT_EMAIL}")


# --- Main --------------------------------------------------------------------

def main():
    if not SCRAPER_API_KEY:
        raise ValueError("SCRAPER_API_KEY is not set.")

    run_date   = datetime.date.today().strftime("%B %d, %Y")
    state      = load_state()
    is_day_one = len(state) == 0

    log.info("Fetching listings via ScraperAPI...")
    listings = deduplicate(fetch_redfin() + fetch_realtor())
    log.info(f"Total unique qualifying listings: {len(listings)}")

    new_l, price_ch, all_l = compute_diff(listings, state)

    if is_day_one:
        subject = f"Las Cruces SFR - Day 1 Baseline: {len(all_l)} Listings | {run_date}"
    else:
        subject = f"Las Cruces SFR - {len(new_l)} New / {len(price_ch)} Price Changes | {run_date}"

    send_email(subject, build_html_report(new_l, price_ch, all_l, is_day_one, run_date))

    new_state = {l["id"]: {"price": l["price"], "address": l["address"]} for l in listings}
    save_state(new_state)
    log.info("Done.")


if __name__ == "__main__":
    main()
