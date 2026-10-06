"""Helpers to generate test PDFs (English and Chinese) with reportlab."""
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def make_pdf(path, pages, title=None):
    """pages: list of pages, each a list of text lines."""
    c = canvas.Canvas(str(path), pagesize=A4)
    if title:
        c.setTitle(title)
    for lines in pages:
        y = 800
        for line in lines:
            c.setFont("STSong-Light", 12)
            c.drawString(60, y, line)
            y -= 18
        c.showPage()
    c.save()
