"""Text extraction from PDF files, one string per page.

Runs in worker processes during import, so everything here is a plain function
that takes a path and returns picklable data.
"""
import hashlib
import re

import pymupdf

from ..ocr import needs_ocr, page_lines

# CJK characters (incl. full-width punctuation): line breaks between them are
# layout artefacts, not word boundaries, and must be removed or phrases that wrap
# across lines become unsearchable.
_CJK = r"　-〿㐀-䶿一-鿿豈-﫿＀-￯"
_CJK_BREAK = re.compile(rf"(?<=[{_CJK}])\s*\n\s*(?=[{_CJK}])")
_HYPHEN_BREAK = re.compile(r"(?<=[A-Za-z])-\n(?=[a-z])")
_SPACE = re.compile(r"\s+")


def normalize(text):
    """Join lines the way a reader would: drop breaks inside CJK text and
    hyphenated English words, turn other breaks into spaces."""
    text = text.replace("\r", "\n").replace("\x00", "")
    text = _HYPHEN_BREAK.sub("", text)
    text = _CJK_BREAK.sub("", text)
    return _SPACE.sub(" ", text).strip()


def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def extract(path, ocr="auto"):
    """Return dict(path, sha256, title, pages=[str, ...], ocr_pages=int, error=None|str).

    ocr: "auto" OCRs pages that have no text layer (scans) when Tesseract is
    installed; "off" skips OCR; "force" OCRs every page.
    """
    out = dict(path=path, sha256=None, title="", pages=[], ocr_pages=0, error=None)
    try:
        out["sha256"] = file_sha256(path)
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                out["error"] = "encrypted"
                return out
            out["title"] = (doc.metadata or {}).get("title") or ""
            for i, page in enumerate(doc, 1):
                text = page.get_text("text")
                if ocr == "force" or (ocr == "auto" and needs_ocr(page, len(text.strip()))):
                    lines, _, used = page_lines(page, i, ocr)
                    if used:
                        text = "\n".join(l.text for l in lines)
                        out["ocr_pages"] += 1
                out["pages"].append(normalize(text))
    except Exception as e:  # corrupt or non-PDF file: record it, keep importing
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out
