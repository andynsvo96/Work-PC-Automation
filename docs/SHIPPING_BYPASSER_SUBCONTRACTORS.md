# Shipping Bypasser subcontractors

Shipping Bypasser supports the 45 additional subcontractors whose CRM names and
SanMar saved destinations were reviewed together. The explicit aliases and full
shipping addresses are in
[`shipping_bypasser_subcontractors.py`](../shipping_bypasser_subcontractors.py).
Names are matched without case or punctuation differences, with an optional
matching state suffix such as `(NJ)`. Unknown names, wrong state suffixes, and
ambiguous destinations require review; the worker never guesses a similar vendor.

The reviewed SanMar addresses are authoritative for these two CRM discrepancies:

| CRM subcontractor | CRM location | Approved SanMar destination |
|---|---|---|
| SEW NICE EMBROIDERY & GIFTS | Alabaster, AL 35007 | Sew Nice, 1390 County Road 12, Odenville, AL 35120-3521 |
| PRINT WORKS | Paris, TX 75462 | Print Works, 6955 Lamar Rd, Reno, TX 75462-7113 |

Other significant aliases include Central Apollo → APOLLO - IL, SOUTH APOLLO →
Apollo Dallas, DESIGNER T'S → Designer Ts Inc, DESTROYER GRAPHICS LLC → Destroyer
Graphicks, TFORMATION → T Formation of Tallahassee, and Think Ink → Think Ink and
Thread LLC. CRM subcontractors without a reviewed SanMar destination and the 13
SanMar-only addresses from the supplied snapshot remain unmapped.

## Latest ship deadline

For these additional subcontractors, the newest **matching saved Production
Note** supplies the deadline. A newer unrelated approval note does not replace
it. Examples accepted include `LATEST SHIP DATE 12/2` and
`LATEST SHIP DATE - 12/3***`; capitalization and separator variations are allowed.
Sales Notes, Art Notes, hidden notes, and unsaved drafts do not supply deadlines.

Dates without a year use their note's year. A December note naming a January
deadline uses the following year: a December 28, 2026 note with `1/3` means
January 3, 2027. Missing or unreadable timestamps, invalid dates, conflicting
dates at the newest matching timestamp, and dates preceding their own note stop
the affected stock tab for review. A malformed newer note never falls back to
an older valid deadline.

Both the UPS ETA and the planned production date must be **strictly before**
the latest ship date. Arrival and production can share a date. Production uses
the existing Monday-through-Friday calculation and is moved later only when
stock arrival requires it. An existing production date already on or after the
deadline requires review. The latest ETA across all selected warehouses controls
a split shipment; an unreadable ETA for any selected warehouse prevents purchase.
Freight-calculation errors cannot bypass the deadline check.

The actual CRM **Due Date is never changed** for these subcontractors, including
free-shipping orders. A production-date edit checks the Due Date before Save
Order and verifies it again after reload. The worker also reloads saved CRM
fields immediately before submission and stops if the destination, deadline, or
Due Date changed, or saved production cannot meet the arrival/deadline checks.
Dry runs propose date adjustments without saving CRM or submitting SanMar orders.

Existing in-house and Mach6 date policies retain their own deadlines and routing.

## Purchasing and partial results

All additional subcontractor stock ships directly to the reviewed SanMar saved
address by **UPS**, including NJ stock. Pickup and in-house NJ/ADD routing are
unavailable. Regional warehouse preferences, existing stock buffers and manual
overrides, closed-warehouse checks, exact product/color/size/quantity cart checks,
same-PO split shipments, and purchase/recovery receipts remain in use. The saved
address's name, street, suite, city, state, and ZIP must match the reviewed entry,
and the shipping address must be confirmed again on the review page. A billing
address cannot establish the shipping destination.

Review verification recognizes SanMar's **Shipping Information** section on
`/checkout/reviewandsubmit`, as well as Ship To / Shipping Address headings.
The full reviewed destination must still match within that shipping section;
correct billing details cannot compensate for a different shipping address.
This review-layout correction loads with the next worker run and needs no app
restart, extension reload, or CRM page refresh.

Tabs run independently, as they do for Mach6. A stock or timing issue skips that
tab and permits other eligible tabs to continue after the usual cart cleanup.
Confirmed purchases are preserved and are not repeated on Retry. Shared-cart
cleanup failures and uncertain submissions retain their existing review stops.
The Queue/report retains each tab's result. A failed tab now also triggers the
existing desktop notification even when other purchases make the overall run a
partial success; the alert includes the order, tab/PO label, and reason. This
notification correction also applies to Mach6.

## FREE and ALL automation reports

After manual testing, **Automate Processing > FREE / ALL** also supports
**Bypasser** and **Push Back**. Each tool uses its own configured report;
FREE and ALL do not fall back to a Rush report when a link is missing.
Selections remain independent for each mode and are preserved in normal,
scheduled, and repeating queue entries. Neither tool is enabled automatically
by this update, and installing it does not start a run or create a schedule.

Every new run scans its selected report again. Earlier failures do not create
an order ignore list, and Shipping Bypasser no longer uses Stock Tools history
to exclude entire orders with successful or unfinished tabs. Attempted-order
sets apply only to refresh passes within the current run. Push Back reads all
loaded report rows before filtering and limiting eligible orders. A Rush run
and a FREE run still use different report filters; a failed Rush order must
also match the FREE report to be detected there. Keep history and receipts:
per-tab confirmed-purchase checks and uncertain-submission review stops remain
in force when an order is reconsidered.

The four additional machine-local settings are:

| Mode | Tool | Setting | Configured report filters |
|---|---|---|---|
| FREE | Bypasser | `CRM_SHIPPING_BYPASS_FREE_URL` | Report 967, shipping charge at most 1, Sales Notes `too expensive` |
| FREE | Push Back | `CRM_PUSH_BACK_FREE_URL` | Report 967, shipping charge at most 1, Sales Notes `unable to be delivered on time` |
| ALL | Bypasser | `CRM_SHIPPING_BYPASS_ALL_URL` | Report 122, no shipping-charge limit, Sales Notes `too expensive` |
| ALL | Push Back | `CRM_PUSH_BACK_ALL_URL` | Report 122, no shipping-charge limit, Sales Notes `unable to be delivered on time` |

All four supplied reports retain their approval Production Notes filter,
unordered stock, and quantity range 1–100. The supplied production-date lower
bounds are November 22, 2022 for FREE and November 24, 2026 for ALL; they are
fixed report filters, not daily rolling dates. Store these URLs in local
`config.py`, without session/CSRF tokens. Other computers need their own local
configuration and authenticated CRM browser session.

The selected **Unlocker**, **Order Goods**, **Bypasser**, and **Push Back**
buttons are purple in FREE and ALL, in both color themes, to identify their
subcontractor reports. Unselected buttons retain the neutral style. The existing
Unlocker and Order Goods report links and submission behavior are preserved.

Push Back checks a reviewed subcontractor's newest saved **LATEST SHIP DATE**
before changing production or ordering stock. It permits the business day before
that deadline, and stops the affected order if the next production date would be
on or after it. This also bounds retries after CRM reports no purchase plan.
The actual Due Date is checked before and after the production save and remains
unchanged. A changed destination, deadline, or Due Date stops for review before
stock ordering. Its existing SanMar fallback uses the shared Shipping Bypasser
ETA, address, UPS, cart, and purchase-receipt checks.

Restart the local Automation app and refresh the dashboard to load the new
options and colors. Do not restart during active automation. No extension reload,
CRM page refresh, or new host permission is needed. **Manual Process > Shipping
Bypasser** in the extension continues to queue only the selected order and keeps
its existing explicit manual stock-buffer override. Reorder Stock retains its
in-house destination restriction.

## Local verification

Use the virtual environment and isolated fixtures; do not run live purchasing as
a regression check:

```powershell
python -m unittest discover -s tests -p test_shipping_bypasser_subcontractors.py
python -m unittest discover -s tests -p test_crm_shipping_bypasser.py
python -m unittest discover -s tests -p test_sanmar_nj_orders.py
python -m unittest discover -s tests -p test_crm_processing_subcontractors.py
python -m unittest discover -s tests -p test_push_back_subcontractors.py
node --test tests/test_processing_subcontractor_modes.mjs
```

The optional `test_sanmar_nj_browser.py` suite uses a local ChromeDriver selected
by `SANMAR_FIXTURE_CHROMEDRIVER`. It verifies all 45 saved-address selections,
changed/duplicate-address rejection, UPS/NJ fulfillment, full shipping-address
review, newest saved Production Notes, and a persisted production-date edit that
preserves the Due Date. It uses an isolated ignored runtime profile and opens
only local fixtures. Run the full Python suite for server/config changes and
the Node regressions for dashboard changes. Browser verification has been run
on Windows; macOS has not been exercised for these new report options.
