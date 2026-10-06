"""Read an invoice PDF into text lines (text layer or OCR, see pdftoolkit.ocr)."""
from dataclasses import dataclass

import numpy as np
import pymupdf

from ..ocr import Line, needs_ocr, ocr_available, page_lines  # noqa: F401  (re-exported)


@dataclass
class Document:
    path: str
    lines: list
    method: str            # "text" or "ocr"
    pages: int
    ocr_conf: float = None  # mean word confidence over OCR'd pages


def read_pdf(path, force_ocr=False):
    lines, confs, used_ocr = [], [], False
    with pymupdf.open(path) as doc:
        n = len(doc)
        for i, page in enumerate(doc, 1):
            ls, cs, used = page_lines(page, i, "force" if force_ocr else "auto")
            if not used and needs_ocr(page, sum(len(l.text) for l in ls)) and not ocr_available():
                raise RuntimeError("scanned page needs OCR but tesseract is not installed")
            lines.extend(ls)
            confs.extend(cs)
            used_ocr |= used
    return Document(str(path), lines, "ocr" if used_ocr else "text", n,
                    round(float(np.mean(confs)), 1) if confs else None)
