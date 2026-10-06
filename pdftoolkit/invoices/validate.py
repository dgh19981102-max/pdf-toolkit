"""Reconciliation checks. Every problem becomes an Issue; an invoice with any
issue goes on the review list ("待复核") instead of being trusted blindly."""
from dataclasses import dataclass
from datetime import date

TOL = 0.011                 # money tolerance (rounding to cents)
LOW_OCR_CONF = 85.0         # mean Tesseract confidence below this: review the scan
LOW_LINE_CONF = 85.0        # an OCR'd item row read with confidence below this
CN_VAT_RATES = (0.13, 0.09, 0.06, 0.05, 0.03, 0.01, 0.0)

REQUIRED = [("invoice_no", "发票号码"), ("date", "开票日期"), ("seller", "销售方"),
            ("total", "价税合计")]


@dataclass
class Issue:
    file: str
    invoice_no: str
    kind: str               # short category shown in Excel
    detail: str             # what exactly is wrong
    found: str = ""         # value as extracted
    expected: str = ""      # value implied by the other numbers
    severity: str = "错误"   # 错误 (blocks booking) or 提示 (worth a look)


def _m(x):
    return "" if x is None else f"{x:,.2f}"


def check_invoice(inv, today=None):
    today = today or date.today()
    out = []

    def add(kind, detail, found="", expected="", severity="错误"):
        out.append(Issue(inv.file, inv.invoice_no or "", kind, detail, found, expected, severity))

    for attr, name in REQUIRED:
        if getattr(inv, attr) in (None, ""):
            raw = inv.date_raw if attr == "date" and inv.date_raw else ""
            add("缺少字段", f"未识别到{name}" + ("（原文无法解析为日期）" if raw else ""), raw)
    if inv.date and not (date(2000, 1, 1) <= inv.date <= today):
        add("日期异常", "开票日期不在合理范围内", inv.date.isoformat())
    if not inv.buyer:
        add("缺少字段", "未识别到购买方", severity="提示")
    if not inv.items:
        add("明细缺失", "未识别到任何明细行")

    poor_scan = inv.method == "ocr" and inv.ocr_conf is not None and inv.ocr_conf < LOW_OCR_CONF
    for it in inv.items:
        if not it.ok:
            calc = it.qty * it.unit_price if it.qty is not None and it.unit_price is not None else None
            add("明细不平", f"第 {it.line_no} 行「{it.desc}」数量×单价≠金额",
                f"{it.qty:g} × {_m(it.unit_price)} → {_m(it.amount)}" if calc is not None else _m(it.amount),
                _m(calc))
        elif it.conf < LOW_LINE_CONF and not poor_scan:   # a poor scan is reviewed in full anyway
            add("识别存疑", f"第 {it.line_no} 行「{it.desc}」OCR 置信度 {it.conf:.0f}",
                it.desc, severity="提示")

    items_sum = round(sum(it.amount for it in inv.items if it.amount is not None), 2)
    if inv.items and inv.subtotal is not None and abs(items_sum - inv.subtotal) > TOL:
        add("合计不符", "明细金额之和 ≠ 不含税金额", _m(inv.subtotal), _m(items_sum))
    if inv.subtotal is None and inv.items:
        add("缺少字段", "未识别到不含税金额（已可由明细合计推算）", "", _m(items_sum), "提示")
    if None not in (inv.subtotal, inv.tax, inv.total) and abs(inv.subtotal + inv.tax - inv.total) > TOL:
        add("合计不符", "不含税金额 + 税额 ≠ 价税合计", _m(inv.total), _m(inv.subtotal + inv.tax))
    if None not in (inv.subtotal, inv.tax, inv.tax_rate):
        expect = round(inv.subtotal * inv.tax_rate, 2)
        if abs(expect - inv.tax) > max(TOL, 0.01 * len(inv.items)):   # per-line rounding allowed
            add("税额不符", f"税额 ≠ 不含税金额 × 税率 {inv.tax_rate:.0%}", _m(inv.tax), _m(expect))
    elif inv.currency == "CNY" and None not in (inv.subtotal, inv.tax) and inv.subtotal > 0:
        # no rate printed: the implied rate should still be a statutory VAT rate
        implied = inv.tax / inv.subtotal
        if not any(abs(inv.subtotal * r - inv.tax) <= max(TOL, 0.01 * len(inv.items))
                   for r in CN_VAT_RATES):
            near = min(CN_VAT_RATES, key=lambda r: abs(r - implied))
            add("税额不符", f"未印税率；税额/不含税金额 = {implied:.2%}，不是标准增值税率",
                _m(inv.tax), f"{near:.0%} → {_m(inv.subtotal * near)}")
    for fld, note in inv.notes:
        add("推断取值", note, severity="提示")
    if inv.method == "ocr" and inv.ocr_conf is not None and inv.ocr_conf < LOW_OCR_CONF:
        add("扫描质量", f"扫描件 OCR 平均置信度 {inv.ocr_conf:.0f}，建议人工核对原件",
            f"{inv.ocr_conf:.0f}", f"≥ {LOW_OCR_CONF:.0f}", "提示")
    return out


def check_batch(invoices, today=None):
    issues = []
    for inv in invoices:
        issues += check_invoice(inv, today)
    seen = {}
    for inv in invoices:
        if not inv.invoice_no:
            continue
        key = (inv.invoice_no, inv.seller)
        seen.setdefault(key, []).append(inv.file)
    for (no, seller), files in seen.items():
        if len(files) > 1:
            for f in files:
                others = "、".join(x for x in files if x != f)
                issues.append(Issue(f, no, "重复发票", f"与 {others} 发票号码和销售方相同", no))
    return issues
