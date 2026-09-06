# Nancy Pelosi Stock Tracker

Tracks Nancy Pelosi's stock and stock-option transactions and publishes them
as a simple table (`index.html`), updated daily by a scheduled GitHub Action.

## Data source

Transactions come from **Periodic Transaction Reports (PTRs)** filed with
the **Clerk of the U.S. House of Representatives**
(<https://disclosures-clerk.house.gov>) under the STOCK Act.

This is **not SEC EDGAR**. Members of Congress are not SEC-registered
corporate insiders, so their trades are not Form 4 / 13D-13G filings — they
go through the House (or Senate) financial disclosure system instead. The
pipeline downloads the House Clerk's official annual filing index, finds
PTRs filed under the last name "Pelosi", and parses the linked PTR PDF for
each one.

## Known data limitations

* **No exact price or share count is ever disclosed.** PTRs report a
  transaction as a **dollar-value range** (e.g. `$15,001 - $50,000`), not an
  exact amount, price, or quantity.
* `est_value_usd` is simply the midpoint of that disclosed range.
* `est_price_usd` / `est_quantity` are a **derived best-effort estimate**:
  a historical closing price near the transaction date is looked up (via
  Yahoo Finance's unauthenticated chart JSON endpoint) and used to back into
  an estimated share count (`est_value_usd / est_price_usd`). This lookup
  only runs for plain stock trades with a resolvable ticker, is capped per
  run, and fails silently (leaving the fields `null`) rather than guessing.
  (An earlier version used Stooq's CSV endpoint, but it serves a
  JavaScript bot-verification page to non-browser requests, which a
  server-side script can never pass -- confirmed via a live run's debug
  log, not assumed.)
* Options transactions are flagged with `asset_type: "Stock Option"` but do
  not get a price/quantity estimate, since option pricing isn't available
  from a simple daily-close feed.
* Always check `filing_url` (the underlying PDF) before treating any number
  here as authoritative.

## Position summary (open P/L and closed annualized P/L)

Every run also builds `pelosi_positions.json` by FIFO-matching each ticker's
Purchases against its later Sales/Exchanges (stock only — options are
excluded, since there's no simple daily price series to value them against):

* **Open positions** (shares bought but not yet sold): a running,
  unrealized `P/L (Running)` vs. today's looked-up closing price.
* **Closed lots** (a matched buy → sell pair): a `% P/L (Annualized)`,
  computed CAGR-style as `(1 + realized_return) ** (365 / days_held) - 1`.
* **Unmatched sales**: a sale with no purchase lot anywhere in our tracked
  history to match it against -- most likely a position she already held
  *before* this tracker started recording (PTRs only disclose new trades,
  not pre-existing holdings). There's no cost basis for shares we never saw
  bought, so no P/L can be computed; the ticker still shows up (quantity 0,
  `unattributed_sale_qty` set) rather than the sale silently disappearing.

**Read this before trusting the numbers**: this stacks a *third* layer of
estimation on top of the two already described above (range midpoint →
looked-up price → FIFO-matched P/L). A short holding period will also
produce mathematically-correct but extreme-looking annualized figures (e.g.
a small loss over 4 days can annualize to close to −100%) — always check
the `days_held` / underlying dollar amount alongside the percentage.
Positions missing a price/quantity estimate anywhere in their history are
flagged `data_incomplete: true` (shown with a `*` in the UI) and may
understate or omit lots entirely.

## "Reported" date: could you actually have followed this trade?

A retail investor never sees a trade on its `transaction_date` -- only once
the PTR document is actually filed with the Clerk. That filing can lag the
real trade by weeks. So every transaction also carries:

* `filing_date` -- when the PTR document was submitted, from the House
  Clerk's own filing index (one value per filing, applied to every
  transaction in it). This is what "Reported" means in the UI, and the
  earliest point the trade was public record.
* `notification_date` -- a different field, pulled from inside the PTR PDF
  itself: the date the *filer* was told about the transaction (e.g. by
  their broker). Per the House Ethics instruction guide, for a
  self-executed trade this is usually the same as `transaction_date` --
  it is **not** a public-disclosure date, and is only shown as secondary
  context in the tooltip when it differs from `filing_date`. (An earlier
  version of this feature used `notification_date` as "Reported," which
  was wrong for exactly this reason.)
* `est_price_on_report_usd` -- looked-up closing price on `filing_date`.
* `current_price_usd` -- latest looked-up closing price.
* `pl_since_report_pct` -- the move between those two, i.e. roughly "if you
  acted the moment this became public, how would that have gone so far."

This is framed as "how has the stock moved," not a claim about what
following a Sale specifically would net you (that would require assuming
you'd short the stock, which this tool doesn't presume) -- the UI shows the
raw price change either way and lets you judge it in context of the trade
type. Same caveats as everywhere else here: derived from a looked-up price,
best-effort, and left blank when a lookup fails rather than guessed.

## Known parser gap: some transactions can be silently lost

pdfplumber's table-cell splitting occasionally fails for a specific row in
a PTR's transaction table (observed: the row right after the header, and
rows whose asset/amount text wraps across a column boundary) -- when that
happens, the whole row's text lands in one cell with every other cell
empty, and since there's no reliable way to un-scramble the column order
from a blob like that, the row is dropped rather than guessed at. If the
dropped blob still looks like a real transaction (a date and a dollar
amount are both present in it), `track_pelosi.py` logs it as **"likely
LOSING a real transaction"** in `debug_output.txt` so it's a visible gap,
not a silent one -- check that log periodically. This is a real, confirmed
limitation (found via a live run, not hypothetical), not yet fixed.

## Files

* `track_pelosi.py` — fetches the House Clerk index + PTR PDFs, parses new
  transactions, and writes `pelosi_trades.json` (cumulative transactions),
  `pelosi_updates.json` (this run's new transactions), and
  `pelosi_positions.json` (the open/closed position summary above).
* `index.html` — static page rendering all three files.
* `.github/workflows/daily-update.yml` — runs the tracker daily and commits
  any new data.

## Tracking more members

`TRACKED_MEMBERS` in `track_pelosi.py` is a list — add another
`{"display_name": ..., "last_name": ...}` entry to track additional House
members the same way.

## A note on parsing reliability

The House Clerk's PTR PDFs are parsed with `pdfplumber`'s table extraction
(falling back to text regex if no table is detected). The government
endpoints used here (`disclosures-clerk.house.gov`) could not be reached
for live testing from the environment this was originally built in, so the
first real scheduled/manual run should be checked against the
`debug_output.txt` artifact it uploads — if the PDF layout doesn't match
what the parser expects, that log will show raw extracted rows to debug
against.
