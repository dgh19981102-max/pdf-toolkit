"""Generate 20 mock invoices in 5 different layouts, some as scanned images.

Every invoice is fictional (made-up companies, "SAMPLE / 演示样例" watermark).
Alongside the PDFs, truth.json records what each invoice really says, including the
problems deliberately planted in a few of them, so the extractor can be scored.

    python demo/generate.py --out samples
"""
import argparse
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(TTFont("Hei", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", subfontIndex=0))
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
SANS, SANS_B, SERIF = "Helvetica", "Helvetica-Bold", "Times-Roman"

SELLERS_CN = ["星河示例科技有限公司", "青禾演示办公用品有限公司", "远帆样例机电设备有限公司",
              "云杉模拟信息服务有限公司", "北辰示范物流有限公司"]
BUYERS_CN = ["明湖演示制造有限公司", "启元样例贸易有限公司", "松石示例咨询有限公司",
             "安澜模拟电子有限公司"]
SELLERS_EN = ["Northwind Demo Supplies Ltd.", "Bluepeak Sample Systems Inc.",
              "Harbor Example Logistics Co."]
BUYERS_EN = ["Contoso Demo Manufacturing", "Fabrikam Sample Trading", "Tailspin Example Labs"]
ITEMS_CN = [("办公椅", 450), ("激光打印机", 1899), ("A4打印纸 500张/包", 26.5),
            ("显示器 27英寸", 1299), ("无线键鼠套装", 159), ("会议服务费", 800),
            ("网线 CAT6 305米", 568), ("文件柜", 720), ("技术支持服务(小时)", 300),
            ("硒鼓", 389), ("白板笔 10支装", 18.8), ("路由器", 649)]
ITEMS_EN = [("Office chair", 120), ("Laser printer", 289), ("Copy paper, 500 sheets", 6.4),
            ("27-inch monitor", 219), ("Consulting (hours)", 95), ("USB-C dock", 149.5),
            ("Network cable 50m", 38), ("Desk lamp", 42.75), ("Toner cartridge", 79)]

# planted problems: invoice index -> issue
PLANTED = {4: "total_mismatch", 9: "subtotal_mismatch", 13: "duplicate",
           17: "missing_date", 20: "tax_mismatch"}
SCANNED = {3, 7, 9, 11, 15, 18, 19}          # rendered as image-only PDFs
HEAVY_SCAN = {15}                            # extra-poor scan quality
TEMPLATES = ["cn_classic", "en_modern", "receipt", "bilingual", "cn_multipage"]


def money(x):
    return f"{x:,.2f}"


def make_data(i, rng):
    tpl = TEMPLATES[(i - 1) % 5]
    en = tpl == "en_modern"
    n_items = {"receipt": rng.randint(2, 4), "cn_multipage": rng.randint(24, 30)}.get(
        tpl, rng.randint(2, 6))
    catalog = ITEMS_EN if en else ITEMS_CN
    picks = [catalog[rng.randrange(len(catalog))] for _ in range(n_items)]
    items = []
    for desc, price in picks:
        qty = rng.choice([1, 2, 3, 4, 5, 6, 10, 12, 20]) if price > 50 else rng.choice([10, 20, 50, 100])
        if "小时" in desc or "hours" in desc:
            qty = rng.choice([2.5, 4, 7.5, 12])
        items.append(dict(desc=desc, qty=qty, unit_price=price, amount=round(qty * price, 2)))
    rate = {"en_modern": 0.06, "receipt": 0.06}.get(tpl, 0.13)
    subtotal = round(sum(it["amount"] for it in items), 2)
    tax = round(subtotal * rate, 2)
    d = date(2026, 1, 5) + timedelta(days=rng.randrange(0, 240))
    inv = dict(
        file=f"invoice_{i:02d}.pdf", template=tpl, scanned=i in SCANNED,
        invoice_no=(f"INV-2026-{rng.randrange(1000, 9999)}" if en else f"{rng.randrange(10**7, 10**8)}"),
        date=d.isoformat(),
        seller=rng.choice(SELLERS_EN if en else SELLERS_CN),
        buyer=rng.choice(BUYERS_EN if en else BUYERS_CN),
        currency="USD" if en else "CNY",
        subtotal=subtotal, tax_rate=rate, tax=tax, total=round(subtotal + tax, 2),
        items=items, planted=PLANTED.get(i),
    )
    # printed values differ from the true arithmetic where a problem is planted
    inv["printed"] = dict(subtotal=subtotal, tax=tax, total=inv["total"])
    if inv["planted"] == "total_mismatch":
        inv["printed"]["total"] = round(inv["total"] + 100, 2)
    elif inv["planted"] == "subtotal_mismatch":
        inv["printed"]["subtotal"] = round(subtotal - 50, 2)
        inv["printed"]["total"] = round(inv["printed"]["subtotal"] + tax, 2)
    elif inv["planted"] == "tax_mismatch":
        inv["printed"]["tax"] = round(tax + 88, 2)                    # typo in the tax
        inv["printed"]["total"] = round(subtotal + inv["printed"]["tax"], 2)
    elif inv["planted"] == "missing_date":
        inv["date"] = None
    return inv


# ----------------------------------------------------------------------------- drawing
def watermark(c, w, h):
    c.saveState()
    c.setFillGray(0.88)
    c.setFont("Hei", 46)
    c.translate(w / 2, h / 2)
    c.rotate(30)
    c.drawCentredString(0, 0, "SAMPLE 演示样例")
    c.restoreState()


def fmt_date(d, style):
    if d is None:
        return ""
    d = date.fromisoformat(d)
    return {"cn": f"{d.year}年{d.month:02d}月{d.day:02d}日", "dmy": d.strftime("%d %b %Y"),
            "slash": d.strftime("%Y/%m/%d"), "mdy": d.strftime("%b %d, %Y").replace(" 0", " "),
            "dot": d.strftime("%Y.%m.%d")}[style]


def draw_cn_classic(c, inv):
    w, h = A4
    p = inv["printed"]
    watermark(c, w, h)
    c.setFont("Hei", 20)
    c.drawCentredString(w / 2, h - 60, "销 售 发 票")
    c.setFont("Hei", 10)
    c.drawString(400, h - 90, f"发票号码：{inv['invoice_no']}")
    c.drawString(400, h - 106, f"开票日期：{fmt_date(inv['date'], 'cn')}")
    c.rect(40, h - 190, w - 80, 70)
    c.drawString(50, h - 140, f"购买方名称：{inv['buyer']}")
    c.drawString(50, h - 158, "纳税人识别号：91310000MA1D0EMO1X")
    c.drawString(310, h - 140, f"销售方名称：{inv['seller']}")
    c.drawString(310, h - 158, "地址电话：示例市演示路 88 号")
    y = h - 220
    cols = [50, 80, 330, 400, 490]
    for x, t in zip(cols, ["序号", "货物或服务名称", "数量", "单价", "金额"]):
        c.drawString(x, y, t)
    c.line(40, y - 6, w - 40, y - 6)
    for n, it in enumerate(inv["items"], 1):
        y -= 20
        c.drawString(50, y, str(n))
        c.drawString(80, y, it["desc"])
        c.drawRightString(370, y, f"{it['qty']:g}")
        c.drawRightString(460, y, money(it["unit_price"]))
        c.drawRightString(550, y, money(it["amount"]))
    y -= 14
    c.line(40, y, w - 40, y)
    c.drawString(330, y - 20, "合计金额")
    c.drawRightString(550, y - 20, f"¥{money(p['subtotal'])}")
    c.drawString(330, y - 38, f"税额（{inv['tax_rate']:.0%}）")
    c.drawRightString(550, y - 38, f"¥{money(p['tax'])}")
    c.setFont("Hei", 12)
    c.drawString(330, y - 60, "价税合计（小写）")
    c.drawRightString(550, y - 60, f"¥{money(p['total'])}")
    c.setFont("Hei", 9)
    c.drawString(50, 60, "开户行及账号：演示银行示例支行 6222 0210 0100 1234")


def draw_en_modern(c, inv):
    w, h = A4
    p = inv["printed"]
    watermark(c, w, h)
    c.setFillColorRGB(0.12, 0.25, 0.55)
    c.rect(0, h - 80, w, 80, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(SANS_B, 26)
    c.drawString(40, h - 52, "INVOICE")
    c.setFont(SANS, 10)
    c.drawRightString(w - 40, h - 40, inv["seller"])
    c.drawRightString(w - 40, h - 54, "100 Example Street, Demo City")
    c.setFillColorRGB(0, 0, 0)
    c.setFont(SANS, 10)
    c.drawString(40, h - 110, f"Invoice No: {inv['invoice_no']}")
    c.drawString(40, h - 125, f"Invoice Date: {fmt_date(inv['date'], 'dmy')}")
    c.drawString(40, h - 140, f"From: {inv['seller']}")
    c.drawString(330, h - 110, f"Bill To: {inv['buyer']}")
    c.drawString(330, h - 125, "Attn: Accounts Payable")
    y = h - 180
    c.setFont(SANS_B, 10)
    for x, t, right in [(40, "Description", 0), (360, "Qty", 1), (460, "Unit Price", 1), (555, "Amount", 1)]:
        (c.drawRightString if right else c.drawString)(x, y, t)
    c.setFont(SANS, 10)
    for it in inv["items"]:
        y -= 22
        c.setFillGray(0.95)
        c.rect(36, y - 6, w - 72, 20, stroke=0, fill=1)
        c.setFillGray(0)
        c.drawString(40, y, it["desc"])
        c.drawRightString(360, y, f"{it['qty']:g}")
        c.drawRightString(460, y, money(it["unit_price"]))
        c.drawRightString(555, y, money(it["amount"]))
    y -= 40
    for label, val in [("Subtotal", p["subtotal"]), (f"VAT {inv['tax_rate']:.0%}", p["tax"])]:
        c.drawString(380, y, label)
        c.drawRightString(555, y, f"${money(val)}")
        y -= 18
    c.setFont(SANS_B, 12)
    c.drawString(380, y - 4, "Total Due (USD)")
    c.drawRightString(555, y - 4, f"${money(p['total'])}")
    c.setFont(SANS, 9)
    c.drawString(40, 60, "Payment terms: 30 days. Thank you for your business.")


def draw_receipt(c, inv):
    w, h = 240, 520
    c.setPageSize((w, h))
    p = inv["printed"]
    c.setFont("STSong-Light", 12)
    c.drawCentredString(w / 2, h - 30, inv["seller"])
    c.setFont("STSong-Light", 9)
    c.drawCentredString(w / 2, h - 46, "SAMPLE 演示样例  销售单据")
    c.drawString(14, h - 70, f"单号 No.{inv['invoice_no']}")
    c.drawString(14, h - 84, f"日期 {fmt_date(inv['date'], 'slash')}")
    c.drawString(14, h - 98, f"客户 {inv['buyer']}")
    c.drawString(14, h - 112, "-" * 46)
    y = h - 128
    for it in inv["items"]:
        c.drawString(14, y, it["desc"])
        y -= 13
        c.drawString(24, y, f"x{it['qty']:g} @{money(it['unit_price'])}")
        c.drawRightString(w - 14, y, money(it["amount"]))
        y -= 16
    c.drawString(14, y, "-" * 46)
    y -= 16
    for label, val in [("小计", p["subtotal"]), (f"税额({inv['tax_rate']:.0%})", p["tax"]), ("总计", p["total"])]:
        c.drawString(14, y, label)
        c.drawRightString(w - 14, y, money(val))
        y -= 15
    c.drawCentredString(w / 2, 30, "谢谢惠顾")


def draw_bilingual(c, inv):
    w, h = A4
    p = inv["printed"]
    watermark(c, w, h)
    c.setFont("Hei", 16)
    c.drawString(40, h - 55, "商业发票 COMMERCIAL INVOICE")
    c.setFont("Hei", 9)
    c.drawString(40, h - 85, f"销售方 Seller: {inv['seller']}")
    c.drawString(40, h - 100, f"购买方 Buyer: {inv['buyer']}")
    c.drawString(360, h - 85, f"发票号 Invoice No.: {inv['invoice_no']}")
    c.drawString(360, h - 100, f"日期 Date: {fmt_date(inv['date'], 'mdy')}")
    c.drawString(360, h - 115, "币种 Currency: CNY")
    y = h - 150
    c.setFillColorRGB(0.85, 0.9, 0.85)
    c.rect(40, y - 5, w - 80, 18, stroke=0, fill=1)
    c.setFillGray(0)
    heads = [(45, "品名 Description", 0), (300, "数量 Qty", 1), (390, "单价 Price", 1),
             (460, "税率 Tax", 1), (550, "金额 Amount", 1)]
    for x, t, right in heads:
        (c.drawRightString if right else c.drawString)(x, y, t)
    for it in inv["items"]:
        y -= 20
        c.drawString(45, y, it["desc"])
        c.drawRightString(300, y, f"{it['qty']:g}")
        c.drawRightString(390, y, money(it["unit_price"]))
        c.drawRightString(460, y, f"{inv['tax_rate']:.0%}")
        c.drawRightString(550, y, money(it["amount"]))
        c.line(40, y - 6, w - 40, y - 6)
    y -= 34
    for label, val in [("不含税金额 Net Amount", p["subtotal"]), ("税额 Tax Amount", p["tax"]),
                       ("含税总额 Grand Total", p["total"])]:
        c.drawString(330, y, label)
        c.drawRightString(550, y, f"CNY {money(val)}")
        y -= 17


def draw_cn_multipage(c, inv):
    w, h = A4
    p = inv["printed"]
    per_page = 18
    chunks = [inv["items"][i:i + per_page] for i in range(0, len(inv["items"]), per_page)]
    n = 0
    for pg, chunk in enumerate(chunks, 1):
        watermark(c, w, h)
        c.setFont("STSong-Light", 18)
        c.drawString(40, h - 55, f"{inv['seller']} 发货结算单")
        c.setFont("STSong-Light", 10)
        c.drawString(40, h - 80, f"单据编号：{inv['invoice_no']}    日期：{fmt_date(inv['date'], 'dot')}")
        c.drawString(40, h - 95, f"客户名称：{inv['buyer']}")
        y = h - 130
        for x, t in [(45, "行"), (75, "品名"), (330, "单价"), (410, "数量"), (480, "金额")]:
            c.drawString(x, y, t)
        c.line(40, y - 6, w - 40, y - 6)
        for it in chunk:
            n += 1
            y -= 19
            c.drawString(45, y, f"{n}.")
            c.drawString(75, y, it["desc"])
            c.drawRightString(380, y, money(it["unit_price"]))
            c.drawRightString(440, y, f"{it['qty']:g}")
            c.drawRightString(550, y, money(it["amount"]))
        if pg == len(chunks):
            y -= 30
            c.drawString(330, y, f"金额合计：{money(p['subtotal'])}")
            c.drawString(330, y - 17, f"增值税额：{money(p['tax'])}")
            c.drawString(330, y - 34, f"应付总额：{money(p['total'])}")
        c.drawCentredString(w / 2, 40, f"第 {pg} 页 共 {len(chunks)} 页")
        c.showPage()


DRAW = dict(cn_classic=draw_cn_classic, en_modern=draw_en_modern, receipt=draw_receipt,
            bilingual=draw_bilingual, cn_multipage=draw_cn_multipage)


def render_pdf(inv):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Sample invoice {inv['invoice_no']}")
    DRAW[inv["template"]](c, inv)
    if inv["template"] != "cn_multipage":
        c.showPage()
    c.save()
    return buf.getvalue()


def to_scan(pdf_bytes, rng, heavy=False):
    """Rasterise each page and degrade it like a cheap office scanner."""
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    nrng = np.random.default_rng(rng.randrange(1 << 30))
    for page in src:
        dpi = 110 if heavy else 170
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        angle = rng.uniform(1.2, 2.2) * rng.choice([-1, 1]) if heavy else rng.uniform(-1.2, 1.2)
        img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(0.9 if heavy else 0.5))
        a = np.asarray(img, dtype=np.float32)
        a = a * (0.82 if heavy else 0.9) + (22 if heavy else 14)          # grey paper, weak ink
        a += nrng.normal(0, 14 if heavy else 7, a.shape)
        img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
        jpg = io.BytesIO()
        img.save(jpg, "JPEG", quality=45 if heavy else 70)
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=jpg.getvalue())
    return out.tobytes(garbage=3, deflate=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples")
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    invoices = [make_data(i, rng) for i in range(1, 21)]
    # planted duplicate: invoice 13 repeats the number and seller of invoice 8 (same layout)
    dup, orig = invoices[12], invoices[7]
    dup["invoice_no"], dup["seller"] = orig["invoice_no"], orig["seller"]
    orig["planted"] = "duplicate"         # both copies should be flagged
    for i, inv in enumerate(invoices, 1):
        pdf = render_pdf(inv)
        if inv["scanned"]:
            pdf = to_scan(pdf, rng, heavy=i in HEAVY_SCAN)
        (out / inv["file"]).write_bytes(pdf)
    (out / "truth.json").write_text(json.dumps(invoices, ensure_ascii=False, indent=1))
    print(f"wrote {len(invoices)} invoices to {out}/ ({len(SCANNED)} scanned)")


if __name__ == "__main__":
    main()
