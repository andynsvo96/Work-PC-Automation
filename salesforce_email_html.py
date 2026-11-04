"""Remove editor metadata from email HTML without reserializing its markup."""

from html.parser import HTMLParser
import re


_DATA_ATTRIBUTE_HINT = re.compile(r"\bdata-", re.IGNORECASE)
_TAG_OPEN = re.compile(r"<[^\s/>]+")
_ATTRIBUTE = re.compile(
    r'''\s*(?P<name>[^\s=/>]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?'''
)


class _EditorMetadata(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.line_offsets = [0, *(match.end() for match in re.finditer("\n", source))]
        self.removals = []

    def handle_starttag(self, tag, attrs):
        if not any(name.startswith("data-") for name, _value in attrs):
            return
        raw = self.get_starttag_text()
        line, column = self.getpos()
        offset = self.line_offsets[line - 1] + column
        for match in _ATTRIBUTE.finditer(raw, _TAG_OPEN.match(raw).end()):
            if match["name"].lower().startswith("data-"):
                start = match.start()
                # Retain a separator if pasted HTML omitted whitespace before the next attribute.
                if match.end() < len(raw) and raw[match.end()] not in " \t\r\n\f/>":
                    start = match.start("name")
                self.removals.append((offset + start, offset + match.end()))

    handle_startendtag = handle_starttag


def clean_email_html(source):
    """Strip data-* attributes from actual tags, preserving other HTML verbatim.

    Editor clipboard metadata such as data-pm-slice is unnecessary in an email.
    This does not validate all Salesforce HTML rules or replace the sender's
    active-content checks. Text, comments, styles, URLs, and placeholders retain
    their original spelling and formatting.
    """
    if not _DATA_ATTRIBUTE_HINT.search(source):
        return source
    parser = _EditorMetadata(source)
    parser.feed(source)
    parser.close()
    parts = []
    end = 0
    for start, stop in parser.removals:
        parts.append(source[end:start])
        end = stop
    parts.append(source[end:])
    return "".join(parts)
