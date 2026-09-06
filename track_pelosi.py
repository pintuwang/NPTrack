"""
Nancy Pelosi stock/option trade tracker.

Data source: Periodic Transaction Reports (PTRs) filed under the STOCK Act
with the Clerk of the U.S. House of Representatives
(https://disclosures-clerk.house.gov). This is NOT SEC EDGAR -- members of
Congress are not SEC-registered insiders, so their trades are disclosed
through the House Clerk instead.

Important data limitation: PTRs disclose a *dollar-value range* for each
transaction (e.g. "$15,001 - $50,000"), never an exact price or share
count. Any "estimated price" / "estimated quantity" fields in the output
are derived (midpoint of the disclosed range, divided by a historical
closing price looked up separately) and are clearly best-effort estimates,
not reported figures.
"""

import requests
import zipfile
import io
import re
import json
import os
import time
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

DEBUG_FILE = "debug_output.txt"
TRADES_FILE = "pelosi_trades.json"
UPDATES_FILE = "pelosi_updates.json"
POSITIONS_FILE = "pelosi_positions.json"

# Members to track. Extensible: add more {"display_name", "last_name"}
# entries to track additional members of Congress.
TRACKED_MEMBERS = [
    {"display_name": "Nancy Pelosi", "last_name": "Pelosi"},
]

BASE_URL = "https://disclosures-clerk.house.gov"
ZIP_URL_TMPL = BASE_URL + "/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF_URL_TMPL = BASE_URL + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PelosiTracker/1.0; "
                  "+https://github.com/pintuwang/NPTrack)"
}

# Yahoo Finance's unauthenticated chart JSON endpoint, used only as a
# best-effort enrichment to estimate price/quantity. Never blocks core
# functionality. (Stooq's CSV endpoint was tried first but serves a
# JavaScript bot-verification challenge page to non-browser requests --
# a plain HTTP client can never pass that, so it was replaced outright.)
YAHOO_CHART_URL_TMPL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={period1}&period2={period2}&interval=1d"
)
# A generic browser UA for this specific host -- unlike the House Clerk
# requests above, self-identifying as a bot here is more likely to trigger
# exactly the kind of anti-bot challenge that broke the Stooq lookup.
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

TICKER_RE = re.compile(r"\(([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\)")
TICKER_DENYLIST = {"ST", "OP", "PTR", "MF", "CT", "OT", "PL", "REP"}
AMOUNT_RE = re.compile(
    r"(Over\s*\$[\d,]+(?:\.\d{2})?|\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?)"
)
DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")

TXN_TYPE_MAP = {
    "p": "Purchase",
    "s": "Sale (Full)",
    "s (partial)": "Sale (Partial)",
    "s(partial)": "Sale (Partial)",
    "e": "Exchange",
}


def log_debug(message):
    try:
        with open(DEBUG_FILE, "a") as f:
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}: {message}\n")
        print(message)
    except Exception as e:
        print(f"Error writing to {DEBUG_FILE}: {e}")


def init_debug_file():
    try:
        with open(DEBUG_FILE, "w") as f:
            f.write(f"Script started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
        os.chmod(DEBUG_FILE, 0o666)
    except Exception as e:
        print(f"Fatal error initializing {DEBUG_FILE}: {e}")


def load_existing_data(filename, default_factory):
    try:
        if os.path.exists(filename):
            with open(filename, "r") as f:
                return json.load(f)
        return default_factory()
    except (json.JSONDecodeError, IOError) as e:
        log_debug(f"Error loading {filename}: {e}, creating new")
        return default_factory()


def save_json_file(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.chmod(filename, 0o666)
        log_debug(f"Successfully saved {filename}")
        return True
    except Exception as e:
        log_debug(f"Error saving {filename}: {e}")
        return False


# ---------------------------------------------------------------------------
# House Clerk annual index (bulk ZIP of every filing that year)
# ---------------------------------------------------------------------------

def field(rec, *names):
    """Case-insensitive lookup across possible tag-name variants."""
    lower_map = {k.lower(): v for k, v in rec.items()}
    for name in names:
        if name.lower() in lower_map:
            return (lower_map[name.lower()] or "").strip()
    return ""


def fetch_year_index(year):
    """Download and parse the annual House financial-disclosure XML index."""
    url = ZIP_URL_TMPL.format(year=year)
    log_debug(f"Fetching filing index: {url}")
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log_debug(f"Failed to download index for {year}: {e}")
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not xml_names:
                log_debug(f"No XML file found inside {year}FD.zip. Contents: {zf.namelist()}")
                return []
            xml_bytes = zf.read(xml_names[0])
    except zipfile.BadZipFile as e:
        log_debug(f"Bad zip file for {year}: {e}")
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log_debug(f"XML parse error for {year} index: {e}")
        return []

    records = []
    for member_el in root:
        rec = {child.tag.strip(): (child.text or "").strip() for child in member_el}
        if rec:
            records.append(rec)

    log_debug(f"Parsed {len(records)} filing records for {year}")
    if records:
        log_debug(f"Sample record fields for {year}: {list(records[0].keys())}")
    return records


def filter_member_ptrs(records, last_name):
    matches = []
    for rec in records:
        last = field(rec, "Last", "LastName", "Last_Name")
        filing_type = field(rec, "FilingType", "Filing_Type")
        if last.lower() == last_name.lower() and filing_type.strip().upper() == "P":
            matches.append(rec)
    return matches


# ---------------------------------------------------------------------------
# Individual PTR PDF fetch + parse
# ---------------------------------------------------------------------------

def fetch_ptr_pdf(year, doc_id):
    url = PTR_PDF_URL_TMPL.format(year=year, doc_id=doc_id)
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.content, url


def parse_ptr_text_fallback(text, doc_id):
    """Regex-based fallback when table extraction finds nothing (e.g. an
    image-based PDF, or a layout pdfplumber can't detect as a table)."""
    rows = []
    for line in text.splitlines():
        amount_match = AMOUNT_RE.search(line)
        if not amount_match:
            continue
        dates = DATE_RE.findall(line)
        type_match = re.search(r"\b(P|S \(partial\)|S|E)\b", line)
        rows.append({
            "owner_raw": "",
            "asset_raw": line[:amount_match.start()].strip(),
            "type_raw": type_match.group(1) if type_match else "",
            "date_raw": dates[0] if dates else "",
            "notification_date_raw": dates[1] if len(dates) > 1 else "",
            "amount_raw": amount_match.group(1),
        })
    return rows


def parse_ptr_pdf(pdf_bytes, doc_id):
    if pdfplumber is None:
        log_debug("pdfplumber not installed; cannot parse PTR PDFs")
        return []

    raw_rows = []
    text_pages = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header = [(c or "").strip().lower() for c in table[0]]
                    col = {}
                    for i, h in enumerate(header):
                        if "asset" in h:
                            col.setdefault("asset", i)
                        elif "owner" in h:
                            col.setdefault("owner", i)
                        elif "transaction" in h and "type" in h:
                            col.setdefault("type", i)
                        elif "notification" in h:
                            col.setdefault("notif_date", i)
                        elif h.strip() == "date":
                            col.setdefault("date", i)
                        elif "amount" in h:
                            col.setdefault("amount", i)
                    if "asset" not in col or "amount" not in col:
                        continue  # not the transactions table on this page
                    log_debug(f"Doc {doc_id}: table header={header!r} col_map={col!r}")
                    if table[1:]:
                        log_debug(f"Doc {doc_id}: first data row raw={table[1]!r}")
                    for row in table[1:]:
                        row = [(c or "").strip().replace("\n", " ") for c in row]
                        if len(row) <= col["asset"] or not row[col["asset"]]:
                            continue
                        raw_rows.append({
                            "owner_raw": row[col["owner"]] if "owner" in col and col["owner"] < len(row) else "",
                            "asset_raw": row[col["asset"]],
                            "type_raw": row[col["type"]] if "type" in col and col["type"] < len(row) else "",
                            "date_raw": row[col["date"]] if "date" in col and col["date"] < len(row) else "",
                            "notification_date_raw": row[col["notif_date"]] if "notif_date" in col and col["notif_date"] < len(row) else "",
                            "amount_raw": row[col["amount"]] if "amount" in col and col["amount"] < len(row) else "",
                        })
                text_pages.append(page.extract_text() or "")
    except Exception as e:
        log_debug(f"Error opening/parsing PDF for doc {doc_id}: {e}")
        return []

    if not raw_rows:
        log_debug(f"No table rows extracted for doc {doc_id}; trying text fallback")
        raw_rows = parse_ptr_text_fallback("\n".join(text_pages), doc_id)

    if not raw_rows:
        log_debug(f"No transactions extracted from doc {doc_id} (table or text)")

    for row in raw_rows:
        row["doc_id"] = doc_id
    return raw_rows


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def extract_ticker(asset_raw):
    for m in TICKER_RE.finditer(asset_raw or ""):
        candidate = m.group(1)
        if candidate not in TICKER_DENYLIST:
            return candidate
    return None


def classify_asset_type(asset_raw):
    lower = (asset_raw or "").lower()
    if "call option" in lower or "put option" in lower or re.search(r"\boption\b", lower):
        return "Stock Option"
    return "Stock"


def normalize_txn_type(type_raw):
    key = (type_raw or "").strip().lower()
    return TXN_TYPE_MAP.get(key, (type_raw or "").strip() or "Unknown")


def parse_amount_range(amount_raw):
    amount_raw = (amount_raw or "").replace("\n", " ").strip()
    if not amount_raw:
        return None, None
    if amount_raw.lower().startswith("over"):
        nums = re.findall(r"[\d,]+", amount_raw)
        low = float(nums[0].replace(",", "")) if nums else None
        return low, None
    nums = re.findall(r"[\d,]+(?:\.\d{2})?", amount_raw)
    if len(nums) >= 2:
        return float(nums[0].replace(",", "")), float(nums[1].replace(",", ""))
    if len(nums) == 1:
        # Not a bracketed range -- an exact disclosed amount (e.g. a small
        # cash-in-lieu payment from a spinoff). Treat as low == high so
        # est_value_usd comes out exact instead of silently null.
        exact = float(nums[0].replace(",", ""))
        return exact, exact
    return None, None


def parse_mmddyyyy(date_str):
    date_str = (date_str or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_transaction(raw_row, member_name, filing_year, filing_url):
    amount_low, amount_high = parse_amount_range(raw_row.get("amount_raw"))
    est_value = None
    if amount_low is not None and amount_high is not None:
        est_value = (amount_low + amount_high) / 2
    elif amount_low is not None:
        est_value = amount_low  # open-ended "Over $X" range

    return {
        "member": member_name,
        "doc_id": raw_row.get("doc_id"),
        "filing_year": filing_year,
        "filing_url": filing_url,
        "owner": raw_row.get("owner_raw") or "Self",
        "asset_description": raw_row.get("asset_raw"),
        "ticker": extract_ticker(raw_row.get("asset_raw")),
        "asset_type": classify_asset_type(raw_row.get("asset_raw")),
        "trade_type": normalize_txn_type(raw_row.get("type_raw")),
        "transaction_date": parse_mmddyyyy(raw_row.get("date_raw")),
        "notification_date": parse_mmddyyyy(raw_row.get("notification_date_raw")),
        "amount_range": (raw_row.get("amount_raw") or "").strip(),
        "amount_range_low": amount_low,
        "amount_range_high": amount_high,
        "est_value_usd": est_value,
        # Best-effort only -- PTRs never disclose exact price or share count.
        "est_price_usd": None,
        "est_quantity": None,
        # "Reported date" analysis: how the stock has moved from the price
        # when the trade became public (notification_date) to today -- see
        # enrich_with_report_date_analysis. Also best-effort.
        "est_price_on_report_usd": None,
        "current_price_usd": None,
        "pl_since_report_pct": None,
    }


# ---------------------------------------------------------------------------
# Optional price/quantity estimation (best-effort, never blocks the pipeline)
# ---------------------------------------------------------------------------

def fetch_close_price(ticker, on_date_iso):
    """Look up a closing price near on_date_iso via Yahoo Finance's
    unauthenticated chart JSON endpoint. Returns None on any failure --
    this is pure enrichment, never allowed to block the pipeline."""
    try:
        target = datetime.strptime(on_date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

    period1 = int((target - timedelta(days=10)).timestamp())
    period2 = int((target + timedelta(days=1)).timestamp())
    url = YAHOO_CHART_URL_TMPL.format(symbol=ticker.upper(), period1=period1, period2=period2)

    try:
        resp = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code != 200 or "json" not in content_type.lower():
            log_debug(
                f"Price lookup for {ticker} near {on_date_iso}: unexpected response "
                f"(status={resp.status_code}, content-type={content_type}, "
                f"first 200 chars={resp.text[:200]!r})"
            )
            return None

        data = resp.json()
        results = ((data or {}).get("chart") or {}).get("result") or []
        if not results:
            chart_error = ((data or {}).get("chart") or {}).get("error")
            log_debug(f"Price lookup for {ticker} near {on_date_iso}: no chart result (error={chart_error})")
            return None

        timestamps = results[0].get("timestamp") or []
        quote = ((results[0].get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        if not timestamps or not closes:
            log_debug(f"Price lookup for {ticker} near {on_date_iso}: empty timestamp/close series")
            return None

        # Latest close on/before the target date; else earliest available.
        best_price, best_date = None, None
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            row_date = datetime.fromtimestamp(ts, tz=timezone.utc)
            if row_date <= target and (best_date is None or row_date > best_date):
                best_date, best_price = row_date, close
        if best_price is None:
            best_price = next((c for c in closes if c is not None), None)

        if best_price is None:
            log_debug(f"Price lookup for {ticker} near {on_date_iso}: no non-null close in series")
            return None
        return float(best_price)
    except Exception as e:
        log_debug(f"Price lookup failed for {ticker} near {on_date_iso}: {e}")
        return None


def enrich_with_price_estimates(transactions, max_lookups=40):
    """Fill in est_price_usd / est_quantity for stock (non-option) trades
    with a known ticker, transaction date, and disclosed value. Capped and
    best-effort: any failure just leaves the fields null."""
    lookups_done = 0
    consecutive_failures = 0
    price_cache = {}

    for txn in transactions:
        if lookups_done >= max_lookups or consecutive_failures >= 5:
            break
        if txn["asset_type"] != "Stock":
            continue
        if txn.get("est_price_usd") is not None:
            continue  # already resolved in an earlier run
        if not txn["ticker"] or not txn["transaction_date"] or txn["est_value_usd"] is None:
            continue

        cache_key = (txn["ticker"], txn["transaction_date"])
        if cache_key in price_cache:
            price = price_cache[cache_key]
        else:
            price = fetch_close_price(txn["ticker"], txn["transaction_date"])
            price_cache[cache_key] = price
            lookups_done += 1
            time.sleep(0.5)

        if price:
            consecutive_failures = 0
            txn["est_price_usd"] = round(price, 2)
            txn["est_quantity"] = round(txn["est_value_usd"] / price)
        else:
            consecutive_failures += 1

    if consecutive_failures >= 5:
        log_debug("Stopping price enrichment early after repeated failures (endpoint likely unreachable)")


def fetch_latest_close(ticker):
    """Most recent available closing price for `ticker`, best-effort."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    return fetch_close_price(ticker, today_iso)


# ---------------------------------------------------------------------------
# Position summary: FIFO-match Purchases against Sales/Exchanges per ticker
# to separate still-open lots (-> running P/L) from closed lots (-> realized,
# annualized P/L). Two extra layers of estimation stack on top of the
# est_price_usd / est_quantity fields here (themselves already estimates),
# so treat every number in this table as directional, not precise.
# ---------------------------------------------------------------------------

def compute_positions(all_trades):
    from collections import defaultdict, deque

    by_ticker = defaultdict(list)
    for t in all_trades:
        if t.get("asset_type") != "Stock":
            continue  # options are excluded: no reliable price series here
        if not t.get("ticker") or not t.get("transaction_date"):
            continue
        by_ticker[t["ticker"]].append(t)

    open_positions = []
    closed_positions = []

    for ticker, txns in by_ticker.items():
        txns_sorted = sorted(txns, key=lambda t: (t["transaction_date"], t.get("doc_id") or ""))
        lots = deque()  # each: {"qty", "price", "date"}
        data_incomplete = False
        unattributed_sale_qty = 0.0

        for t in txns_sorted:
            qty = t.get("est_quantity")
            price = t.get("est_price_usd")
            ttype = (t.get("trade_type") or "").lower()

            if ttype.startswith("purchase"):
                if qty and price:
                    lots.append({"qty": qty, "price": price, "date": t["transaction_date"]})
                else:
                    data_incomplete = True
                continue

            if ttype.startswith("sale") or ttype.startswith("exchange"):
                if not qty or not price:
                    data_incomplete = True
                    continue
                remaining = qty
                sell_date = datetime.strptime(t["transaction_date"], "%Y-%m-%d")
                while remaining > 0 and lots:
                    lot = lots[0]
                    matched_qty = min(lot["qty"], remaining)
                    buy_date = datetime.strptime(lot["date"], "%Y-%m-%d")
                    days_held = max((sell_date - buy_date).days, 0)
                    pct = (price - lot["price"]) / lot["price"] if lot["price"] else None
                    annualized_pct = None
                    if pct is not None and days_held > 0:
                        try:
                            annualized_pct = (1 + pct) ** (365.0 / days_held) - 1
                        except (OverflowError, ValueError):
                            annualized_pct = None
                    closed_positions.append({
                        "ticker": ticker,
                        "quantity": round(matched_qty, 4),
                        "buy_date": lot["date"],
                        "buy_price_usd": round(lot["price"], 2),
                        "sell_date": t["transaction_date"],
                        "sell_price_usd": round(price, 2),
                        "days_held": days_held,
                        "realized_pl_usd": round((price - lot["price"]) * matched_qty, 2),
                        "realized_pl_pct": round(pct * 100, 2) if pct is not None else None,
                        "annualized_pl_pct": round(annualized_pct * 100, 2) if annualized_pct is not None else None,
                    })
                    lot["qty"] -= matched_qty
                    remaining -= matched_qty
                    if lot["qty"] <= 1e-6:
                        lots.popleft()
                if remaining > 1e-6:
                    # Sold more than we have a matching purchase lot for --
                    # most likely a position she already held before this
                    # tracker's history begins (PTRs only cover trades made
                    # after the fact, not pre-existing holdings). We can't
                    # compute a cost basis or P/L for shares we never saw
                    # purchased, so surface the gap rather than guessing.
                    unattributed_sale_qty += remaining
                    data_incomplete = True

        if lots:
            total_qty = sum(l["qty"] for l in lots)
            total_cost = sum(l["qty"] * l["price"] for l in lots)
            avg_cost = total_cost / total_qty if total_qty else None
            oldest_date = min(l["date"] for l in lots)
            open_positions.append({
                "ticker": ticker,
                "quantity": round(total_qty, 4),
                "avg_cost_usd": round(avg_cost, 2) if avg_cost is not None else None,
                "held_since": oldest_date,
                "current_price_usd": None,
                "running_pl_usd": None,
                "running_pl_pct": None,
                "unattributed_sale_qty": round(unattributed_sale_qty, 4),
                "data_incomplete": data_incomplete,
            })
        elif unattributed_sale_qty > 1e-6:
            # Sold shares in this ticker with no purchase lot in our history
            # at all -- e.g. a long-held position sold this year. No cost
            # basis, so no P/L is computable; still show the ticker rather
            # than dropping the sale silently.
            open_positions.append({
                "ticker": ticker,
                "quantity": 0,
                "avg_cost_usd": None,
                "held_since": None,
                "current_price_usd": None,
                "running_pl_usd": None,
                "running_pl_pct": None,
                "unattributed_sale_qty": round(unattributed_sale_qty, 4),
                "data_incomplete": True,
            })

    return open_positions, closed_positions


def get_current_price(ticker, price_cache, budget):
    """Shared helper: look up (and cache, within this run) a ticker's latest
    price. `budget` is a mutable {"lookups": int, "failures": int} dict so
    callers can share one rate/failure budget across the whole run."""
    if ticker in price_cache:
        return price_cache[ticker]
    if budget["lookups"] >= budget["max_lookups"] or budget["failures"] >= 5:
        return None

    price = fetch_latest_close(ticker)
    price_cache[ticker] = price
    budget["lookups"] += 1
    time.sleep(0.5)
    budget["failures"] = 0 if price else budget["failures"] + 1
    return price


def enrich_open_positions(open_positions, price_cache, budget):
    """Best-effort: fetch a current price for each open ticker so we can
    show a running (unrealized) P/L. Failure just leaves fields null."""
    for pos in open_positions:
        if pos["avg_cost_usd"] is None:
            continue

        price = get_current_price(pos["ticker"], price_cache, budget)
        if price:
            pos["current_price_usd"] = round(price, 2)
            pos["running_pl_usd"] = round((price - pos["avg_cost_usd"]) * pos["quantity"], 2)
            pos["running_pl_pct"] = round((price - pos["avg_cost_usd"]) / pos["avg_cost_usd"] * 100, 2)

    if budget["failures"] >= 5:
        log_debug("Stopping current-price lookups early after repeated failures")


# ---------------------------------------------------------------------------
# Per-transaction "reported date" analysis: a retail investor can only ever
# have known about a trade once it was *reported* (notification_date), not
# on the actual transaction_date -- STOCK Act filings can lag by weeks. This
# shows how the stock has moved from the price when it was reported to now,
# as a rough read on whether following the disclosure would still have paid
# off. Directional framing (does a rising price help or hurt) is left to the
# UI, since it depends on trade_type (Purchase vs. Sale).
# ---------------------------------------------------------------------------

def enrich_with_report_date_analysis(transactions, price_cache, budget, report_price_cache=None):
    """Fill in est_price_on_report_usd / current_price_usd / pl_since_report_pct
    for every stock transaction with a resolvable ticker and notification
    date. Best-effort and capped, like the other enrichment passes."""
    if report_price_cache is None:
        report_price_cache = {}

    for txn in transactions:
        if txn["asset_type"] != "Stock" or not txn["ticker"]:
            continue

        if txn.get("est_price_on_report_usd") is None and txn.get("notification_date"):
            if budget["lookups"] < budget["max_lookups"] and budget["failures"] < 5:
                key = (txn["ticker"], txn["notification_date"])
                if key not in report_price_cache:
                    report_price_cache[key] = fetch_close_price(txn["ticker"], txn["notification_date"])
                    budget["lookups"] += 1
                    time.sleep(0.5)
                    budget["failures"] = 0 if report_price_cache[key] else budget["failures"] + 1
                report_price = report_price_cache[key]
                if report_price:
                    txn["est_price_on_report_usd"] = round(report_price, 2)

        current_price = get_current_price(txn["ticker"], price_cache, budget)
        if current_price:
            txn["current_price_usd"] = round(current_price, 2)

        report_price = txn.get("est_price_on_report_usd")
        cur_price = txn.get("current_price_usd")
        if report_price and cur_price:
            txn["pl_since_report_pct"] = round((cur_price - report_price) / report_price * 100, 2)
        else:
            txn["pl_since_report_pct"] = txn.get("pl_since_report_pct")

    if budget["failures"] >= 5:
        log_debug("Stopping report-date price lookups early after repeated failures")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def years_to_check():
    now = datetime.now(timezone.utc)
    years = [now.year]
    if now.month <= 2:
        years.append(now.year - 1)  # catch late-filed prior-year PTRs
    return years


def main():
    init_debug_file()

    all_trades = load_existing_data(TRADES_FILE, lambda: [])
    known_txn_keys = {
        (t.get("doc_id"), t.get("asset_description"), t.get("transaction_date"), t.get("amount_range"))
        for t in all_trades
    }

    new_trades = []

    for member in TRACKED_MEMBERS:
        log_debug(f"\n{'=' * 50}\nChecking filings for {member['display_name']}")

        for year in years_to_check():
            index_records = fetch_year_index(year)
            if not index_records:
                continue

            member_filings = filter_member_ptrs(index_records, member["last_name"])
            log_debug(f"Found {len(member_filings)} PTR filing(s) for {member['display_name']} in {year}")

            for rec in member_filings:
                doc_id = field(rec, "DocID", "Doc_ID")
                if not doc_id:
                    continue

                try:
                    pdf_bytes, filing_url = fetch_ptr_pdf(year, doc_id)
                except requests.RequestException as e:
                    log_debug(f"Failed to download PTR PDF {doc_id}: {e}")
                    continue

                raw_rows = parse_ptr_pdf(pdf_bytes, doc_id)
                for raw_row in raw_rows:
                    txn = normalize_transaction(raw_row, member["display_name"], year, filing_url)
                    # PTR PDFs often carry a free-text "Comment/Description"
                    # footnote section right below the transactions table,
                    # which pdfplumber's grid detection can merge in as more
                    # rows of the same table. A real transaction row always
                    # has both a parsed date and a disclosed amount; anything
                    # missing either is that footnote noise, not a trade.
                    if not txn["transaction_date"] or not txn["amount_range"]:
                        log_debug(f"Dropping non-transaction row for doc {doc_id}: {txn['asset_description']!r}")
                        continue
                    key = (txn["doc_id"], txn["asset_description"], txn["transaction_date"], txn["amount_range"])
                    if key in known_txn_keys:
                        continue
                    known_txn_keys.add(key)
                    new_trades.append(txn)

                time.sleep(1)  # be polite to the House Clerk server

    all_trades.extend(new_trades)

    # Enrich every stock trade still missing a price estimate, not just
    # this run's new ones -- covers backfilling trades saved during an
    # earlier run when the price lookup endpoint was failing.
    enrich_with_price_estimates(all_trades)

    # Shared current-price cache/budget across both the per-transaction
    # "reported date" analysis and the position summary's current prices --
    # they very often want the same ticker, so this halves the redundant
    # Yahoo Finance calls within a single run.
    current_price_cache = {}
    price_budget = {"lookups": 0, "max_lookups": 60, "failures": 0}
    enrich_with_report_date_analysis(all_trades, current_price_cache, price_budget)

    all_trades.sort(key=lambda t: t.get("transaction_date") or "", reverse=True)

    now_utc = datetime.now(timezone.utc)
    tz_plus_8 = timezone(timedelta(hours=8))
    last_updated = now_utc.astimezone(tz_plus_8).strftime("%Y-%m-%d %H:%M:%S %Z")

    updates_payload = {
        "last_updated": last_updated,
        "new_trade_count": len(new_trades),
        "new_trades": new_trades,
    }

    open_positions, closed_positions = compute_positions(all_trades)
    enrich_open_positions(open_positions, current_price_cache, price_budget)
    open_positions.sort(key=lambda p: p["ticker"])
    closed_positions.sort(key=lambda p: p["sell_date"], reverse=True)

    positions_payload = {
        "last_updated": last_updated,
        "open_positions": open_positions,
        "closed_positions": closed_positions,
    }

    success_trades = save_json_file(TRADES_FILE, all_trades)
    success_updates = save_json_file(UPDATES_FILE, updates_payload)
    success_positions = save_json_file(POSITIONS_FILE, positions_payload)

    if success_trades and success_updates and success_positions:
        log_debug("\n=== SUCCESS: All files saved successfully ===")
    else:
        log_debug("\n=== ERROR: Some files failed to save ===")

    log_debug(f"\n=== FINAL SUMMARY ===")
    log_debug(f"New transactions found this run: {len(new_trades)}")
    log_debug(f"Total transactions tracked: {len(all_trades)}")
    log_debug(f"Open positions: {len(open_positions)}, closed lots: {len(closed_positions)}")


if __name__ == "__main__":
    main()
