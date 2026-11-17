"""Pick an installed CJK-capable font for PyMuPDF text insertion."""
import os

CANDIDATES = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]


def cjk_font():
    """Return (fontname, fontfile) for page.insert_text; falls back to PyMuPDF's built-in CJK font."""
    for f in CANDIDATES:
        if os.path.exists(f):
            return "cjkfont", f
    return "china-t", None
