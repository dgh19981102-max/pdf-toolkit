"""README images: a strip of sample invoices and screenshots of the Excel sheets.

    python demo/make_previews.py samples output/发票汇总.xlsx docs
Needs LibreOffice (soffice) for the workbook screenshots.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"


def invoice_strip(samples, out, picks=(1, 2, 3, 4, 5, 15)):
    tiles = []
    font = ImageFont.truetype(FONT, 22)
    for i in picks:
        doc = pymupdf.open(Path(samples) / f"invoice_{i:02d}.pdf")
        pix = doc[0].get_pixmap(dpi=55)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples) if pix.n == 3 else \
            Image.frombytes("L", (pix.width, pix.height), pix.samples).convert("RGB")
        scanned = not doc[0].get_text().strip()
        tile = Image.new("RGB", (img.width, img.height + 36), "white")
        tile.paste(img, (0, 36))
        ImageDraw.Draw(tile).text((4, 4), f"#{i:02d} {'扫描件' if scanned else '文字版'}",
                                  fill="#b45309" if scanned else "#1f3864", font=font)
        tiles.append(tile)
    h = max(t.height for t in tiles)
    strip = Image.new("RGB", (sum(t.width for t in tiles) + 16 * (len(tiles) - 1), h), "#e5e7eb")
    x = 0
    for t in tiles:
        strip.paste(t, (x, 0))
        x += t.width + 16
    strip.save(out)


def sheet_shots(xlsx, outdir):
    """Export the workbook to PDF with LibreOffice and keep the first page of each sheet."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(xlsx)],
                       check=True, capture_output=True)
        pdf = pymupdf.open(next(Path(tmp).glob("*.pdf")))
        # first page of each sheet, recognised by its header row
        wanted = {"序号\n文件名\n发票号码\n开票日期": "excel_summary.png",
                  "序号\n文件名\n发票号码\n级别": "excel_review.png",
                  "币种\n发票张数": "excel_totals.png"}
        for page in pdf:
            text = page.get_text()
            for head, fn in list(wanted.items()):
                if text.startswith(head):
                    page.get_pixmap(dpi=110, clip=_content_box(page)).save(Path(outdir) / fn)
                    del wanted[head]


def _content_box(page):
    blocks = [b[:4] for b in page.get_text("blocks")]
    x0 = min(b[0] for b in blocks) - 6
    y0 = min(b[1] for b in blocks) - 6
    x1 = max(b[2] for b in blocks) + 6
    y1 = max(b[3] for b in blocks) + 6
    return pymupdf.Rect(x0, y0, x1, y1) & page.rect


if __name__ == "__main__":
    samples, xlsx, outdir = (sys.argv[1:] + ["samples", "output/发票汇总.xlsx", "docs"])[:3] \
        if len(sys.argv) < 4 else sys.argv[1:4]
    Path(outdir).mkdir(exist_ok=True)
    invoice_strip(samples, Path(outdir) / "invoices.png")
    sheet_shots(xlsx, outdir)
    print("previews written to", outdir)
