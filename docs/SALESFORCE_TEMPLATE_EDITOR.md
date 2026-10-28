# Salesforce template editor

Open **Settings > Salesforce Template**, then choose an email from the dropdown.
Edit its **Email template name**, **Title** (email subject), and **Body**.

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

IDs remain stable when names change. Unknown per-template metadata is retained.
Files are written atomically, and a prior version is retained before a changed
template is saved. The files stay machine-local and ignored by Git. To use the
same library on another computer, copy the active JSON file there while it is
not being edited. Windows and macOS use the same relative location and format.

The editor reads and writes through the dashboard's existing PIN, CSRF, and
version-lock protections. It does not edit Salesforce or send email. Existing
workers continue to select templates in Salesforce; connecting the local library
to sending workflows is a separate migration.

Restart the local app and refresh the dashboard after installing this change.
No Chrome extension reload is required.

## Validation

Focused storage/API checks:

    python -m unittest discover -s tests -p test_salesforce_templates.py

The browser fixture starts its own temporary Flask server and library, uses
the actual editor markup and assets, and never opens Salesforce or customer
orders:

    python -m unittest discover -s tests -p test_salesforce_template_editor_browser.py

It checks source preservation, saving and reloading, formatting, links, images,
draft conflicts, responsive layout, and blocked script/local-endpoint execution.
If needed, set SALESFORCE_EDITOR_CHROMEDRIVER to a local ChromeDriver path and
SALESFORCE_EDITOR_CHROME to the Chrome executable. Profiles, screenshots, and
test artifacts live under runtime/debug/salesforce_template_editor_tests.
