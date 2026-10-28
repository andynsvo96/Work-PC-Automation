"""Load the Settings JSON template into the active Salesforce email composer."""

from html.parser import HTMLParser
import ipaddress
import re
from urllib.parse import urlsplit

from selenium.webdriver.support.ui import WebDriverWait

from salesforce_templates import default_template_store, TemplateLibraryError


class TemplateComposerError(RuntimeError):
    pass


class _EmailHtml(HTMLParser):
    """Reject active browser content before displaying user-editable email HTML."""
    blocked = {
        "script", "iframe", "frame", "frameset", "object", "embed", "applet",
        "base", "meta", "link", "form", "input", "button", "textarea", "select", "svg", "math",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        if tag in self.blocked:
            raise TemplateComposerError(f"Email HTML contains unsupported active content: <{tag}>.")
        if tag == "style":
            self.in_style = True
        for name, value in attrs:
            value = value or ""
            if name.startswith("on") or name in {"srcdoc", "autofocus", "contenteditable", "formaction"}:
                raise TemplateComposerError(f"Email HTML contains an unsupported {name} attribute.")
            if name == "style":
                self._check_css(value)
            if name == "href" and value and not _safe_link(value):
                raise TemplateComposerError("An email hyperlink has an unsupported URL.")
            if name == "src" and (tag != "img" or not _public_image(value)):
                raise TemplateComposerError("Email images must use public HTTPS URLs.")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    @staticmethod
    def _check_css(value):
        if re.search(r"url\s*\(|@import|expression\s*\(|behavior\s*:|-moz-binding|\\", value, re.I):
            raise TemplateComposerError("Email styles cannot load resources or execute browser content.")

    def handle_data(self, data):
        if self.in_style:
            self._check_css(data)


def _safe_link(value):
    value = value.strip()
    if re.fullmatch(r"\[[A-Z][A-Z0-9_-]*\]", value) or value.startswith("#"):
        return True
    value = re.sub(r"[\x00-\x20]", "", value)
    try:
        return urlsplit(value).scheme.lower() in {"https", "http", "mailto", "tel"}
    except ValueError:
        return False


def _public_image(value):
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if (url.scheme.lower() != "https" or url.username or url.password or "." not in host
                or re.search(r"(?:^|\.)(?:localhost|local|internal|lan|home)$", host)):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return (not host.startswith("[") and ":" not in host
                    and not re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host))
    except ValueError:
        return False


COMPOSER_SCRIPT = r"""
function walk(root, out=[]) {
  for (const node of root.querySelectorAll('*')) {
    out.push(node);
    if (node.shadowRoot) walk(node.shadowRoot, out);
  }
  return out;
}
function visible(node) {
  if (!node.getClientRects().length) return false;
  const style = node.ownerDocument.defaultView.getComputedStyle(node);
  return style.display !== 'none' && style.visibility !== 'hidden';
}
const fields = walk(document).filter(node => /^(INPUT|TEXTAREA)$/.test(node.tagName) && visible(node)
  && /subject/i.test([node.placeholder, node.name, node.getAttribute('aria-label')].join(' ')));
if (fields.length !== 1) return {error: 'Expected exactly one visible email subject field.'};
const subject = fields[0];
const composer = subject.closest('[role="dialog"],.slds-docked-composer,.modal-container');
if (!composer) return {error: 'The email subject is outside a Salesforce composer.'};
return {subject, frames: walk(composer).filter(node => node.tagName === 'IFRAME' && visible(node))};
"""


EDITOR_SCRIPT = r"""
const editors = Object.values((window.CKEDITOR && window.CKEDITOR.instances) || {})
  .filter(editor => editor.status === 'ready' && !editor.readOnly && editor.container
    && editor.container.$.getClientRects().length);
return editors.map(editor => ({name: editor.name, mode: editor.mode, source: !!editor.commands.source}));
"""


VERIFY_SCRIPT = r"""
const editor = window.CKEDITOR.instances[arguments[0]];
const expected = new DOMParser().parseFromString(arguments[1], 'text/html');
const actual = editor.document.$;
const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
function text(root) {
  if (root.nodeType === 3) return root.nodeValue;
  if (root.nodeType === 1 && /^(SCRIPT|STYLE)$/.test(root.tagName)) return '';
  const parts = Array.from(root.childNodes).map(text).join('');
  return root.nodeType === 1 && /^(P|DIV|LI|OL|UL|BR|BODY|TABLE|TR|TD|H[1-6])$/.test(root.tagName)
    ? ' ' + parts + ' ' : parts;
}
const links = doc => Array.from(doc.body.querySelectorAll('a')).map(node =>
  [node.getAttribute('href'), clean(node.textContent)]);
const images = doc => Array.from(doc.body.querySelectorAll('img')).map(node =>
  [node.getAttribute('src'), node.getAttribute('alt') || '']);
const bold = doc => Array.from(doc.body.querySelectorAll('b,strong')).map(node => clean(node.textContent));
const counts = doc => ['ol','ul','li'].map(tag => doc.body.querySelectorAll(tag).length);
return {
  text_matches: clean(text(expected.body)) === clean(text(actual.body)),
  links_match: JSON.stringify(links(expected)) === JSON.stringify(links(actual)),
  images_match: JSON.stringify(images(expected)) === JSON.stringify(images(actual)),
  bold_match: JSON.stringify(bold(expected)) === JSON.stringify(bold(actual)),
  lists_match: JSON.stringify(counts(expected)) === JSON.stringify(counts(actual)),
  body: clean(actual.body.innerText || actual.body.textContent), body_html: editor.getData()
};
"""


def _enter_editor(driver, frames, timeout):
    """Inspect only frames owned by the one visible composer, including shadow DOM."""
    for frame in frames:
        title = frame.get_attribute("title")
        driver.switch_to.frame(frame)
        if title == "CK Editor Container":
            editors = WebDriverWait(driver, timeout).until(lambda browser: browser.execute_script(EDITOR_SCRIPT))
        else:
            WebDriverWait(driver, timeout).until(lambda browser: browser.execute_script("return document.readyState === 'complete';"))
            editors = driver.execute_script(EDITOR_SCRIPT)
        if len(editors) == 1:
            return editors[0]
        if len(editors) > 1:
            raise TemplateComposerError("The email composer contains multiple editable HTML editors.")
        driver.switch_to.parent_frame()
    raise TemplateComposerError("The Salesforce email HTML editor was not found in the active composer.")


def fill_template(driver, reference, *, store=None, timeout=15):
    """Read the current local template, paste its subject/HTML, and verify the draft.

    No template picker, clipboard, Salesforce save, or Send action is used here.
    Workflow-specific placeholder replacement and send receipts remain with callers.
    """
    try:
        template = (store or default_template_store()).get_template(reference)
    except (TemplateLibraryError, OSError) as exc:
        raise TemplateComposerError(f"Could not load the local Salesforce template: {exc}") from exc
    _EmailHtml().feed(template["body_html"])
    try:
        driver.switch_to.default_content()
        composer = driver.execute_script(COMPOSER_SCRIPT) or {}
        if composer.get("error") or not composer.get("subject"):
            raise TemplateComposerError(composer.get("error") or "The email composer was not found.")
        subject = composer["subject"]
        driver.execute_script(r"""
          const field=arguments[0],value=arguments[1],view=field.ownerDocument.defaultView;
          const proto=field.tagName==='TEXTAREA'?view.HTMLTextAreaElement.prototype:view.HTMLInputElement.prototype;
          field.focus();Object.getOwnPropertyDescriptor(proto,'value').set.call(field,value);
          field.dispatchEvent(new view.InputEvent('input',{bubbles:true,inputType:'insertFromPaste',data:value}));
          field.dispatchEvent(new view.Event('change',{bubbles:true}));field.blur();
        """, subject, template["subject"])
        if subject.get_attribute("value") != template["subject"]:
            raise TemplateComposerError("The local template title did not remain in the email subject.")
        editor = _enter_editor(driver, composer.get("frames", []), timeout)
        name = editor["name"]
        if not editor["source"]:
            raise TemplateComposerError("The Salesforce email editor does not support HTML source mode.")
        driver.execute_script("const e=window.CKEDITOR.instances[arguments[0]];if(e.mode!=='source')e.execCommand('source');", name)
        wait = WebDriverWait(driver, timeout)
        wait.until(lambda browser: browser.execute_script(
            "const e=window.CKEDITOR.instances[arguments[0]];return e.mode==='source' && !!e.container.$.querySelector('textarea.cke_source');", name))
        exact = driver.execute_script(r"""
          const e=window.CKEDITOR.instances[arguments[0]],source=e.container.$.querySelector('textarea.cke_source');
          const value=arguments[1];
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(source,value);
          source.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertFromPaste',data:value}));
          source.dispatchEvent(new Event('change',{bubbles:true}));return source.value===value;
        """, name, template["body_html"])
        if not exact:
            raise TemplateComposerError("The email HTML source paste did not match the local JSON.")
        driver.execute_script("window.CKEDITOR.instances[arguments[0]].execCommand('source');", name)
        wait.until(lambda browser: browser.execute_script(
            "const e=window.CKEDITOR.instances[arguments[0]];return e.mode==='wysiwyg' && !!e.document;", name))
        driver.execute_script("const e=window.CKEDITOR.instances[arguments[0]];e.updateElement();e.fire('change');e.fire('blur');", name)
        verified = driver.execute_script(VERIFY_SCRIPT, name, template["body_html"]) or {}
        checks = ("text_matches", "links_match", "images_match", "bold_match", "lists_match")
        if not all(verified.get(key) for key in checks) or not verified.get("body"):
            failed = ", ".join(key for key in checks if not verified.get(key))
            raise TemplateComposerError(f"The pasted email HTML could not be verified ({failed or 'empty body'}).")
        return {"source": "local_json", "template": template["name"], "template_id": template["id"],
                "revision": template["revision"], "state": {"subject": template["subject"],
                "body": verified["body"], "body_html": verified["body_html"]}}
    except TemplateComposerError:
        raise
    except Exception as exc:
        raise TemplateComposerError(f"Could not paste the local Salesforce HTML template: {exc}") from exc
    finally:
        driver.switch_to.default_content()
