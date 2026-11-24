# Work PC Automation

Windows/macOS Flask and Selenium dashboard for CRM and Salesforce workflows, SanMar stock purchasing, Paycom time tracking, Slack messages, Google Sheets scanning, and private remote controls. Includes the Manifest V3 **CRM Order Assistant** Chrome extension for native order editing and stock ordering, queued order-page tools, list helpers, dark mode, and Salesforce tab reuse. Desktop power and hardware metrics remain Windows-only.

The app runs locally with a browser control panel and a tray icon. Its persistent queue coordinates scheduling, repeat runs, locks, guarded retries, per-order reports, stage timing, and audit logging. Supported single-order workflows share a bounded worker pool; the same order and incompatible workflows retain their queue boundaries. Native extension editors and Order All actions also work with the app closed.

Each computer owns its queue, browser sessions, credentials, and recovery receipts. Remote controls run work on the computer serving that dashboard; queue work from only one computer at a time. Private Tailscale access and authenticated Windows/macOS clipboard transfer are supported. See [local setup](#local-setup), the [extension guide](crm-order-dark-mode-extension/README.md), and [Mac/tablet setup](docs/MAC_AND_TABLET_SETUP.md).

## What It Does

- Runs Paycom clock-in and clock-out actions.
- Syncs weekly Paycom hours and tracks local work-hour state.
- Calculates auto clock-out timing against a configurable weekly hour cap.
- **Weekly schedule calculator:** click a day to cycle **On → Half → Off**. Half sets a **4 paid-hour daily target without lunch**; saved and live paid hours for that day count toward the target. On days split the remaining weekly hours after reserving the Half targets. The calculator flags any excess Half hours or hours left without an On day. Selections stay saved in that browser for the week, including existing On/Off plans. Refresh the dashboard to load this change; no app restart or extension reload is needed. These are planning estimates; clock-out schedules continue to use their existing controls.
- Sends Slack start/end/lunch/custom status messages.
- Rotates day-specific Slack messages.
- Runs CRM automation workers for address validation, stock unlocks, rush goods ordering, auto-splitting, shipping bypasses, push-back handling, Sleeve Prints reachouts, and queue-driven issue processing.
- Scans supported queue rows for cancellation, reachout, auto-split, and manual stock-order workflows.
- Provides a local web UI and HTTP API for manual controls and external triggers.
- Records automation results in a shared audit log.
- Keeps local state, result JSON, screenshots, logs, debug output, and cloned browser profiles under ignored runtime folders.
- Supports hidden startup through a Windows Script Host launcher.
- Supports Safe Sync & Start on both operating systems, a macOS LaunchAgent, independent local queues, and a PIN-protected Android control board over Tailscale.
- Supports authenticated manual and opt-in automatic text/PNG clipboard transfer between Windows and macOS without persisting clipboard contents.
- Checks GitHub before every supported startup and fast-forwards a clean checkout before loading the app.
- Automatically runs Safe Sync & Start when a clean checkout falls behind, waiting for active automation or timers before restarting. The Update button can also save tracked local edits, synchronize `main`, publish them, and restart without discarding work.
- Runs every queued task on the computer serving the current dashboard; use only one OS at a time when adding work.
- Runs Home Assistant/Alexa HTTP triggers on the computer whose private URL receives the request.
- Can clear finished local queue history without touching running or waiting tasks.
- **Quiet dashboard notifications:** bottom-right popups appear only for direct-action errors, such as failed settings saves, clipboard transfers, or connection tests. Routine confirmations, refreshes, and automation-run updates—including failed runs—stay in their sections, Queue, and reports. Refresh the dashboard to load this change; no app restart or extension reload is needed.
- **Persistent Queue:** queue entries, execution order, selections, schedules, results, and retry details are saved locally in `runtime/state/automation_queue.json` and restored after updates or restarts. Finished entries stay visible until **Delete** or **Clear finished**; there is no automatic queue-history limit. Waiting tasks resume in order, and idle scheduled/repeating tasks retain their next run time. A task interrupted while running becomes failed (or canceled if cancellation was requested) for review and never restarts automatically. Interrupted Communications tasks require checking Paycom/Slack before queuing another action; their Retry control stays disabled. Startup version locks still apply. Restart the local app and refresh the dashboard to enable persistence; no extension reload is needed. Saving begins with that app version, so entries already lost under older versions cannot be recovered from the queue.
- **Queue restart fix:** completed Communications tasks without additional result details no longer prevent server startup. Their saved history is preserved, and completed actions are not repeated. Restart the local app to load this fix; no extension reload is needed.
- Sorts the Queue list by **Latest first** (default), **Oldest first**, or **Errors first**. Latest/oldest use the time a task was queued; Errors first puts failed tasks first, newest first within each group. The selection stays active during queue refreshes. Position numbers show execution order, and drag-and-drop still reorders waiting tasks. Refresh the dashboard to load the control; no app restart or extension reload is needed.
- **Post to M6:** the switch at the right of the Communications / Processing / System / Console tab bar controls posts to M6 (`T03DK2TN7/C04KPACK6VC`) from this computer, including Chrome extension tasks. It starts **On**, stays saved locally across restarts, and is checked when a Slack step runs. On is filled; Off uses a neutral, unfilled outline in both color themes, including on hover. **Off** records an intentional skip and lets CRM work, emails, and stock-issue status updates finish. Other Slack conversations keep their existing behavior. Changes take effect immediately after saving; turning it back on does not send skipped messages. Restart the app and refresh the dashboard once after installing this feature; no extension reload is needed. Button appearance changes need only a dashboard refresh.
- **Schedule and repeat together:** Processing's Main automation and Sheet Scanner can use both controls. For example, **Scheduled 3:00 PM** plus **Repeat every 30 minutes** starts at 3:00 PM, then queues runs at 3:30 PM, 4:00 PM, and so on until canceled. Existing queue serialization applies; a busy queue may delay a run, and long runs skip elapsed intervals without catching up. A zero-minute interval repeats after each finish. Schedules and repeat timing survive restarts. Scheduling controls accept only later times today, including Communications; past times are rejected instead of moving to tomorrow. Restart the app and refresh the dashboard to load this change; no extension reload is needed.
- Each Queue entry links to its CRM order from the seven-digit order number in its title, opening a new tab. Queue subtitles use plain text. Repeat history and run reports also provide CRM order links. Refresh the dashboard to load the links; no app restart or extension reload is needed.
- **Queue run reports:** **View Report** replaces **View Errors**. Finished Main automation entries open the full run report with tool/order/error totals, duration, per-tool outcomes, and linked order-specific errors, including successful runs and the last finished run of an idle repeating task. Each report uses that queue entry's saved results. The report dialog fits the window height and scrolls only its report body, keeping the title and Close button visible. Reorder Stock retains **View orders** and its purchase confirmations; retry reports retain their recovery details. Refresh the dashboard to load this change; no app restart or extension reload is needed.

## CRM Order Assistant and Newer Workflows

- **Order Goods now only submits stock:** the existing automation opens applicable
  orders in one browser, up to 25 background tabs at a time, and runs the native
  Order All requests for every enabled automatic stock control. Single-product
  orders use the same procedure. Orders in edit mode and locked, disabled,
  manual, or already ordered stock are skipped. Order Goods no longer unlocks
  stock, investigates
  error messages, retries orders, or refreshes to verify purchases. Reports say
  **Submitted**; a completed submission run does not establish that every purchase
  succeeded. Existing list filters, batch limits, queue locks, dry runs, and
  durable duplicate-submission receipts remain. The saved worker-count parameter
  remains compatible; Order Goods uses one browser regardless of that value.
  Separate Stock Unlocker and Push Back workflows retain their own behavior.
  Restart the app and refresh the dashboard for the updated report labels; the
  next queued worker uses the replacement. No extension reload is needed.

- **Order All in Window:** CRM Order Assistant **1.8.8** adds a toolbar-popup button
  that orders eligible stock for all unique CRM orders open in the current Chrome
  window, including single-product orders and unchecked editing-list entries.
  Orders in Edit Order, including editing duplicate tabs, are skipped. The action
  reuses native Order Goods, preserves editing drafts, skips locked/manual/already
  ordered stock, and lists per-order and product-tab results. Confirmed unchanged
  stock is protected across duplicate tabs and window batches; uncertain requests
  require CRM review. Each fully confirmed order schedules one guarded refresh of
  its chosen tab. Reload the extension at `chrome://extensions` and refresh open
  CRM pages; no app restart or new permission is needed. See the
  [window ordering guide](crm-order-dark-mode-extension/README.md#order-all-in-window---188).

- **Parallel single-order Queue:** compatible live orders submitted one at a time can
  start immediately in free worker slots, up to the existing **1–8 worker**
  setting in **Settings > System & workers**. The pool supports Copyright,
  Content Violation, Existing Designs, Outside Limit, and Unresponsive
  cancellations; Copyright Removal/Reachout; both Complicated EMB routes;
  Oversize EMB; Address Validator; Product Separator; Order Goods; and Push Back.
  Different supported workflows share the same limit. Each order retains its
  own Queue row, live progress, report, Cancel, and Retry. Requests for the same
  order wait, and an intervening unsupported task drains the pool before running;
  list runs and existing purchasing workflows retain their queue boundaries.
  Push Back's SanMar fallback serializes cart access across workers. Cancellation
  and reachout slots reuse the profiles in Salesforce Worker Setup/Test.
  **CRM Order Assistant 1.8.7** polls each order independently and refreshes open
  matching order tabs after completion, including partial failures. Refreshes
  wait for native edits, draft notes, and dialogs to close; native Save All keeps
  its existing behavior. Restart the local app, refresh the dashboard, reload the
  extension at `chrome://extensions`, and refresh open CRM pages. QGT support and
  existing workflow recovery receipts remain available.

- **CRM worker logins:** Address Validator, Product Separator, Order Goods, and Push Back refresh each owned CRM worker slot from the current connection profile before starting its browser. A newly authenticated connection therefore reaches the next dispatch instead of leaving an older worker copy at Login. Salesforce scanner/reachout slots retain their saved worker sessions and setup. Existing same-order exclusions, queue barriers, purchasing locks, and recovery receipts remain. Restart the app and refresh the dashboard; no extension reload or CRM page refresh is needed.

- **Exclude orders during editing:** CRM Order Assistant **1.8.6** lets you uncheck an order during All Orders editing, including after validation, rescan, or pre-save conflicts, and continue with the remaining orders and the same draft. Apply, Save All, and Cancel skip excluded orders, leaving their native editors untouched for manual review. Interrupted saves retain their review safeguards. Resolve existing native edits before reloading the extension at `chrome://extensions`, then reopen the popup or expanded editor. No app restart or order-page refresh is needed. See the [exclusion guide](crm-order-dark-mode-extension/README.md#exclude-orders-during-batch-editing---186).

- **Compact automation dialogs:** CRM Order Assistant **1.8.5** removes workflow descriptions and explanatory hints from order-page automation popups. Titles, controls, product details, field labels, selection/scope indicators, and validation/error messages remain. Reload the extension at `chrome://extensions` and refresh open CRM order pages; no app restart is needed.

- **Faster on-demand tab scans:** CRM Order Assistant **1.8.4** reads already-loaded products, design names, totals, and print areas when you open Stock Issue, Copyright - Cancel, Complicated EMB, or Oversize EMB. Complete data is read without switching design tabs or waiting per tab. There is no background scan or extra order-opening request. Missing fields retain the existing visible-tab fallback; changed or ambiguous data stops for review. Copyright and embroidery workers also use fresh loaded-data reads during verification. Reload the extension at `chrome://extensions` and refresh open CRM order pages; no app restart or new permission is needed.

- **Native Save All without refresh:** CRM Order Assistant **1.8.3** replaces Save All & Refresh with **Save All**. It starts every included order's native Save Order action together, records per-order outcomes, and never refreshes order or duplicate tabs. Slow or failed orders do not delay starting the other saves. **Check save status** only reads the current pages and never reloads or resubmits. Inspect the actual orders for confirmation. Reload the extension and reopen its popup; no app restart or new permission is needed.

- **All Orders apply/discard and larger editor:** CRM Order Assistant **1.8.2** removes Preview changes; Apply changes still validates every included order before editing. Cancel unsaved edits chooses **No** only on CRM's exact review-and-save warning and verifies the original saved values afterward. The footer is one row, with the three smaller actions on the left and Apply / Save on the right. The popup uses up to Chrome's 600-pixel height limit, and **Expand editor** opens the same batch in a taller, resizable window sized to the current monitor while preserving its original order-window scope. Reload the extension and refresh CRM pages; no app restart or new permission is needed.

- **Popup header cleanup:** CRM Order Assistant **1.8.1** places the dark-mode toggle at the top right and the local-app bridge status at the top left. The separate extension title, window badge, and Extension settings section are removed. Reload the extension at `chrome://extensions` and refresh open CRM pages; no app restart is needed.

- **All Orders popup editor:** CRM Order Assistant **1.8.3** opens an All Tabs-style editor from the extension icon for CRM orders in that Chrome window only. Select orders, use **Edit All Orders**, change print-area locations/methods or add/remove areas, and optionally append Sales, Production, and Art notes. Single-tab and multi-tab orders are included; existing note history cannot be edited or removed. **Apply changes** validates and updates the native editors, and **Save All** starts their native Save Order actions together without refreshing any tabs. Inspect the actual orders for confirmation; failed saves retain their editors. Popup drafts survive closing/reopening; interrupted work stops for review without automatic resubmission. The feature uses native CRM functions and requires no local app, Selenium worker, or queue. Reload the extension at `chrome://extensions` and reopen its popup; no app restart is needed. See the [All Orders guide](crm-order-dark-mode-extension/README.md#all-orders-popup-editor---180).

- **All Tabs print-area title colors:** CRM Order Assistant **1.7.29** uses CRM's exact native title colors for HD Digital (pink), Screen Printing (blue), Embroidery (green), and Direct To Garment (purple). Colors follow staged method selections and new areas in both light and dark mode; other methods and rows keeping mixed methods stay uncolored. Reload the extension at `chrome://extensions` and refresh open CRM order pages; no local app restart is needed. See the [title color guide](crm-order-dark-mode-extension/README.md#all-tabs-print-area-title-colors---1729).

- **All Tabs shared stock editing:** CRM Order Assistant **1.7.28** uses a native-style product row with a preview, description, current color already selected, and combined read-only size quantities, without price fields. For example, matching L × 1 and XL × 2 on tabs 2 and 3 show L: 1 and XL: 2. Only catalog product/color combinations appearing on two or more distinct active product tabs are included; other colors do not contribute to those totals. Each matching item retains its own sizes in use, quantities, prices, and names. A replacement missing any required size lists the missing sizes and affected tabs and blocks the entire stock/print-area batch. Select a native product suggestion and color, use **Apply changes**, then CRM's **Save Order** and refresh to verify persistence. **Keep original stock** resets one row; **Discard pending changes** resets the staged batch. Reload the extension at `chrome://extensions` and refresh open CRM order pages; no local app restart is needed. See the [shared stock guide](crm-order-dark-mode-extension/README.md#all-tabs-shared-stock-editing---1728).

- **Multi-tab controls:** CRM Order Assistant **1.7.26** shows **All Tabs** and **Order All** only when an order has more than one active product tab. Removed tabs do not count. Adding a second tab restores the applicable control; Order All can still appear when only one of several tabs needs stock. Reload the extension at `chrome://extensions` and refresh open CRM order pages; no local app restart is needed.
- **Editing previews, Order All, and area removal:** CRM Order Assistant **1.7.24** keeps the highlighted tab's Design Studio Previews at the bottom during Edit Order. Outside edit mode, **Order All** follows the last real tab when any tab has eligible automatic stock ordering. One click starts the native CRM Order Goods requests for all eligible tabs immediately, with per-tab results and one final refresh. It works with the local app closed and creates no automation job. Locked, ordered, manual-order, and Add Box controls remain excluded; failed or uncertain requests stop for stock review without automatic retries. All Tabs adds a red **×** to stage removal of exactly that location from its listed tabs, with **Undo**, **Apply changes**, and normal **Save Order**. Front removal keeps chest locations. Reload the extension at `chrome://extensions` and refresh open CRM pages; no local app restart is needed. See the [editing and stock guide](crm-order-dark-mode-extension/README.md#editing-previews-and-order-all---1722).

- **All Tabs print-area editing:** CRM Order Assistant **1.7.21** adds **All Tabs** before tab 1 in Edit Order while preserving the highlighted real tab and customer tab count. It lists every print-area location with its affected tabs, supports location/method changes and swaps, and adds areas only to tabs missing that location. Stage edits, use **Apply changes**, then CRM's **Save Order**. Duplicate-location conflicts block the batch; each tab retains its notes, artwork, dimensions, and prices. Reload the extension at `chrome://extensions` and refresh open CRM order pages; no local app restart is needed. See the [All Tabs guide](crm-order-dark-mode-extension/README.md#all-tabs-print-area-editing---1721).

- **Reorder Stock (SanMar only):** in Custom Processing, select **Order IDs or order links**, paste explicit orders, then select **Reorder Stock** at the bottom left. The input creates **one queue entry** with **View orders**, per-order results, and confirmed purchase links. Orders run serially; Retry skips successes and retains recovery receipts. Red SanMar stock assigned to a former subcontractor is replaced; **M6 Print and Ship to Philly (PA)** means in-house. SanMar retains existing mappings, exact-stock allocation, Robbinsville pickup for NJ-only stock, the **CRM order number as Customer PO**, delivery-date adjustments, saved-value verification, and duplicate-purchase protection. S&S website automation, browser connection settings, and extension access have been removed. Non-SanMar red stock and old S&S receipts stop for manual review before any purchase. Existing receipts and historical confirmation links remain available. Restart the app, reload **CRM Order Assistant 1.7.16** at `chrome://extensions`, and refresh the dashboard and open CRM pages. See the [Reorder guide](docs/REORDER_STOCK.md).
- **Custom Processing lists:** in Processing's **New main automation run**, select **Custom**, then choose a **CRM list link** or **Order IDs or order links**. The second option accepts up to 100 orders as seven-digit numbers (with an optional `#`), individual CRM order links, or a mix, separated by commas, spaces, or new lines. Direct `/order/1234567` and embedded `/app#/order/1234567` links are converted to IDs before queuing; duplicates run once even when pasted as both an ID and a link. Report links belong in **CRM list link**. Select **Use this input**, choose the tools appropriate for the list, and **Add to Queue**. All seven tools are available and retain their existing save-verification and retry rules. Order ID lists target only those orders, in the pasted order, for each selected tool. If a step needs review, later tools skip that order while the remaining orders continue; failed-order retries stay targeted. Unlocker uses the configured locked-orders report and verifies a single-order selection before Apply. Custom keeps separate tool selections and offers **Edit input**; choosing another list mode restores that mode's selections. Queued, repeated, and scheduled runs retain their original custom input. Address Validator uses its existing All-mode per-order shipping rules. Restart the local app and refresh the dashboard to load this change; no extension reload is needed.
- **Order-page controls:** Chrome extension with dark mode, queued single-order Manual Process, Cancel, Reachout, and Stock Issue actions, plus Salesforce tab reuse across windows in the same Chrome profile. Version 1.6.2 removes Auto-Process from the order page and Process current order from the toolbar popup. Reload CRM Order Assistant at `chrome://extensions` and refresh open CRM order pages; no local app restart is needed for this removal.
- **Salesforce email sender default:** CRM Order Assistant **1.7.19** automatically chooses **Orders &lt;orders@rushordertees.com&gt;** when a Salesforce email composer opens. You can manually change the sender afterward. It leaves recipients and email content intact and never sends the draft. Missing, disabled, ambiguous, or unloaded Orders options leave the current sender for manual review. Reload the extension at `chrome://extensions` and refresh open Salesforce pages; no local app restart is needed. See the [sender default guide](crm-order-dark-mode-extension/README.md#salesforce-orders-email-default---1719).
- **CRM list selection and rush highlighting:** CRM Order Assistant provides **Open Applicable** and **Select Applicable** below Show Images & Sizes, opposite the report title. Open Applicable selects the tan, neon-green, and purple orders on the loaded page and opens each in a background tab in the same Chrome window, keeping the list active and selected. Select Applicable applies the same three-color selection without opening tabs. Stock-issue and dimmed orders keep their original status colors and are excluded and deselected by both buttons, even when max-rush dates match. **Version 1.7.20** adds a **1 PM local-time cutoff**: production scheduled for today or the next business day becomes neon-green max rush when fulfillment/due date is the business day after that. For example, Tuesday from 1 PM highlights Tuesday/Wednesday production due Thursday; Friday from 1 PM highlights Friday/Monday production due Tuesday. Saturdays and Sundays are skipped when calculating business days; holidays are not. The existing Friday-production/Monday-due rule on Friday and Monday-production/Tuesday-due rule on weekends remain active all day. Grouped production-date headers are supported, and colors update automatically at 1 PM, midnight, and as rows change. These features work independently of dark mode. Reload **CRM Order Assistant 1.7.20** at `chrome://extensions` and refresh CRM lists; no local app restart is needed. See the [extension guide](crm-order-dark-mode-extension/README.md#open-applicable-and-select-applicable).
- **Copyright cancellation by design:** Cancel > Copyright - Cancel scans every tab's Design Name. Tabs that share the same artwork Design ID remain individually selectable using their saved order-tab IDs. Selecting all tabs retains whole-order cancellation and **[AUTO] Copyright Cancel**. Selecting some removes only those designs, accepts purchased-stock tabs retained at QTY 0/$0.00, verifies the recalculated credit, and uses **[AUTO] Copyright Cancel - Partial** while the remaining order stays active. Partial work and financial submissions have recovery receipts; retry the original queued task. Restart the local app, reload CRM Order Assistant 1.6.1, and refresh CRM order pages after updating. See the extension guide for refund checks and recovery details.
- **Partial copyright cancellation recovery:** if a refund is completed while a task is stopped after removing designs, Retry verifies the exact credit amount, reduced Paid, zero Balance Due, unchanged total/fees, and retained transaction history before resuming the Sales Note and email. It does not issue the refund again. A missing Sales Note is saved again once after reload and must persist before the workflow continues. Note entry waits for Edit Order to initialize, and an unsaved draft is saved rather than treated as a completed note. Unrelated payment changes still stop for review; existing Salesforce email confirmation receipts still apply. The next worker loads this change; no app restart, extension reload, or page refresh is needed. Keep the original queue entry and recovery receipt.
- **Manual single-order processing:** choose address validation, mixed-product separation, auto-splitting, stock unlocking and goods ordering, shipping bypass, push back, or Extra Print Areas from the extension's Manual Process menu.
- **Order Goods across product tabs:** single- and multi-product orders use the native stock submission engine shared with the extension's **Order All**. Normal Order Goods submits enabled automatic stock requests in one browser, with up to 25 background order tabs; it skips editing, locked, disabled, manual, and already ordered stock. Results say **Submitted**, without unlocking, supplier-error investigation, automatic purchase retries, or a verification refresh. Durable receipts under `runtime/state/order_goods_native/` protect unchanged submissions across worker profiles and restarts; uncertain or interrupted submissions stop for review. Keep the receipts. Dry runs submit nothing, and Push Back retains its separate feedback and recovery flow. See the [native stock workflow](docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md#order-goods-native-batch).
- **Auto Splitter stock changes during a split:** the worker rechecks the original's stock before trimming, after saving, and after retained-order payment allocation. If CRM orders stock during a split that began with unordered stock, the worker switches to the stock-ordered cancellation method: reuse the existing split orders, create a replacement for split 1, verify all split contents, follow the existing vendor/PO routing rules, allocate the original payment, then cancel the original through the existing verified cleanup. Saved design contents, shipping shares, and promo allocation stay consistent with the already-created orders. A trimmed original uses its current subtotal for the Refund fee and only its remaining payment for the manual transfer transaction. The switch and new order IDs are saved for targeted retries. An uncertain replacement conversion or ambiguous stock state stops for review before creating another replacement. If stock remains unordered, planned moved tabs retained by CRM are accepted only with verified zero quantity and $0.00 totals. Restart the local app and refresh the dashboard to load recovery changes; no extension reload is needed.
- **Separator screenprinting review:** the Auto Separator warning checks only the source design tabs actually being separated. Screenprinting on an untouched tab or a tab excluded because of custom names and numbers does not trigger the warning. Print methods are checked before separation changes the designs. The next separator worker uses this change automatically; no app restart or extension reload is needed.
- **Batch address validation:** an order that still times out after the bounded recovery attempts is recorded for manual review. The validator continues to the remaining eligible orders and excludes attempted orders from later list scans in that run. A completed batch shows its success/review counts; the order's timeout remains available in **View Report**. Restart the local app and refresh the dashboard to load the summary update.
- **Extra print areas:** sleeve, side, and reversible printing, plus additional embroidery areas, with design selection, configurable pricing, saved-change verification, and retry receipts for supported workflows.
  Reverse-print clones use **REVERSEPRINT**. CRM saved the older **REVERSE-PRINT** name as **H-PRINT**, which stopped verification before email. Retry can reuse and rename an existing H-PRINT clone only when its artwork, print areas, Style Sub details, reversed colors, and size quantities match the original; uncertain or duplicate matches stop for review. The saved order is reloaded and verified before email. Restart the app and refresh the dashboard to load this fix; no extension reload is needed. Use Retry on the original queue entry to keep its selections, price, and email receipts.
  The worker waits up to 45 seconds for the invoice popup's non-PDF **View Invoice** link, checking both the CRM app and its parent page. It cancels the popup after copying the link and never sends the CRM invoice. A timeout reports the wait explicitly.
  Sleeve-only requests priced at $0.00 use Salesforce **[AUTO] Comp Sleeves**, add the selected sleeve areas, and retain existing product prices. Restart the local app to load this worker update; no extension reload is needed.
  Extra Print Areas waits for Edit Order to initialize and reloads after saving to verify persisted print areas, prices, and Sales Notes. An embroidery receipt records intended changes before Save Order, so Retry can apply missing areas and fees after a rejected or partial save. Every product/size price must equal its original value or the receipt's exact target; Retry writes the target and never adds the fee again to an already-priced row. New receipts also verify product/size identities and quantities; older receipts retain their targets and derive the original price by subtracting the original selected fee. Unrelated price, product, size, or quantity changes stop before saving. A missing note gets one note-only save retry, and Salesforce email proceeds only after all saved changes verify, retaining confirmed-email receipts. Restart the local app to load this fix; no extension reload is needed. Use Retry on the original queue entry and keep its recovery receipt.
  Email success uses the shared Salesforce Activity confirmation and bounded resend policy described below.
  **Payment texts:** paid Extra Print Areas requests send two Twilio texts after the Salesforce email is confirmed: the requested payment message with the selected request and unit prices, then the same **View Invoice** URL by itself. Accounts use their visible Messages panel; Contacts open **Text** first. Mixed requests retain each category's price, and reverse printing retains its per-area price. Requests with no additional payment, including Comp Sleeves, remain email-only.
  Text receipts under `runtime/state/extra_print_text_receipts/` retain the original invoice URL, confirmed email, and each text's submission state. A new matching outgoing Twilio record confirms submission even when its delivery status is **failed**, **undelivered**, or still pending. The invoice text follows the payment text regardless of carrier delivery; status is retained for the record. Retry skips confirmed email/text submissions and can reconcile an attempted text only when the original customer's history contains more exact outgoing matches than its saved pre-send count. It then sends only the unattempted invoice text. Missing or ambiguous records stop for review without resending an attempted text. Keep the original queue entry and receipts. Restart the local app to load this submission-based behavior; no extension reload or settings change is needed.
  Twilio message entry uses native keyboard events so Lightning enables **Send Message**, and focuses the scoped input above Salesforce's fixed utility bar. Confirmation checks normal and failed outgoing records. Restart the local app to load this live-input correction; no extension reload is needed.
  Text entry now returns only scalar readiness data and uses Chrome DevTools native insertion and Tab, avoiding stale Lightning textarea references. Exact existing drafts are selected and rebound without appending the message again; unrelated drafts stop for review. Each saved text pair has a process lock, so a concurrent retry cannot reuse an earlier ready state. Attempted texts still require history reconciliation and are never resent automatically. Restart the app to load this update; no extension reload or page refresh is needed.
- **Extension task retries:** failed or canceled Manual Process (including Extra Print Areas), Cancel, Reachout, and Stock Issue tasks offer **Retry** beside **Delete** in the Automation queue. Retry uses the same entry, order, selections, prices, and reason, including after a server restart. Existing recovery receipts and review stops still apply. Restart the local app and refresh the dashboard to load the retry controls and invoice wait; no extension reload is needed. Requests already lost under older versions must be queued again with the same selections and prices; durable workflow receipts remain available.
- **Complicated embroidery:** select affected print areas under each design, including designs already switched to ink printing, then choose the existing Yes/No feedback route. Templates with `[PRINT]` and `[DESIGN]` name each selected area with its own design in one email, using lowercase wording such as `back print for "Team Logo"`. The worker rechecks names and areas before proceeding. Restart the local app, reload CRM Order Assistant, and refresh CRM order pages after updating.
- **Oversize embroidery:** Reachout > Oversize EMB to HDD scans design tabs and their current print areas, including designs already switched to ink. Front and Back areas start checked for every design; chest areas and other locations start unchecked. Adjust the affected areas before queuing; one Salesforce email replaces `[PRINT]` and `[DESIGN]` while keeping each area paired with its Design Name. The queued worker rechecks names and areas before proceeding. Reload CRM Order Assistant and refresh CRM order pages to load the automatic Front/Back selection; no local app restart is needed for this selection change.
- **Stock issues:** dedicated size, color, and extension-required workflows using order-page selections and queued workers.
  Extension Required lets you choose affected sizes for each product/color. All detected order sizes start checked; **Clear** deselects them so you can choose specific sizes. CRM Sales Notes and the Salesforce email include each product/color's selected sizes. Restart the local app, reload CRM Order Assistant, and refresh CRM order pages after installing this update.
  Extension Required and Suggest Different Size/Color use the shared Salesforce Activity confirmation described below.
  Suggest Different Size and Color emails also use color-before-description wording and keep each product/color's affected sizes separate. Their generated stock availability sentence works for one or multiple products. Restart the local app to load message wording updates.
  **CRM Order Assistant 1.7.9** adds **Stock Issue > Suggest Different Item**: select scanned CRM stock and enter a required replacement product. Edit **[AUTO] STOCK - Item**, retaining `[ORDER-NUMBER]` in its title and `[STOCK]` and `[PRODUCT]` in its body. **Cancel > Unresponsive** also has an editable **[AUTO] CANCEL - Unresponsive** starter with `[ORDER-NUMBER]` in its title. Both templates start with placeholder copy and are added once without replacing existing edits. Item suggestions verify saved stock before sending and use Activity confirmation and per-order retry receipts. Restart the local app, refresh the dashboard, reload the extension, and refresh CRM order pages. See the [email options guide](crm-order-dark-mode-extension/README.md#unresponsive-cancellation-and-item-suggestion-emails).
  **Version 1.7.10** gives every checked item its own required replacement input directly underneath it. Multiple items start unchecked; select each affected item and fill every displayed replacement field before queuing. One email pairs the `[STOCK]` and `[PRODUCT]` lists in the same order. Unchecking hides the field and excludes that item. Queue retries retain each pairing, while older queued requests keep their original combined replacement text. Restart the app, refresh the dashboard, reload the extension, and refresh CRM order pages.
  **Version 1.7.11** also lets you choose affected sizes for each item. All detected sizes start checked, with **Clear** and **Select all** controls. Each checked item requires at least one size and a replacement. `[STOCK]` names the affected sizes and uses exactly `in all sizes` when all are checked; `[PRODUCT]` adds no size wording. Sizes are verified against the saved order and retained for retries. Older whole-item requests and confirmed emails keep their recovery checks. Restart the app, refresh the dashboard, reload the extension, and refresh CRM order pages.
  **Suggest Different Item follow-up correction:** after Salesforce confirms the email, the worker saves `[STOCK] stock issue`, `Suggest [PRODUCT]`, and `Emailed Txted` on three separate **Sales Note** lines, reloads to verify persistence, confirms the `[CRM ORDER LINK] - Rush Order stock issue` Slack message when **Post to M6** is On, then applies and reload-verifies the configured stock issue status (normally `Issue - Stock`). A missing Sales Note gets one save retry; unsaved drafts and Production Notes do not count as confirmation. Retry the original queue entry to reuse confirmed email/Slack receipts and finish missing steps. Existing Production Notes are preserved. Restart the app and refresh the dashboard; no extension reload is needed.
- **Stock Swap reachout:** after saving comparable replacement products in CRM, choose **Reachout > Stock Swap** in CRM Order Assistant **1.6.7**. One email covers every unambiguous swap, using original products from the saved order's linked `/app#/quotes/<quote-id>` page for `[STOCK]` and saved current products for `[PRODUCT]`, including Style Sub replacements. Edit **[AUTO] Stock Swap** in **Settings > Salesforce Template**, keeping `[ORDER-NUMBER]` in its title and both product placeholders in its body. The placeholder template is added once without replacing existing edits. Unreadable or ambiguous comparisons stop for review; existing Salesforce confirmation checks and additional per-order receipts prevent repeated emails after title edits. Restart the local app, refresh the dashboard, reload the extension, and refresh CRM order pages. See the [Stock Swap guide](crm-order-dark-mode-extension/README.md#stock-swap-reachout).
  Stock Swap also records `Swapped [STOCK] to [PRODUCT]` followed by `Emailed` on a new Sales Note line after Salesforce confirms the email. The note must persist after reload; retries skip an existing note and reuse the email receipt if only the note needs recovery. This worker update loads on the next queued run without an app restart or extension reload.
  Stock Swap emails and Sales Notes name specific affected sizes, such as `in size medium` or `in sizes medium and large`, and use `in all sizes` when every ordered size of that product/color is affected and there is more than one size. Original and replacement sizes are checked separately across the saved quote/order, including other design tabs. Exact quantities remain in comparison and retry checks; earlier confirmed email receipts and Sales Notes remain recognized. The next queued worker loads this wording update; no app restart or extension reload is needed.
- **Sheets Scanner and Salesforce setup:** saved Salesforce worker profiles, setup and authentication checks, verification-code prompts, and scheduled or repeating queue runs.
- **Salesforce Contact emails:** Sheets Scanner and single-order cancellation/reachout and embroidery workflows send to Salesforce Contacts using the same local JSON templates as Accounts. Email recovery and partial copyright cancellation also support Contacts, with draft checks, Activity confirmation, and duplicate-send receipts. These senders now verify exactly one customer recipient, no Cc/Bcc, and one Orders sender before Send or a resend. Sender inspection reads the selected From control instead of repeating surrounding labels and recipient pills. Earlier Contact skips stay failed until retried successfully. The next worker uses this change automatically; no app restart or extension reload is needed.
- **Salesforce email confirmation:** every Salesforce email sender requires a new matching outgoing email in Activity. After Send, it waits up to 30 seconds, refreshes Salesforce if the email is missing, and checks for another 30 seconds. **Extra Print Areas/Comp Sleeves make one send attempt and stop for review if Activity is still missing; they never automatically resend an uncertain email.** Other senders can rebuild and verify the same draft once for a second send, with the same Activity/refresh checks. If it remains unconfirmed, the affected order stops, the dashboard records the error, and the app sends a desktop notification when its tray icon is available. Durable receipts under `runtime/state/extra_print_email_confirmations/` block later automatic resends, including queue retries; inspect Activity before resolving a receipt manually. Existing unconfirmed receipts remain blocked. Workflow-specific receipts keep Size and Color emails separate even when their subjects match. Delayed Activity can cause a permitted resend in another workflow to deliver a duplicate; Activity confirmation does not prove inbox delivery. Restart the local app to load this update; no extension reload is needed.
- **Salesforce page redraws:** login checks reacquire the complete input list up to three times if Salesforce replaces its page elements. Repeated redraws stop with a specific error; partial input reads never count as an authenticated session. The customer's email link is now found and clicked within one page command, including links in Lightning shadow DOM. This avoids the stale element list that could keep failing after rereads. Only an exact matching email link is eligible; hidden links, unrelated page text, and draft recipients are excluded. These changes do not repeat credentials or sends. Stale element failures show **Page changed during automation** instead of **Browser session failed**. Restart the local app and refresh the dashboard; no extension reload or CRM page refresh is needed. Retry the original queue entry to retain its selections and receipts.
- **Salesforce HTML loading and Send:** the template loader fills the subject in the page and addresses only the owned CKEditor frame through Chrome DevTools, using frame indexes or scoped shadow paths. It preserves the existing HTML, formatting, placeholder, and recipient checks while avoiding Lightning element serialization and WebDriver frame-switch errors. Send uses one native mouse click from verified button coordinates; an uncertain click is never replayed through another click method. Live Windows verification confirmed one outgoing email that remained after refresh, with a receipt preventing another send. Restart the local app to load these shared worker changes; no extension reload or CRM page refresh is needed. Live macOS remains unverified.
- **Salesforce refresh indicator:** if email Activity is missing and the confirmation check refreshes Salesforce, the result shows **Salesforce refresh used** with the order number. The warning appears in queue messages and Sheets Scanner run/order history, and triggers a desktop notification when the tray icon is available, even if the email is later confirmed successfully. The receipt also retains the refresh flag and count. An old receipt reused on a later run does not trigger another refresh notification. Restart the local app and refresh the dashboard to load this update; no extension reload is needed.
- **Slack delivery confirmation:** Slack sends use the Send button and require a new matching message with a Slack timestamp in the intended conversation, followed by a refresh that still shows the message. Clearing the composer alone does not confirm delivery. Only confirmed posts enter local Slack history. An uncertain submission stops automatic resends and browser fallback; inspect the conversation before using a Slack-only action again. Work In and Work Out save the successful Paycom punch before attempting Slack and report a failed task if Slack fails, with the Paycom success retained in the message. Their Retry control stays unavailable after a recorded punch so recovery cannot repeat it. Restart the local app and refresh the dashboard to load the server changes; no extension reload is needed. These changes do not resend earlier missing messages.
- **Slack confirmation with older channel history:** delivery checks return the main conversation to its latest messages when Slack restores an older scroll position, including after the verification refresh. A submitted message still needs its new Slack timestamp, matching text, and persisted non-pending state; scrolling does not submit another message. This prevents successful posts from being reported as unconfirmed merely because recent messages were not loaded in the restored view. The next worker uses this fix automatically; no app restart or extension reload is needed. Existing failed queue entries retain their recorded status until retried.
- **Cancellation Slack duplicate prevention:** full-order cancellation and post-cancel Slack recovery check for the exact order notification in the intended conversation before sending. A matching post must have a Slack timestamp and remain present after refresh; an existing confirmed delivery receipt also completes the Slack step without another send. Receipts under `runtime/state/slack_order_deliveries/` are scoped to the order, workspace, and conversation and are reserved before Send, so an interrupted or unconfirmed submission cannot be blindly repeated after a retry or restart. Retries of already cancelled/refunded orders without an old receipt can adopt a verified existing post. If the post cannot be found in loaded channel history, or a matching post is pending or cannot be verified, recovery stops for review without resending. Keep these receipts. The next worker loads this change; no app restart, extension reload, or page refresh is needed. Earlier failed labels are updated only when their task is retried successfully. Ordinary Slack status/custom sends retain their existing behavior.
- **Slack visibility:** local post history and paid-rush notifications for supported order workflows.
- **Salesforce template editor and sending:** Settings > **Salesforce Template** edits the local JSON library with **Add**, **Duplicate**, and **Remove** controls, a single HTML/formatted-view toggle, and controls for fonts, sizes, bold, underline, lists, alignment, links, and hosted images. **Automatic** beside the text-color picker resets only selected text color, preserving other formatting and following light/dark mode; manually selected colors stay fixed. New templates use automatic color, and theme changes never save the dashboard's display colors into emails. Refresh the dashboard to load this control; no app restart or extension reload is needed for the color update. Add/Duplicate open a draft for review before **Create template** saves it; Remove confirms deletion and retains a backup. All 18 automated email templates load their saved subject and HTML directly into Salesforce's source editor, without searching its template picker. This includes Sheets Scanner, cancellation/reachout and embroidery emails, Additional Requests/Comp Sleeves, all Stock Issue emails, Stock Swap, and their recovery paths. Existing placeholder checks, recipient/From verification, Activity confirmation, duplicate-send receipts, and queue locks remain in effect. Renaming a template retains its old worker reference; missing or invalid local templates stop for review. New library entries do not create new automation actions. Copy the active JSON library to each computer that runs email workers. Restart the local app and refresh the dashboard after updating; no extension reload is needed for template-editor changes. See [the template editor guide](docs/SALESFORCE_TEMPLATE_EDITOR.md).
- **Settings and connectivity:** shipping product/color mapping editor, service connection controls, OS credential storage, private tablet access, and authenticated Windows/macOS clipboard transfer.

Salesforce template saves remove pasted `data-*` attributes such as
`data-pm-slice`, preventing the reported Salesforce HTML attribute error.
Existing templates are cleaned with a backup on load, and workers clean HTML
before filling the Salesforce composer. Formatting, links, images, and
placeholders are retained. Restart the app and refresh the dashboard after
updating; no extension reload is needed. See the
[template editor guide](docs/SALESFORCE_TEMPLATE_EDITOR.md).

See the [CRM Order Assistant guide](crm-order-dark-mode-extension/README.md) for installation, workflow details, and reload instructions. The extension is loaded locally through Chrome's **Load unpacked** control.

## Project Layout

- `server.py` - main Flask server, persistent queue, scheduler, worker pool, tray app, and orchestration layer.
- `queue_order_work.py` / `workers/crm_queue_order.py` - explicit single-order pool eligibility and isolated worker dispatch.
- `process_locks.py` - cross-platform process locks for shared vendor-cart access.
- `ui_panel.html` / `static/` - local browser control panel, Salesforce template editor, and dashboard assets.
- `workers/` - Selenium workflows for CRM, SanMar, Salesforce email/texts, Paycom, Slack, and Google Sheets scanning.
- `routes/` - grouped Flask route modules.
- `automation_runtime.py` - shared Selenium/runtime helpers.
- `runtime_paths.py` - centralized paths for ignored local runtime artifacts.
- `runtime_maintenance.py` - bounded retention for disposable runtime evidence.
- `config_defaults.py` - tracked non-secret setting contract inherited by each machine's `config.py`.
- `platform_runtime.py` / `node_preferences.py` - platform capabilities, worker capacity, and machine-local preferences.
- `automation_audit.py` - audit log helpers.
- `credential_store.py` / `manage_credentials.py` - Windows Credential Manager and macOS Keychain integration.
- `service_health.py` - read-only live authentication checks that never print secret values.
- `app_security.py` / `setup_app_security.py` - PIN/session, CSRF, and private-access security setup.
- `clipboard_runtime.py` / `configure_clipboard.py` - authenticated text/PNG clipboard transfer between computers.
- `safe_sync.py` / `version_state.py` - safe Git startup, version locks, updates, and publish/restart handling.
- `slack_message_rotation.py` - alternating Slack message state logic.
- `slack_delivery_receipts.py` / `slack_post_history.py` / `m6_posting.py` - order-notification receipts, confirmed post history, and the local Post to M6 switch.
- `salesforce_templates.py` / `salesforce_email_html.py` - local email-template library, backups, and HTML cleanup.
- `config.example.py` - safe template for local runtime settings.
- `shipping_bypasser_mappings.py` / `shipping_bypasser_product_color_mappings.json` - editable CRM-to-SanMar product/color rules shared by Shipping Bypasser and Reorder Stock.
- `docs/` - fuller system guide and CRM automation notes.
- `crm-order-dark-mode-extension/` - CRM Order Assistant extension and local app bridge.
- `tests/` - regression coverage for CRM workflows, queues, security, credentials, cross-platform runtime, sync, and extension behavior.
- `AGENTS.md` - repository guidance for coding agents, validation, and handling local data.

## Local Setup

This repo intentionally does not commit real credentials, browser sessions, logs, screenshots, state files, or machine-local binaries.

1. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Create your local config. It imports the tracked defaults, so new safe
   settings added by future updates are available automatically:

   ```powershell
   Copy-Item config.example.py config.py
   ```

3. Fill in `config.py` with non-secret local values such as Slack channel URLs and CRM report URLs.

4. Store login secrets with the cross-platform credential tool: Windows Credential Manager on Windows or Keychain on macOS. Secret prompts do not echo their values. Paycom setup prompts for its username, password, and four-digit PIN:

   ```powershell
   python manage_credentials.py set paycom
   python manage_credentials.py set crm
   python manage_credentials.py set sanmar
   python manage_credentials.py set salesforce
   python manage_credentials.py set slack
   ```

   For Google Sheets, import your local service-account JSON with `python manage_credentials.py set google_sheets --json-file PATH_TO_SERVICE_ACCOUNT.json`. Keep the source JSON private and outside tracked files. The older `manage_windows_credentials.py` remains available for Windows-specific setup and migration.

5. Start the server:

   ```powershell
   python server.py
   ```

6. Open the local UI:

   ```text
   http://127.0.0.1:5123/ui
   ```

For hidden startup on Windows, use `start_server_hidden.vbs`; it now performs Safe Sync & Start. For macOS and Android/Tailscale onboarding, follow [`docs/MAC_AND_TABLET_SETUP.md`](docs/MAC_AND_TABLET_SETUP.md).
A direct `python server.py` launch also performs the same safe startup check. Uncommitted, ahead, or diverged work is never overwritten; the app starts locked and explains what must be resolved.

The dashboard's **Update** action includes new `.mjs` JavaScript modules and Node
tests in recognized source folders. Secret-name and runtime-file exclusions still
apply. If an older running app blocks a reviewed `.mjs` file, stage that exact file
with Git and retry **Update**; the normal update restart loads this correction.

ChromeDriver launches do not inherit the dashboard's open handles. This prevents
a detached setup driver from keeping port 5123 occupied after a Windows server
restart. Restart the local app to load this change for setup and connection tests;
queued workers load it on their next run. No extension reload is needed. If an
older driver already holds the port, exit that orphaned driver before starting
the app again.

If a worker reports **Windows Application Control blocked ChromeDriver (WinError 4551)**,
Windows prevented the browser driver from running before an automation session
started. The error includes the selected driver path. Check **Windows Security >
App & browser control** and **Event Viewer > Applications and Services Logs >
Microsoft > Windows > CodeIntegrity > Operational**, event **3077**, for the
blocked file and policy. The `VerifiedAndReputableDesktop` policy belongs to
[Smart App Control](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/inbox-appcontrol-policies).
[Microsoft's FAQ](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions)
explains that it has no individual-app exception; have the device owner or Windows
administrator review the driver and available security options. Retrying the
same blocked driver or switching browser visibility does not resolve the policy.
The app does not change Windows security settings. For a clock action blocked at
this stage, that attempt submitted no Paycom punch; check Paycom before manually
recording or retrying a punch. Restart the local app to load this diagnostic for
in-process browser setup; queued workers load it on their next run. No extension
reload is needed.

## Shipping Bypasser Product/Color Mappings

Shipping Bypasser reuses an open SanMar inventory view when its product style header exactly matches the requested style. It preserves the selected color and still verifies color, size quantities, and cart contents before checkout. Searches that are needed allow up to 45 seconds for SanMar's search-to-product redirects. A slow repeat lookup previously could report an available product such as A903 as not found. The next worker loads this fix; no app restart, extension reload, or page refresh is needed. Products absent from SanMar search still require review.

Shipping Bypasser resolves each design's PO from the selected tab before searching the rest of the order page. A tab without an `H-` prefix keeps its own PO when another tab has an `H-` PO, so receipt checks compare the correct design's products and quantities. Numeric POs such as `5450885` and numeric POs with hyphens such as `5450885-3` are supported, and leading zeros are preserved. This fix loads with the next worker run; no app restart, extension reload, or page refresh is needed. Existing purchase receipts remain intact.

Shipping Bypasser supports XXS in SanMar inventory tables, quantity entry, and cart verification, including mobile and multiple-size cart rows. XXS remains separate from XS; warehouse allocation and the existing stock buffer still apply. This fix loads with the next worker run; no app restart, extension reload, or page refresh is needed.

Shipping mappings include Bella+Canvas CRM style `3200` and SanMar style `BC3200`, with 26 CRM color entries matching the supplied SanMar labels, including discontinued colors and both Deep Heather/Black spellings. `GREY/LT RED TRIBLEND` maps to `Grey/ Red Triblend`, and `WHT FLCK/CHAR TRIBLND` maps to `White Fleck/ Charcoal-Black Triblend`. CRM-only neon colors and White Fleck/Navy remain unmapped because they were absent from the supplied SanMar color list. Workers load these mappings on their next run. Use **Reload** in **Settings > Shipping mappings** to see the new entries; no app restart or extension reload is needed.

Shipping Bypasser checks each active design's visible vendor stock records before ordering. A recorded SanMar, S&S, or Local Inventory PO skips that tab even when splitting the order assigned it a different tab PO, the stock status still says unordered, or dark mode changes the row color. Tabs with no recorded stock remain eligible; saved NJ/ADD allocations still resume unfinished purchases through their existing receipts. This worker change loads with the next queued run; no app restart, extension reload, or page refresh is needed.

Use **Settings → Shipping mappings** to add an exact CRM product/color to SanMar product/color mapping. Each CRM product appears once and contains its own color list. Advanced fields support the SanMar inventory/pricing button, expected style IDs, and report labels. The same values remain directly editable in `shipping_bypasser_product_color_mappings.json`; malformed JSON and duplicate product/color entries fail with a specific error instead of being silently ignored.

Switch between **Specific items** and **Special brand rules** in that settings page. Specific items retains the editable product/color mappings; Special brand rules shows the automatic rules for Bella+Canvas, Comfort Colors, A4, Gildan, Rabbit Skins, Jerzees, and Next Level, with examples and matching limits. For example, recognized Bella+Canvas `3413C` resolves to `BC3413`, while `3001CVC` keeps its CVC letters. Brand rules are read-only, and exact item mappings take priority (including the separate `3001C` search-as-`3001` exception). Switching views preserves unsaved item edits. Restart the local app and refresh the dashboard to load the new view; no extension reload is needed.

Comfort Colors products remove `C` letters from their CRM style IDs before searching SanMar: `1467CC` searches as `1467`, and `1717CC` searches as `1717`. The rule requires Comfort Colors in the product details, retains exact item mapping priority, and verifies the resulting SanMar style in inventory and the cart. It also applies to Reorder Stock, which shares the Shipping Bypasser search rules. The next worker loads the rule automatically. Restart the local app and reload shipping mappings to display it in **Special brand rules**; no extension reload is needed.

Shipping Bypasser reads the order's subcontractor banner before ordering. Mach 6 / Mach6 Manufacturing orders always ship by **UPS to Mach6 in California**; they never use pickup. Prefer suitable stock near California, including a complete non-NJ multi-warehouse allocation before falling back to NJ. When NJ is needed, both NJ-only and mixed NJ/non-NJ allocations ship UPS under the **same original customer PO**, without an ADD prefix. Unsupported or unclear subcontractors stop for review and never default to in-house production.

For in-house orders, prefer a complete single-warehouse shipment instead of taking NJ stock first. Automatic Processing and list-driven runs leave NJ-only stock to normal CRM ordering and stop for review without a purchase. A manually queued single-order Shipping Bypasser task, including **Manual Process > Shipping Bypasser** and Retry of that task, may order NJ-only stock using **Pick Up at warehouse**, then **Robbinsville**, under the original customer PO. It records one CRM Manual Order without an ADD PO or Add box. This pickup permission is separate from the stock-buffer override; actual shortages, closed warehouses, subcontractor checks, cart verification, and purchase receipts still apply. Restart the local app to enable manual NJ-only pickup; no extension reload or page refresh is needed. Direct CLI single-order runs require `--allow-nj-only-pickup`; the option does not enable pickup in batch mode, and `--dry-run` still skips purchasing and CRM saves.

When multiple warehouses are needed and the allocation includes NJ, the worker purchases NJ separately under the original customer PO using **Pick Up at warehouse**, then **Robbinsville**. The remaining stock ships UPS under `ADD-<original PO>`, using one or multiple non-NJ warehouses, and is recorded through **Add box**. In-house multi-warehouse allocations without NJ use UPS and the same original PO across boxes. The configured stock buffer, manual buffer override, and closed-warehouse checks are retained.

For NJ/ADD orders, both carts, POs, and ADD delivery dates are checked before the first purchase. Each purchase is confirmed and recorded in CRM. Production notes identify the ADD PO and each product/color's sizes, piece quantities, and warehouse; multi-warehouse ADD notes include the box count. Ordinary shipped multi-warehouse notes retain their same-PO box counts. UPS selection is checked for the selected warehouses, and review-page fulfillment is verified before submission. Cart validation, late single-warehouse fallback, the latest-arrival production/due-date check, and free-shipping due-date extensions retain their behavior.

NJ/ADD allocations and purchase states are saved under `runtime/state/shipping_bypass_nj_orders/`, alongside the existing submission receipts. A confirmed NJ purchase does not complete an unfinished ADD purchase. An in-house retry uses its saved allocation, repairs missing CRM records or notes, and never repeats a confirmed purchase. A saved NJ/ADD allocation on a subcontractor order stops for review, including a completed receipt; confirmed purchases and receipts remain intact. Uncertain submissions, missing allocation receipts, and changed CRM product/color/quantity also stop for review. This fix does not change fulfillment on previously submitted SanMar orders. History retains both PO confirmation links and shows incomplete purchases as needing attention. The routing correction loads with the next worker run; no app restart, extension reload, or page refresh is required.

Shipping Bypasser keeps each tab's PO links with that tab; a partial order can still name another tab's purchases in its labeled summary. Production-note edits target the Production Notes field and stop for review if the field is ambiguous. NJ/ADD notes are checked after save and refresh for up to 12 seconds. If the note is missing, the worker refreshes and retries the note save once, preserving existing text and avoiding a duplicate note or repeated stock purchases. A second failed verification stops for review and captures a CRM error screenshot under `runtime/screenshots/`; the purchase receipts remain available for a later note-only recovery. The worker changes load on the next run. Restart the local app and refresh the dashboard to load the PO-link correction; no extension reload is needed.

## Runtime Files

The following are created or maintained locally and are ignored by Git:

- `config.py`
- `runtime/state/` for active JSON state and templates
- `runtime/results/` for latest and historical result JSON
- `runtime/screenshots/` for browser screenshots
- `runtime/logs/` for `server.log` and `automation_record_log.txt`
- `runtime/generated_profiles/` for temporary cloned browser profiles
- `runtime/browser_profiles/` for platform-specific browser sessions
- `runtime/debug/` for old backups, exports, temp files, and test artifacts
- browser profile folders such as `chrome_profile_crm/`
- driver downloads and cache folders

Keeping these files local prevents credentials, login sessions, audit history, and generated artifacts from being published.

## Testing

Use the existing virtual environment (`.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on macOS), or activate it before running these commands.

Run the regression suite with:

```powershell
python -m unittest discover -s tests
```

Run the extension's Node tests with:

```powershell
node --test tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

The extension and dashboard also have Node coverage for native editing/ordering,
completion refreshes, tab scans, queue reports/sorting, scheduling, stock history,
and work plans. Run every Node regression in PowerShell with:

```powershell
node --test (Get-ChildItem tests/test_*.mjs).FullName
```

On macOS, use `node --test tests/test_*.mjs`. For focused native-editor and stock
checks, see the [extension guide](crm-order-dark-mode-extension/README.md).
Queue persistence, pool isolation, scheduling, and recovery are covered by
`tests/test_queue_persistence.py`, `tests/test_queue_parallel.py`,
`tests/test_scheduled_repeat.py`, and `tests/test_queue_retry.py`.

Check CRM list selection, rush highlighting, tab opening, and the offline Chrome fixtures with:

```powershell
node --test tests/test_rush_list_highlight.mjs tests/test_crm_list_tabs.mjs
python -m unittest discover -s tests -p test_rush_list_browser.py
```

The browser check uses a local ChromeDriver (set `CHROMEDRIVER` if needed), an
isolated profile under `runtime/debug/`, and a loopback fixture server. It never
opens live CRM orders.

Some browser suites also require ignored local fixture assets, such as the
Angular fixture used by the native editors, or suite-specific driver variables.
Missing prerequisites cause skips; report them separately from passing tests.
Browser verification must use local fixtures and isolated profiles.

Verify Queue reports, sorting, task controls, and execution order with:

```powershell
node --test tests/test_queue_reports.mjs tests/test_queue_sort.mjs
```

Verify weekly schedule calculations, Half targets, and saved day selections with:

```powershell
node --test tests/test_work_plan.mjs
```

Verify combined scheduling, today-only time controls, and the M6 switch with:

```powershell
node --test tests/test_scheduling_controls.mjs
python -m unittest discover -s tests -p test_scheduled_repeat.py
python -m unittest discover -s tests -p test_m6_posting.py
```

The Stock Issue offline browser checks also verify Sales Note persistence,
note-only save recovery, and preservation of Production Notes. They open local
fixtures only (`python -m unittest discover -s tests -p test_stock_issue_browser.py`).

Verify Order Goods ordering-only dispatch, background tabs, repeat runs, and dry
runs with `python -m unittest discover -s tests -p 'test_crm_order_goods*.py'`.
The browser checks use a local ChromeDriver (`CHROMEDRIVER` can override it), an
isolated profile under `runtime/debug/`, and local fixtures only. They cover
concurrent order tabs, native submissions, locked-stock skips, ignored supplier
error messages, durable receipts, and dry runs. Legacy feedback/save-verification
checks cover the helpers retained for Push Back. The shared submission engine is
also checked with `node --test tests/test_order_all_batch.mjs`.

Verify the NJ/ADD history display with:

```powershell
node --test tests/test_nj_stock_history.mjs
```

Optional local browser verification for NJ pickup, distinct CRM POs, Add box selection, and persisted production notes uses `tests/fixtures/sanmar_nj_checkout.html`. Set `SANMAR_FIXTURE_CHROMEDRIVER` to a local ChromeDriver path (and `SANMAR_FIXTURE_CHROME` if Chrome is not in its usual location), then run `python -m unittest discover -s tests -p test_sanmar_nj_browser.py`. It uses an isolated headless profile under ignored runtime folders and never opens live vendor or CRM pages.

Auto Splitter's stock-race and recovery regressions run with `python -m unittest discover -s tests -p test_crm_splitter_stock_race.py`. To include its local empty-tab browser fixture, set `SPLITTER_FIXTURE_CHROMEDRIVER` to a local ChromeDriver path and optionally `SPLITTER_FIXTURE_CHROME` to the Chrome executable. It uses an isolated profile under `runtime/generated_profiles/` and opens only `tests/fixtures/crm_retained_order.html`.

You can also run a syntax compile pass:

```powershell
python -m compileall automation_audit.py automation_runtime.py server.py slack_message_rotation.py routes workers tests
```

## Notes

Login secrets are stored in Windows Credential Manager or macOS Keychain under the `WorkAutomation/*` targets. `config.py`, local template libraries, recovery receipts, and browser profiles can contain private operational data and active sessions; they remain ignored and should not be force-added to Git. Preserve recovery receipts when cleaning runtime data so confirmed purchases, refunds, emails, and messages cannot be repeated accidentally.

For contributor guidance, see [AGENTS.md](AGENTS.md). For deeper implementation details, see [the system guide](docs/AUTOMATION_SYSTEM_GUIDE.md) and [CRM edit/save runbook](docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md). Older planning documents may describe earlier versions; check current code and tests when implementing changes.
