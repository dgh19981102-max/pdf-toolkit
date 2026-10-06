"""Page text as lines, from the text layer or, for scanned pages, by OCR.

Shared by keyword search (to make scanned PDFs searchable) and invoice extraction.

Text pages: words with coordinates from PyMuPDF, grouped into visual lines.
Scanned pages: rendered at 300 dpi, deskewed, then OCR'd with Tesseract
(chi_sim+eng); per-word confidence is kept so low-quality pages can be flagged.
"""
import csv
import io
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image, ImageFilter, ImageOps

MIN_TEXT_CHARS = 30          # fewer extractable characters than this: treat page as scanned
OCR_DPI = 300
OCR_LANG = "chi_sim+eng"
# One thread per Tesseract process: files are already OCR'd in parallel by a process
# pool, and Tesseract's own OpenMP threads oversubscribe the CPU (10x slower).
_TESS_ENV = dict(os.environ, OMP_THREAD_LIMIT="1")

_CJK = "\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}]) +(?=[{_CJK}])")


@dataclass
class Line:
    text: str
    page: int
    y: float               # top of line, page units (for ordering)
    conf: float = 100.0    # OCR confidence 0-100 (100 for text-layer PDFs)


def _group_words(words, page_no, tol=3.0):
    """words: (x0, y0, x1, y1, text) -> Lines, merging words whose vertical centres align."""
    rows = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        if rows and abs(rows[-1]["yc"] - yc) <= tol:
            rows[-1]["words"].append(w)
        else:
            rows.append(dict(yc=yc, words=[w]))
    out = []
    for r in rows:
        ws = sorted(r["words"], key=lambda w: w[0])
        out.append(Line(" ".join(w[4] for w in ws), page_no, min(w[1] for w in ws)))
    return out


# ----------------------------------------------------------------------------- OCR
def ocr_available():
    return shutil.which("tesseract") is not None


def _deskew(gray):
    """Estimate skew by maximising the variance of the horizontal ink profile, then
    rotate it away. Text rows give sharp peaks only when they are level."""
    small = gray.resize((gray.width // 4, gray.height // 4))
    ink = (np.asarray(small) < 140).astype(np.float32)
    best, best_angle = -1.0, 0.0
    for angle in np.arange(-3.0, 3.01, 0.2):
        rot = np.asarray(Image.fromarray(ink * 255).rotate(angle, resample=Image.BILINEAR))
        score = rot.sum(axis=1).var()
        if score > best:
            best, best_angle = score, angle
    if abs(best_angle) < 0.1:
        return gray, 0.0
    return gray.rotate(best_angle, resample=Image.BICUBIC, fillcolor=255), best_angle


def _tesseract(img, page_no):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    tsv = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", OCR_LANG, "--psm", "6", "tsv"],
        input=buf.getvalue(), capture_output=True, check=True,
        env=_TESS_ENV).stdout.decode("utf-8")
    scale = 72.0 / OCR_DPI
    groups = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if row["level"] != "5" or not text:
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        g = groups.setdefault(key, dict(words=[], confs=[], top=int(row["top"])))
        g["words"].append(text)
        g["confs"].append(float(row["conf"]))
    lines, confs = [], []
    for g in sorted(groups.values(), key=lambda g: g["top"]):
        text = _CJK_GAP.sub("", " ".join(g["words"]))
        lines.append(Line(text, page_no, g["top"] * scale, float(np.mean(g["confs"]))))
        confs.extend(g["confs"])
    return lines, confs


def _ocr_page(page, page_no):
    """OCR the deskewed page twice, as is and denoised (median filter + contrast
    stretch), and keep the reading Tesseract is more confident about. Denoising
    rescues poor scans but can blur fine strokes on clean ones."""
    pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    img, _ = _deskew(img)
    clean = ImageOps.autocontrast(img.filter(ImageFilter.MedianFilter(3)), cutoff=1)
    best = None
    for variant in (img, clean):
        lines, confs = _tesseract(variant, page_no)
        score = float(np.mean(confs)) if confs else 0.0
        if best is None or score > best[0]:
            best = (score, lines, confs)
    return best[1], best[2]


def needs_ocr(page, text_chars):
    """A page is a scan when it has (almost) no text layer but does carry an image.
    A short page of real text (one line, a cover) has no image and is left alone."""
    return text_chars < MIN_TEXT_CHARS and bool(page.get_images(full=False))


def page_lines(page, page_no, ocr="auto"):
    """Lines of one PyMuPDF page -> (lines, word_confidences, used_ocr).

    ocr: "auto" OCRs pages without a usable text layer (if Tesseract is installed),
         "force" OCRs every page, "off" never OCRs.
    """
    words = [w[:5] for w in page.get_text("words")]
    chars = sum(len(w[4]) for w in words)
    if ocr == "off" or (ocr == "auto" and not needs_ocr(page, chars)):
        return _group_words(words, page_no), [], False
    if not ocr_available():
        if ocr == "force":
            raise RuntimeError("OCR requested but tesseract is not installed")
        return _group_words(words, page_no), [], False
    lines, confs = _ocr_page(page, page_no)
    return lines, confs, True
