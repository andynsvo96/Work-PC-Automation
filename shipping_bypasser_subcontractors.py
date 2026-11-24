"""Reviewed CRM/SanMar destinations and Production Notes shipping deadlines.

These are explicit aliases, not fuzzy purchase-time matches. Sew Nice and Print
Works intentionally use the SanMar locations confirmed by the user.
"""

import re
from datetime import date, datetime


# CRM name, SanMar saved name, street, suite/unit, city, state, ZIP.
_APPROVED_DESTINATIONS = (
    ("APOLLO USA", "Apollo - USA", "7895 Airport Hwy", "", "Pennsauken", "NJ", "08109-4322"),
    ("ATHENS PRINT SHOP LLC", "Athens Print Shop LLC", "3450 Jefferson Rd", "", "Athens", "GA", "30607-1453"),
    ("AXELRAD SCREEN PRINTING", "AxelRad Screen Printing", "1019 N Washington St", "", "Wilkes Barre", "PA", "18705-1816"),
    ("BLACK BEAR CUSTOMS LLC", "BLACK BEAR CUSTOMS, LLC", "3315 George Washington Hwy", "", "Grafton", "WV", "26354-8242"),
    ("CEDARSTREAM", "CedarStream", "201 At and T Way", "", "Cedartown", "GA", "30125-6015"),
    ("Central Apollo", "APOLLO - IL", "1401 Estes Ave", "", "Elk Grove Village", "IL", "60007-5405"),
    ("CLUB SHUE", "Club Shue", "34 Okner Pkwy", "", "Livingston", "NJ", "07039-1604"),
    ("COSMIC PRINTING", "Cosmic Printing", "213 Skyline Dr", "Suite 240", "East Stroudsburg", "PA", "18301-1353"),
    ("DC APPAREL INC", "DC Apparel, Inc.", "2021 Executive Rd", "", "Winter Haven", "FL", "33884-1113"),
    ("DESIGNER T'S", "Designer Ts Inc", "20 Creek Rd", "", "Brooklawn", "NJ", "08030-2748"),
    ("DESTROYER GRAPHICS LLC", "Destroyer Graphicks", "1635 Farm Way", "Unit 405", "Middleburg", "FL", "32068"),
    ("ECLIPSE PRINTING", "Eclipse Printing", "N60 W 14423 Kaul Ave", "", "Menomonee Falls", "WI", "53051"),
    ("ELEVATION SPORTSWEAR", "Elevation Sportswear", "3445 William Richardson Dr", "", "South Bend", "IN", "46628-9477"),
    ("FIRST TOUCH", "First Touch Screen Printing", "730 W 9th St", "", "Chico", "CA", "95928-5137"),
    ("Futuristic Innovative Graphics", "Futuristic Innovative Graphi", "156 S Pennsylvania Ave", "", "Wilkes Barre", "PA", "18701-3300"),
    ("GREEN MT MONOGRAM INC", "Green Mt Monogram Inc", "15 MILL ST", "", "WELLS RIVER", "VT", "05081-3008"),
    ("IMAGINE THAT", "Imagine That", "3278 Glassboro Cross Keys Rd", "", "Glassboro", "NJ", "08028-2716"),
    ("Inklings Custom Screen Printing", "Inklings Custom Screen Print", "2230 W 1st St", "Ste E", "Loveland", "CO", "80537-4132"),
    ("JOHNNY D TEES LLC", "Johnny D Tees, LLC", "216 Lake Ave", "", "Lake Villa", "IL", "60046-9150"),
    ("LOGO EXPRESS", "Logo Express", "307 Oxmoor Rd", "", "Homewood", "AL", "35209-6115"),
    ("MAGIC SCREEN PRINTING", "Magic Screen Printing", "250 Esten Ave", "Ste B4", "Pawtucket", "RI", "02860-4865"),
    ("MONSTER KOLOR", "Monster Kolor", "15 Sproat St", "", "Middleboro", "MA", "02346-2228"),
    ("Mountain State Designs", "Mountain State Designs", "26 Iron Drive", "", "Belington", "WV", "26250"),
    ("OLD VEGAS PRINT SHOP", "Old Vegas Print Shop", "6295 Harrison Dr", "Ste 29", "Las Vegas", "NV", "89120-4058"),
    ("PACIFIC IMPRESSIONS", "Pacific Impressions", "3494 Edward Ave", "", "Santa Clara", "CA", "95054-2130"),
    ("PAN ECTOR INDUSTRIES", "Pan Ector Industries", "1017 Shady Oaks Dr", "", "Denton", "TX", "76205-7981"),
    ("POLLCHAPS", "Pollchaps", "104 Brooks Dr", "", "Brandon", "MS", "39042-8679"),
    ("PRINT WORKS", "Print Works", "6955 Lamar Rd", "", "Reno", "TX", "75462-7113"),
    ("PROSTAR INDUSTRIES INC", "Prostar Industries, INC", "1520 N Kelsey St", "", "Visalia", "CA", "93291-9255"),
    ("REDSAND GRAPHICS AND PRINTING", "Redsand Graphics", "3950 Las Vegas Blvd N", "Ste#104.", "Las Vegas", "NV", "89115-0598"),
    ("REDWALL", "Redwall Prints", "7655 S 6th St", "", "Oak Creek", "WI", "53154-2019"),
    ("ROGERS SCREEN PRINTING", "Rogers Screen Printing", "10306 NC Highway 41 S", "", "Fairmont", "NC", "28340-5934"),
    ("SEW NICE EMBROIDERY & GIFTS", "Sew Nice", "1390 County Road 12", "", "Odenville", "AL", "35120-3521"),
    ("SHOWBIZ", "Showbiz Screen Printing", "2001 16th Ave SW", "", "Cedar Rapids", "IA", "52404-1611"),
    ("SOUTH APOLLO", "Apollo Dallas", "2700 Story Rd W Ste 180", "", "Irving", "TX", "75038-5258"),
    ("STILLWATER SCREENPRINTING", "STILLWATER SCREENPRINTING", "114 E 32nd Ave", "Ste 5", "Stillwater", "OK", "74074-7544"),
    ("TA WEAR", "TA Wear", "1533 GEORGIA RD", "", "WETUMPKA", "AL", "36092"),
    ("TALK SHIRTY TO ME", "TALK SHIRTY TO ME", "860 Enterprise St", "", "Dickson City", "PA", "18519-1568"),
    ("TFORMATION", "T Formation of Tallahassee", "864 Commerce Blvd", "", "Midway", "FL", "32343-6618"),
    ("Think Ink", "Think Ink and Thread LLC", "250 Cal Ln", "Ste C", "Sparks", "NV", "89431-6266"),
    ("THRIVE SCREENPRINTING", "Thrives Screenprinting", "3050 S 44th St", "Ste 130", "Phoenix", "AZ", "85040-1704"),
    ("WAVE GRAPHICS INC.", "WAVE GRAPHICS INC", "320 N 2nd St", "", "Mattoon", "IL", "61938-4452"),
    ("WILD CAT TEES LLC", "Wild Cat Tees", "1021 Scituate Harbour", "", "Pasadena", "MD", "21122-6517"),
    ("WRECKLESS PRESS", "Wreckless Press", "1115F Industrial Pkwy", "", "Brick", "NJ", "08724-2507"),
    ("WSA PROMOTIONS LLC", "WSA Promotions, LLC", "520 Franklin St", "", "Center Point", "IA", "52213-9388"),
)


def _name_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


SUBCONTRACTOR_DESTINATIONS = {
    "subcontractor:" + _name_key(row[0]): dict(zip(
        ("crm_name", "name", "line1", "line2", "city", "state", "zip"), row,
    ))
    for row in _APPROVED_DESTINATIONS
}


def subcontractor_destination(order_type):
    return SUBCONTRACTOR_DESTINATIONS.get(order_type)


def subcontractor_order_type(crm_name):
    text = str(crm_name or "").strip()
    suffix = re.search(r"\s*\(([A-Za-z]{2})\)\s*$", text)
    if suffix:
        text = text[:suffix.start()].strip()
    order_type = "subcontractor:" + _name_key(text)
    destination = subcontractor_destination(order_type)
    if not destination or (suffix and suffix.group(1).upper() != destination["state"]):
        return None
    return order_type


def subcontractor_warehouse_priorities(warehouses):
    """Regional preferences; actual checkout ETAs still decide eligibility."""
    preferred = {
        "NJ PA RI MA VT MD": ("NJ", "VA", "OH", "MN", "FL", "TX", "AZ", "NV", "WA"),
        "NC WV": ("VA", "NJ", "OH", "FL", "MN", "TX", "AZ", "NV", "WA"),
        "GA FL AL MS": ("FL", "VA", "OH", "TX", "NJ", "MN", "AZ", "NV", "WA"),
        "IL IN": ("OH", "MN", "VA", "NJ", "TX", "FL", "AZ", "NV", "WA"),
        "WI IA": ("MN", "OH", "TX", "NJ", "VA", "FL", "AZ", "NV", "WA"),
        "TX OK": ("TX", "OH", "FL", "MN", "AZ", "VA", "NJ", "NV", "WA"),
        "CO": ("TX", "AZ", "NV", "MN", "OH", "WA", "FL", "VA", "NJ"),
        "CA NV": ("NV", "AZ", "WA", "TX", "MN", "OH", "FL", "VA", "NJ"),
        "AZ": ("AZ", "NV", "TX", "WA", "MN", "OH", "FL", "VA", "NJ"),
    }
    by_state = {state: priority for states, priority in preferred.items() for state in states.split()}
    by_state_code = {name.rsplit(", ", 1)[-1]: name for name in warehouses}
    return {
        order_type: [by_state_code[state] for state in by_state[destination["state"]]]
        for order_type, destination in SUBCONTRACTOR_DESTINATIONS.items()
    }


def _note_timestamp(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", text, flags=re.I)
    for pattern in ("%b %d, %Y @ %I:%M %p", "%B %d, %Y @ %I:%M %p"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    raise ValueError("A LATEST SHIP DATE note has an unreadable timestamp; its newest value cannot be confirmed.")


def latest_ship_deadline(notes):
    """Read only saved, timestamped Production Notes supplied by the CRM reader.

    A newer malformed deadline must never silently fall back to an older note.
    Dates use their note's year, except January in a December note uses next year.
    """
    candidates = []
    for note in notes if isinstance(notes, list) else []:
        if not isinstance(note, dict):
            continue
        text = str(note.get("text") or "")
        if re.search(r"\bLATEST\s+SHIP\s+DATE\b", text, flags=re.I):
            candidates.append((_note_timestamp(note.get("timestamp")), text))
    if not candidates:
        raise ValueError("No saved LATEST SHIP DATE was found in Production Notes.")
    newest = max(timestamp for timestamp, _text in candidates)
    deadlines = set()
    for timestamp, text in candidates:
        if timestamp != newest:
            continue
        for marker in re.finditer(r"\bLATEST\s+SHIP\s+DATE\b", text, flags=re.I):
            match = re.match(r"[\s:*\-\u2013\u2014]*(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{4}|\d{2}))?(?![\d/])", text[marker.end():])
            if not match:
                raise ValueError("The newest LATEST SHIP DATE note has an unreadable date.")
            month, day = int(match[1]), int(match[2])
            year = int(match[3]) if match[3] else timestamp.year + int(timestamp.month == 12 and month == 1)
            if year < 100:
                year += 2000
            try:
                deadline = date(year, month, day)
            except ValueError as exc:
                raise ValueError("The newest LATEST SHIP DATE note has an invalid date.") from exc
            if deadline < timestamp.date():
                raise ValueError("The newest LATEST SHIP DATE predates its note; review the deadline year/date.")
            deadlines.add(deadline)
    if len(deadlines) != 1:
        raise ValueError("The newest LATEST SHIP DATE notes contain conflicting dates.")
    return deadlines.pop()
