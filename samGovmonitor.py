#!/usr/bin/env python3
"""
SAM.gov Contract Opportunity Monitor
-------------------------------------
Pulls new contract opportunities from the official SAM.gov Get Opportunities
Public API (https://open.gsa.gov/api/get-opportunities-public-api/),
filters them to a set of NAICS codes (your trades) and Central Texas
locations, and emails a daily digest.

This uses ONLY the official, public, documented API. No scraping of the
sam.gov website itself is performed (and none is needed -- the API covers
everything the search page shows).

SETUP:
    1. pip install requests
    2. Set environment variables (see config section below) or edit the
       CONFIG dict directly.
    3. Run manually first to test: python sam_gov_monitor.py
    4. Schedule it (see bottom of this file for cron / Task Scheduler examples)
"""

import os
import csv
import json
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# ---------------------------------------------------------------------------
# CONFIG -- edit these or set as environment variables
# ---------------------------------------------------------------------------

CONFIG = {
    # Get this after SAM.gov entity registration is approved.
    # Public/unregistered tier = 10 requests/day. Registered = 1,000/day.
    "SAM_API_KEY": os.environ.get("SAM_API_KEY", "YOUR_API_KEY_HERE"),

    # NAICS codes for your trades. Add/remove as needed.
    "NAICS_CODES": [
        "236220",  # Commercial building construction
        "236118",  # Residential remodeling
        "238210",  # Electrical contractors
        "238220",  # Plumbing/HVAC/AC
        "238160",  # Roofing
        "238910",  # Site prep/demolition/excavation
        "238320",  # Painting
        "237310",  # Highway/street/bridge
        "561730",  # Landscaping
        "238110",  # Concrete/foundation
    ],

    # Central Texas cities/counties to match against place of performance.
    # Matching is case-insensitive substring match against city name.
    "CENTRAL_TEXAS_CITIES": [
        "austin", "round rock", "georgetown", "pflugerville", "cedar park",
        "san marcos", "kyle", "buda", "leander", "killeen", "temple",
        "waco", "belton", "harker heights", "bryan", "college station",
        "bastrop", "lockhart", "marble falls", "burnet", "taylor",
        "elgin", "smithville", "giddings", "salado", "copperas cove",
    ],

    # How many days back to look each run (overlap is fine -- duplicates
    # are filtered out via the seen-notices file).
    "LOOKBACK_DAYS": 2,

    # Procurement type filter. "o" = Solicitation, "k" = Combined Synopsis/Solicitation
    # Leave as list to include both. Full list of types is in the API docs.
    "PTYPE": ["o", "k"],

    # File used to avoid re-emailing the same opportunity twice
    "SEEN_FILE": os.path.join(os.path.dirname(__file__), "seen_notices.json"),

    # Output CSV of today's matches (kept as a local record)
    "OUTPUT_DIR": os.path.join(os.path.dirname(__file__), "output"),

    # --- Email settings ---
    "SEND_EMAIL": True,
    "SMTP_HOST": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
    "SMTP_PORT": int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER": os.environ.get("SMTP_USER", "your_email@gmail.com"),
    # Use an App Password, not your real password, if using Gmail.
    "SMTP_PASS": os.environ.get("SMTP_PASS", "YOUR_APP_PASSWORD_HERE"),
    "EMAIL_TO": os.environ.get("EMAIL_TO", "your_email@gmail.com"),
}

SAM_API_URL = "https://api.sam.gov/prod/opportunities/v2/search"


# ---------------------------------------------------------------------------
# CORE LOGIC
# ---------------------------------------------------------------------------

def fetch_opportunities(naics_code, posted_from, posted_to):
    """Fetch all pages of opportunities for a single NAICS code in a date range."""
    results = []
    offset = 0
    limit = 100  # API max per page

    while True:
        params = {
            "api_key": CONFIG["SAM_API_KEY"],
            "limit": limit,
            "offset": offset,
            "postedFrom": posted_from,
            "postedTo": posted_to,
            "ncode": naics_code,
            "ptype": CONFIG["PTYPE"],
            "state": "TX",  # place of performance state
        }
        resp = requests.get(SAM_API_URL, params=params, timeout=30)

        if resp.status_code == 429:
            print(f"  Rate limit hit on NAICS {naics_code}. Stopping for today.")
            break
        resp.raise_for_status()

        data = resp.json()
        page = data.get("opportunitiesData", [])
        results.extend(page)

        total = data.get("totalRecords", 0)
        offset += limit
        if offset >= total or not page:
            break

    return results


def is_central_texas(opportunity):
    """Check if the place of performance city matches our Central Texas list."""
    pop = opportunity.get("placeOfPerformance") or {}
    city = (pop.get("city") or {}).get("name", "")
    if not city:
        return False
    city_lower = city.lower()
    return any(ct_city in city_lower for ct_city in CONFIG["CENTRAL_TEXAS_CITIES"])


def load_seen():
    if os.path.exists(CONFIG["SEEN_FILE"]):
        with open(CONFIG["SEEN_FILE"], "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(CONFIG["SEEN_FILE"], "w") as f:
        json.dump(list(seen_ids), f)


def run():
    posted_to = datetime.now()
    posted_from = posted_to - timedelta(days=CONFIG["LOOKBACK_DAYS"])
    posted_from_str = posted_from.strftime("%m/%d/%Y")
    posted_to_str = posted_to.strftime("%m/%d/%Y")

    seen_ids = load_seen()
    all_matches = []

    for naics in CONFIG["NAICS_CODES"]:
        print(f"Fetching NAICS {naics}...")
        opps = fetch_opportunities(naics, posted_from_str, posted_to_str)
        for opp in opps:
            notice_id = opp.get("noticeId")
            if notice_id in seen_ids:
                continue
            if not is_central_texas(opp):
                continue
            seen_ids.add(notice_id)
            all_matches.append(opp)

    save_seen(seen_ids)

    if not all_matches:
        print("No new Central Texas matches today.")
        return

    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    csv_path = os.path.join(CONFIG["OUTPUT_DIR"], f"matches_{today_str}.csv")
    write_csv(all_matches, csv_path)

    print(f"Found {len(all_matches)} new matches. Saved to {csv_path}")

    if CONFIG["SEND_EMAIL"]:
        send_email(all_matches, csv_path)


def write_csv(opportunities, path):
    fields = [
        "title", "noticeId", "solicitationNumber", "department",
        "naicsCode", "type", "postedDate", "responseDeadLine",
        "typeOfSetAsideDescription", "city", "uiLink",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for opp in opportunities:
            pop_city = ((opp.get("placeOfPerformance") or {}).get("city") or {}).get("name", "")
            writer.writerow({
                "title": opp.get("title", ""),
                "noticeId": opp.get("noticeId", ""),
                "solicitationNumber": opp.get("solicitationNumber", ""),
                "department": opp.get("department", ""),
                "naicsCode": opp.get("naicsCode", ""),
                "type": opp.get("type", ""),
                "postedDate": opp.get("postedDate", ""),
                "responseDeadLine": opp.get("responseDeadLine", ""),
                "typeOfSetAsideDescription": opp.get("typeOfSetAsideDescription", ""),
                "city": pop_city,
                "uiLink": opp.get("uiLink", ""),
            })


def send_email(opportunities, csv_path):
    msg = MIMEMultipart()
    msg["From"] = CONFIG["SMTP_USER"]
    msg["To"] = CONFIG["EMAIL_TO"]
    msg["Subject"] = f"SAM.gov: {len(opportunities)} new Central Texas matches ({datetime.now().strftime('%Y-%m-%d')})"

    lines = []
    for opp in opportunities:
        pop_city = ((opp.get("placeOfPerformance") or {}).get("city") or {}).get("name", "")
        lines.append(
            f"- {opp.get('title')}\n"
            f"  Dept: {opp.get('department')}\n"
            f"  NAICS: {opp.get('naicsCode')} | Set-aside: {opp.get('typeOfSetAsideDescription') or 'None'}\n"
            f"  City: {pop_city} | Deadline: {opp.get('responseDeadLine')}\n"
            f"  Link: {opp.get('uiLink')}\n"
        )
    body = "\n".join(lines)

    msg.attach(MIMEText(body, "plain"))

    with open(csv_path, "rb") as f:
        attachment = MIMEApplication(f.read(), Name=os.path.basename(csv_path))
        attachment["Content-Disposition"] = f'attachment; filename="{os.path.basename(csv_path)}"'
        msg.attach(attachment)

    with smtplib.SMTP(CONFIG["SMTP_HOST"], CONFIG["SMTP_PORT"]) as server:
        server.starttls()
        server.login(CONFIG["SMTP_USER"], CONFIG["SMTP_PASS"])
        server.send_message(msg)

    print(f"Email sent to {CONFIG['EMAIL_TO']}")


if __name__ == "__main__":
    run()


# ---------------------------------------------------------------------------
# SCHEDULING THIS SCRIPT
# ---------------------------------------------------------------------------
#
# Linux/Mac (cron) -- run every day at 7 AM:
#   crontab -e
#   Add this line:
#   0 7 * * * /usr/bin/python3 /path/to/sam_gov_monitor.py >> /path/to/log.txt 2>&1
#
# Windows (Task Scheduler):
#   1. Open Task Scheduler -> Create Basic Task
#   2. Trigger: Daily, pick a time
#   3. Action: Start a program -> python.exe -> Arguments: "C:\path\to\sam_gov_monitor.py"
#
# Cloud option (no computer needs to stay on): GitHub Actions scheduled
# workflow, or a small always-on VM/cron job. Ask if you want that set up.
# ---------------------------------------------------------------------------
