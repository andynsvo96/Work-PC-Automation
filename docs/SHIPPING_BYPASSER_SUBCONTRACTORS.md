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

## Initial testing and loading the change

Restart the local Automation app to load the notification correction. The next
worker loads the new mappings and checks. No extension reload, CRM page refresh,
new host permission, or new report/list configuration is needed for this worker
change. Do not restart the app during active automation.

For initial user testing, open the intended CRM order and choose **Manual
Process > Shipping Bypasser** in the Chrome extension. This queues only that
order and retains the existing explicit manual stock-buffer override. No new
automatic list or schedule is configured by this change; a dedicated list can be
connected after manual testing. Reorder Stock retains its in-house destination
restriction.

## Local verification

Use the virtual environment and isolated fixtures; do not run live purchasing as
a regression check:

```powershell
python -m unittest discover -s tests -p test_shipping_bypasser_subcontractors.py
python -m unittest discover -s tests -p test_crm_shipping_bypasser.py
python -m unittest discover -s tests -p test_sanmar_nj_orders.py
```

The optional `test_sanmar_nj_browser.py` suite uses a local ChromeDriver selected
by `SANMAR_FIXTURE_CHROMEDRIVER`. It verifies all 45 saved-address selections,
changed/duplicate-address rejection, UPS/NJ fulfillment, full shipping-address
review, newest saved Production Notes, and a persisted production-date edit that
preserves the Due Date. It uses an isolated ignored runtime profile and opens
only local fixtures. Run the full Python suite for the server notification change.
