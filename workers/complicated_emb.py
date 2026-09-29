"""Selection validation and live design-name resolution for Complicated EMB."""
from pathlib import Path
import re
import time


def normalize_designs(designs):
    if not isinstance(designs, list) or not designs or len(designs) > 100:
        raise ValueError("Select at least one embroidery design tab.")
    result = []
    seen = set()
    for row in designs:
        if not isinstance(row, dict):
            raise ValueError("Invalid embroidery design selection.")
        number = row.get("tab_number")
        name = " ".join(str(row.get("design_name") or "").split())
        if type(number) is not int or not 1 <= number <= 1000 or not name or len(name) > 500:
            raise ValueError("Each embroidery selection needs a valid tab number and Design Name.")
        if re.search(r"\[\s*(?:DESIGN|REASON|ORDER-NUMBER)\s*\]|XXXXXX", name, re.I):
            raise ValueError("Design Name contains an unresolved email placeholder.")
        if number in seen:
            raise ValueError("Select each embroidery tab only once.")
        seen.add(number)
        result.append({"tab_number": number, "design_name": name})
    return sorted(result, key=lambda row: row["tab_number"])


def resolve_designs(driver, selections=None):
    import crm_auto_splitter as splitter

    script = (Path(__file__).resolve().parents[1] / "crm-order-dark-mode-extension/emb-design-reader.js").read_text(encoding="utf-8")
    methods = driver.execute_script(script + "\nreturn readEmbDesignMethods();") or []
    eligible = {row["tab_number"] for row in methods if row.get("eligible")}
    if selections is None:
        if len(eligible) != 1:
            raise ValueError("Choose the applicable embroidery design tabs in Reachout > Complicated EMB.")
        selections = [{"tab_number": next(iter(eligible)), "design_name": ""}]
    else:
        selections = normalize_designs(selections)
    resolved = []
    for selected in selections:
        number = selected["tab_number"]
        if number not in eligible:
            raise ValueError(f"Tab {number} is no longer an embroidery-only design.")
        if not splitter._click_design_tab(driver, number):
            raise ValueError(f"Could not open embroidery tab {number}.")
        time.sleep(0.75)
        previous = ""
        name = ""
        for _ in range(30):
            name = splitter._scan_current_design_detail(driver, number).get("design_name", "")
            if name and name == previous:
                break
            previous = name
            name = ""
            time.sleep(0.2)
        if not name:
            raise ValueError(f"Could not read Design Name for embroidery tab {number}.")
        if selected["design_name"] and name != selected["design_name"]:
            raise ValueError(f"Design Name changed for tab {number}. Reopen Complicated EMB and select the designs again.")
        resolved.append({"tab_number": number, "design_name": name})
    return normalize_designs(resolved)


def design_text(designs):
    names = list(dict.fromkeys(row["design_name"] for row in normalize_designs(designs)))
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def replace_design_placeholder(driver, replacement):
    # Work on text nodes, including tokens split by rich-text formatting. Range
    # insertion treats names as text, preserving links and surrounding HTML.
    return driver.execute_script(r"""
      const replacement = String(arguments[0]);
      let count = 0;
      const seen = new Set();
      function replace(root) {
        if (!root || seen.has(root)) return;
        seen.add(root);
        const doc = root.ownerDocument;
        const walker = doc.createTreeWalker(root, 4);
        const nodes = [];
        let text = '';
        while (walker.nextNode()) {
          const node = walker.currentNode;
          nodes.push({node, start: text.length, end: text.length + node.data.length});
          text += node.data;
        }
        for (const match of Array.from(text.matchAll(/\[\s*DESIGN\s*\]/gi)).reverse()) {
          const start = match.index, end = start + match[0].length;
          const first = nodes.find(item => item.end > start);
          const last = nodes.find(item => item.end >= end);
          if (!first || !last) continue;
          const range = doc.createRange();
          range.setStart(first.node, start - first.start);
          range.setEnd(last.node, end - last.start);
          range.deleteContents();
          range.insertNode(doc.createTextNode(replacement));
          count++;
        }
        root.dispatchEvent(new Event('input', {bubbles: true}));
        root.dispatchEvent(new Event('change', {bubbles: true}));
      }
      function walk(root) {
        for (const el of root.querySelectorAll('*')) {
          if (el.shadowRoot) walk(el.shadowRoot);
          if (el.matches('[contenteditable="true"], [role="textbox"]') && el.getBoundingClientRect().width) replace(el);
          if (el.tagName === 'IFRAME' && el.getBoundingClientRect().width) {
            try { inspect(el.contentDocument); } catch (_) {}
          }
          if (el.tagName === 'TEXTAREA' && el.getBoundingClientRect().width && /\[\s*DESIGN\s*\]/i.test(el.value)) {
            el.value = el.value.replace(/\[\s*DESIGN\s*\]/gi, () => { count++; return replacement; });
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }
      }
      const documents = new Set();
      function inspect(doc) {
        if (!doc || documents.has(doc)) return;
        documents.add(doc);
        for (const editor of Object.values(doc.defaultView.CKEDITOR?.instances || {})) {
          const element = editor.editable?.()?.$;
          if (element) {
            replace(element);
            editor.updateElement?.();
            editor.fire?.('change');
          }
        }
        if (doc.designMode === 'on') replace(doc.body);
        walk(doc);
      }
      inspect(document);
      return count;
    """, replacement)
