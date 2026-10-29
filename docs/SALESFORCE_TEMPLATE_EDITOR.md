# Salesforce template editor

Open **Settings > Salesforce Template**, then choose an email from the dropdown.
Edit its **Email template name**, **Title** (email subject), and **Body**.

Use **Add** to open a new template draft. **Duplicate** copies the currently
displayed name, title, and HTML body into a new draft with a unique copy name;
this includes unsaved edits. Review it and select **Create template** to save.
The original saved template stays unchanged. Copies receive a new ID and do not
inherit the original template's previous-name references. **Cancel** discards the
new draft and returns to the saved library.

**Remove** confirms the selected saved template before deleting its local
record. If the editor has unsaved changes, the confirmation also explains that
they will be discarded. Automations that reference a removed template stop for
review until it is replaced. The previous library remains in the backup file,
and original captures are untouched. Removing the final template leaves an
empty library; use **Add** to create one again. Reload does not restore removed
templates from the capture.

New templates are available in this local library; adding or duplicating one
does not create a new automation or change an existing automation's configured
template reference. No email is sent by these controls.

Use the single **HTML** button to switch between formatted editing and HTML
source. The button is highlighted while HTML source is active; click it again
to return to the formatted view.
The toolbar supports font family, text size, bold, italic, underline, text color,
numbered and bulleted lists, indentation, alignment, links, unlinking, images,
and clearing formatting. Select text in the body before formatting it.
The Image control accepts a public HTTPS image URL, keeping the image hosted
with Salesforce or another public image host. Link URLs can also use a template
placeholder such as [INVOICE_LINK].

Select **Save template** or press Ctrl+S / Cmd+S while editing. This editor uses
explicit saves. Switching templates or reloading asks before discarding a draft.
If another tab or a manual JSON edit changes the same template, Save preserves
the open draft and asks you to reload and review the latest version.

The formatted view uses a separate frame. It does not execute template scripts,
submit forms, navigate links, or fetch application endpoints. Source is preserved
exactly when you switch views or edit only the name/title. Editing the formatted
body serializes the body markup; browsers can normalize entities and formatting
tags during those edits. The existing document wrapper is retained.

Keep placeholders such as [ORDER-NUMBER], [ORDER_NUMBER], [DESIGN], [PRINT],
[REQUEST], [COST], and [INVOICE_LINK]. Existing image URLs and link attributes
remain in the source. The two captured embroidery-guide anchors without href
remain unchanged until they are explicitly edited.

## Local files

The active library is:

    runtime/state/salesforce_templates.json

Its previous saved version is:

    runtime/state/salesforce_templates.json.bak

On first use, if the active file does not exist, the app copies templates whose
names start with [AUTO] from the latest local capture:

    runtime/state/salesforce_email_templates_capture/<capture-date>/email_templates.json

The captured files remain a source backup. A malformed active library is reported
as an error and is never silently replaced from the capture. On a computer with
no active library or captures, the editor shows an empty list.

The active file uses this structure:

    {
      "schema_version": 1,
      "templates": [
        {
          "id": "stable_template_id",
          "name": "[AUTO] Example",
          "subject": "Order #[ORDER-NUMBER]",
          "body_html": "<p>Hello,</p>"
        }
      ]
    }

IDs remain stable when names change. Previous names are retained in
`previous_names`, so a worker configured with the original template name keeps
using the same record after a rename. A name reserved by another template's
history cannot be reused. Unknown per-template metadata is retained.
Files are written atomically, and a prior version is retained before a changed
template is saved, created, or removed. Duplicate names (including names retained
after a rename) are rejected. Removal requires the revision that was opened, so
another tab's newer changes cannot be deleted accidentally. The library holds up
to 500 templates. The files stay machine-local and ignored by Git. To use the
same library on another computer, copy the active JSON file there while it is
not being edited. Windows and macOS use the same relative location and format.

The editor reads and writes through the dashboard's existing PIN, CSRF, and
version-lock protections. Saving here updates the local library; it does not edit
the original Salesforce templates or send email.

## Automated emails

All 16 automation templates now load from this same JSON library: full and
partial Copyright Cancel, Content Violation, Existing T-shirt, Outside Limit,
Unresponsive, Copyright Reachout and Removal, both Complicated Embroidery routes,
Oversize Embroidery, Additional Requests, Comp Sleeves, and Stock Extension,
Color, and Size. Sleeve, side, reverse-print, and extra embroidery requests share
the Additional Requests sender. Sheets Scanner, queue actions, direct single-order
actions, and email recovery use the same sender paths.

Once the customer account and Orders sender are verified, the worker fills the
subject and switches the composer editor to HTML source mode. It inserts the
saved HTML in one operation, returns to formatted mode, and verifies the text,
links, images, bold text, and lists before applying the workflow's placeholders.
It does not open or search the Salesforce template picker. The send composer
does not show the template editor's Source toolbar button; its CKEditor source
command provides the same HTML mode. No operating-system clipboard is needed.

Placeholder replacements retain their surrounding bold, font, and size markup.
The Additional Requests invoice placeholder becomes a clickable link when it is
body text, and can also be used in an anchor's `href` with a label such as
"Pay invoice". Stock Color/Size grammar changes preserve the product name's
formatting.

Workers resolve an exact configured template name (case-insensitive), a retained
previous name, or an exact stable ID. They read the latest saved content for each
draft. A missing, corrupt, ambiguous, or unsupported local template stops the
email step for review; there is no fallback to a Salesforce template or an older
plain-text library. Keep each workflow's required placeholders and identifying
wording when editing its body/title. Images must use public HTTPS URLs. Script,
form, embedded-frame, event-handler, and resource-loading CSS content is rejected
before it enters the browser.

Existing CRM save verification, recipient/From checks, Activity confirmation,
bounded resend checks, workflow receipts, and queue locks remain in effect.
An edited template does not reset a send receipt or automatically resend an
already-confirmed email. A retry whose rebuilt subject/body differs from the
first draft stops before another send. Review an uncertain send in Salesforce
Activity using its original queue entry and receipts.

Copy the active JSON library to each computer that runs email workers. Missing
libraries are initialized only from captures already present on that computer;
there is no implicit cross-machine lookup or execution.

Restart the local app and refresh the dashboard after installing this change.
No Chrome extension reload is required.

## Validation

Focused storage/API checks:

    python -m unittest discover -s tests -p test_salesforce_templates.py

Worker loading, every template route, saved HTML state, and blocked active content:

    python -m unittest discover -s tests -p test_salesforce_template_composer.py

Send confirmation and recovery guards:

    python -m unittest discover -s tests -p test_salesforce_email_workflows.py

The browser fixture starts its own temporary Flask server and library, uses
the actual editor markup and assets, and never opens Salesforce or customer
orders:

    python -m unittest discover -s tests -p test_salesforce_template_editor_browser.py

It checks source preservation, saving and reloading, formatting, links, images,
adding, duplicating unsaved content, removal/cancel confirmation, empty-library
recovery, draft conflicts, responsive layout, and blocked script/local-endpoint
execution. Storage/API tests also check name/history collisions, backup recovery,
failed writes, and the existing PIN/CSRF/version protections for create/remove.
If needed, set SALESFORCE_EDITOR_CHROMEDRIVER to a local ChromeDriver path and
SALESFORCE_EDITOR_CHROME to the Chrome executable. Profiles, screenshots, and
test artifacts live under runtime/debug/salesforce_template_editor_tests.
