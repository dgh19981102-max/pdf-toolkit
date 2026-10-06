import random
from datetime import date

import pytest
from openpyxl import load_workbook

import generate
from pdftoolkit.invoices.excel import write_workbook
from pdftoolkit.invoices.parse import Invoice, Item, _match_item, _value_after, _LBL, parse, parse_date
from pdftoolkit.invoices.reader import Document, Line, ocr_available, read_pdf
from pdftoolkit.invoices.validate import check_batch, check_invoice


# ----------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("text, expect", [
    ("开票日期：2026年03月05日", date(2026, 3, 5)),
    ("开票日期: 2026 年 3 月 5 日", date(2026, 3, 5)),
    ("Invoice Date: 05 Mar 2026", date(2026, 3, 5)),
    ("日期 Date: Mar 5, 2026", date(2026, 3, 5)),
    ("日期：2026.03.05", date(2026, 3, 5)),
    ("2026/3/5", date(2026, 3, 5)),
    ("2026-02-30", None),                          # impossible date
])
def test_parse_date(text, expect):
    assert parse_date(text)[0] == expect


def test_compact_date_only_when_asked():
    assert parse_date("日期 202605/03")[0] is None
    assert parse_date("日期 202605/03", compact=True)[0] == date(2026, 5, 3)


def test_bilingual_label_pairs_and_stop_at_next_label():
    assert _value_after(_LBL["invoice_no"], "销售方 Seller: 甲公司 发票号 Invoice No.: 1234") == "1234"
    assert _value_after(_LBL["buyer"], "购买方名称: 乙公司销售方名称: 甲公司") == "乙公司"
    # an empty field must not swallow the next label's value
    assert _value_after(_LBL["date"], "Invoice Date: Attn: Accounts Payable") is None


def test_match_item_finds_qty_and_price_in_any_order():
    assert _match_item([12, 300.0, 3600.0], False) == (12, 300.0, 3600.0, True)
    assert _match_item([300.0, 12, 3600.0], True) == (12, 300.0, 3600.0, True)
    assert _match_item([5, 1899.0, 13, 9495.0], False)[3] is True       # tax-rate column between
    assert _match_item([1, 149.5, 1794.0], False)[3] is False           # misread quantity


def _doc(lines, method="text"):
    return Document("x.pdf", [Line(t, 1, i * 10.0) for i, t in enumerate(lines)], method, 1)


def test_parse_generic_layout():
    inv = parse(_doc([
        "销 售 发 票", "发票号码：12345678", "开票日期：2026年02月02日",
        "购买方名称：乙公司 销售方名称：甲科技有限公司",
        "序号 货物或服务名称 数量 单价 金额",
        "1 激光打印机 2 1,899.00 3,798.00",
        "2 A4打印纸 500张/包 10 26.50 265.00",
        "合计金额 ¥4,063.00", "税额（13%） ¥528.19", "价税合计（小写） ¥4,591.19",
        "开户行及账号：演示银行 6222 0210 0100 1234",
    ]))
    assert (inv.invoice_no, inv.date, inv.seller, inv.buyer, inv.currency) == \
        ("12345678", date(2026, 2, 2), "甲科技有限公司", "乙公司", "CNY")
    assert (inv.subtotal, inv.tax, inv.tax_rate, inv.total) == (4063.0, 528.19, 0.13, 4591.19)
    assert [(i.desc, i.qty, i.unit_price, i.amount) for i in inv.items] == [
        ("激光打印机", 2, 1899.0, 3798.0), ("A4打印纸 500张/包", 10, 26.5, 265.0)]
    assert check_invoice(inv, today=date(2026, 12, 31)) == []


def test_receipt_style_name_above_numbers():
    inv = parse(_doc(["某某有限公司", "单号 No.123456", "日期 2026/05/03", "办公椅", "x6 @450.00 2,700.00",
                      "小计 2,700.00", "税额(6%) 162.00", "总计 2,862.00"]))
    assert [(i.desc, i.qty, i.unit_price) for i in inv.items] == [("办公椅", 6, 450.0)]
    assert inv.seller == "某某有限公司" and inv.total == 2862.0


def test_unlabelled_total_is_inferred_and_flagged():
    inv = parse(_doc(["发票号码：1", "开票日期：2026年01月01日", "销售方名称：甲公司",
                      "数量 单价 金额", "笔 2 5.00 10.00", "合计金额 ¥10.00", "税额（13%） ¥1.30",
                      "HEA. (ZS) ¥11.30"]))
    assert inv.total == 11.3
    kinds = [i.kind for i in check_invoice(inv, today=date(2026, 12, 31))]
    assert "推断取值" in kinds


# ----------------------------------------------------------------------------- checks
def _inv(**kw):
    base = dict(file="a.pdf", method="text", pages=1, invoice_no="1", date=date(2026, 1, 1),
                seller="甲", buyer="乙", currency="CNY", subtotal=100.0, tax=13.0, tax_rate=0.13,
                total=113.0, items=[Item(1, "x", 2, 50.0, 100.0)])
    base.update(kw)
    return Invoice(**base)


@pytest.mark.parametrize("kw, kind", [
    (dict(total=213.0), "合计不符"),
    (dict(subtotal=90.0, total=103.0, tax=13.0, tax_rate=None), "合计不符"),
    (dict(tax=20.0, total=120.0), "税额不符"),
    (dict(tax=13.88, total=113.88, tax_rate=None), "税额不符"),    # implied rate not statutory
    (dict(date=None), "缺少字段"),
    (dict(date=date(2031, 1, 1)), "日期异常"),
    (dict(items=[]), "明细缺失"),
    (dict(items=[Item(1, "x", 2, 50.0, 100.0, ok=False)]), "明细不平"),
    (dict(method="ocr", ocr_conf=60.0), "扫描质量"),
])
def test_checks(kw, kind):
    assert kind in [i.kind for i in check_invoice(_inv(**kw), today=date(2026, 12, 31))]


def test_clean_invoice_and_statutory_rate_without_printed_rate():
    assert check_invoice(_inv(), today=date(2026, 12, 31)) == []
    assert check_invoice(_inv(tax_rate=None), today=date(2026, 12, 31)) == []


def test_duplicates_flag_both_copies():
    issues = check_batch([_inv(file="a.pdf"), _inv(file="b.pdf"), _inv(file="c.pdf", invoice_no="2")],
                         today=date(2026, 12, 31))
    assert sorted(i.file for i in issues if i.kind == "重复发票") == ["a.pdf", "b.pdf"]


# ----------------------------------------------------------------------------- end to end
@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("inv")
    rng = random.Random(7)
    invs = []
    for i, tpl in enumerate(generate.TEMPLATES, 1):
        inv = generate.make_data(i, rng)
        (out / inv["file"]).write_bytes(generate.render_pdf(inv))
        invs.append(inv)
    return out, invs


def test_every_layout_extracts_exactly(generated):
    out, invs = generated
    for t in invs:
        inv = parse(read_pdf(out / t["file"]))
        assert inv.method == "text"
        assert inv.invoice_no == t["invoice_no"] and inv.seller == t["seller"], t["template"]
        assert inv.date == (date.fromisoformat(t["date"]) if t["date"] else None), t["template"]
        assert (inv.subtotal, inv.tax, inv.total) == tuple(t["printed"][k] for k in ("subtotal", "tax", "total"))
        assert [(i.qty, i.unit_price, i.amount) for i in inv.items] == \
            [(x["qty"], x["unit_price"], x["amount"]) for x in t["items"]], t["template"]


@pytest.mark.skipif(not ocr_available(), reason="tesseract not installed")
def test_scanned_invoice_via_ocr(tmp_path):
    rng = random.Random(3)
    t = generate.make_data(2, rng)                      # English layout
    pdf = generate.to_scan(generate.render_pdf(t), rng)
    (tmp_path / "s.pdf").write_bytes(pdf)
    inv = parse(read_pdf(tmp_path / "s.pdf"))
    assert inv.method == "ocr" and inv.ocr_conf > 80
    assert inv.invoice_no == t["invoice_no"] and inv.total == t["total"]


def test_workbook_sheets_and_formulas(tmp_path, generated):
    out, invs = generated
    parsed = [parse(read_pdf(out / t["file"])) for t in invs]
    issues = check_batch(parsed, today=date(2026, 12, 31))
    path = tmp_path / "o.xlsx"
    write_workbook(path, parsed, issues)
    wb = load_workbook(path)
    assert wb.sheetnames == ["发票汇总", "明细", "待复核", "合计核对", "说明"]
    ws = wb["发票汇总"]
    assert ws.max_row == len(invs) + 1
    assert ws["L2"].value.startswith("=SUMIFS(明细!")
    assert ws["S2"].value == '=IF(R2>0,"待复核","通过")'
    assert wb["明细"].max_row == sum(len(t["items"]) for t in invs) + 1
