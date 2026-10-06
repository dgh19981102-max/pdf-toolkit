"""Extract invoice fields and line items from text lines, independent of layout.

Header fields are found by their labels (Chinese and English variants). Line items
are found by arithmetic instead of position: a row whose trailing numbers contain
quantity x unit price = amount is an item, wherever the columns are. Rows inside
the item table that fail that check are still kept, marked as not reconciling, so a
misread digit becomes a review item instead of silently disappearing.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .reader import Document

# ----------------------------------------------------------------------------- labels
LABELS = {
    "invoice_no": r"发票号码|发票号|单据编号|单号|Invoice\s*No\.?|Invoice\s*Number",
    "date": r"开票日期|日期|Invoice\s*Date|(?<![A-Za-z])Date",
    "seller": r"销售方名称|销售方|销货单位|(?<![A-Za-z])Seller|(?<![A-Za-z])From",
    "buyer": r"购买方名称|购买方|购货单位|客户名称|客户|(?<![A-Za-z])Buyer|Bill\s*To",
    "total": r"价税合计|应付总额|含税总额|总计|Grand\s*Total|Total\s*Due|Amount\s*Due|(?<![A-Za-z])Total",
    "tax": r"增值税额|(?<!含)税额|Tax\s*Amount|(?<![A-Za-z])VAT|(?<![A-Za-z])Tax(?!\s*(Rate|率))",
    "subtotal": r"不含税金额|金额合计|合计金额|小计|Net\s*Amount|Sub-?\s*total",
}
_ANY_LABEL = re.compile("|".join(f"(?:{v})" for v in LABELS.values()), re.I)
_LBL = {k: re.compile(v, re.I) for k, v in LABELS.items()}
# words that start another field on the same line (value of one label stops there)
_STOP = re.compile(_ANY_LABEL.pattern + r"|纳税人|地址|电话|Attn|Currency|币种|日期|Date|发票号|Invoice", re.I)

_HEADER_QTY = re.compile(r"数量|Qty|Quantity", re.I)
_HEADER_AMT = re.compile(r"金额|Amount", re.I)
_HEADER_PRICE = re.compile(r"单价|Unit\s*Price|Price", re.I)
_PAGE_FOOTER = re.compile(r"第\s*\d+\s*页|Page\s+\d+", re.I)
_COMPANY = re.compile(r"([一-鿿A-Za-z0-9()（）]+?(?:有限公司|公司))|"
                      r"([A-Z][\w&.,\- ]+?(?:Ltd\.?|Inc\.?|Co\.?|LLC|GmbH))")

_NUM = re.compile(r"^(?:[x×X@]|¥|￥|\$|€|CNY|USD|RMB|EUR)?\s*(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)$")
_PCT = re.compile(r"^\(?(\d{1,2}(?:\.\d+)?)\s*%\)?$")
_MONEY = re.compile(r"(?:¥|￥|\$|€|CNY|USD|RMB|EUR)?\s*(-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|-?\d+\.\d{2})(?!\d)")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATES = [
    (re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    (re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})"), "dmy"),
    (re.compile(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})"), "mdy"),
]


@dataclass
class Item:
    line_no: int
    desc: str
    qty: float = None
    unit_price: float = None
    amount: float = None
    ok: bool = True          # qty x unit_price == amount
    conf: float = 100.0
    page: int = 1


@dataclass
class Invoice:
    file: str
    method: str
    pages: int
    ocr_conf: float = None
    invoice_no: str = None
    date: date = None
    date_raw: str = None
    seller: str = None
    buyer: str = None
    currency: str = None
    subtotal: float = None
    tax: float = None
    tax_rate: float = None
    total: float = None
    items: list = field(default_factory=list)
    notes: list = field(default_factory=list)     # values inferred rather than read


# ----------------------------------------------------------------------------- helpers
def normalize(text):
    text = (text.replace("：", ":").replace("（", "(").replace("）", ")").replace("，", ",")
            .replace("￥", "¥"))
    text = re.sub(r"\s*:\s*", ": ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_number(s):
    return float(s.replace(",", ""))


# digits without (or with only some) separators, e.g. OCR output "202605/03"; only
# trusted on a line that carries a date label
_COMPACT_DATE = re.compile(r"(?<!\d)(20\d{2})[-/.]?(\d{2})[-/.]?(\d{2})(?!\d)")


def parse_date(text, compact=False):
    rules = _DATES + ([(_COMPACT_DATE, "ymd")] if compact else [])
    for rx, order in rules:
        for m in rx.finditer(text):
            try:
                if order == "ymd":
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif order == "dmy":
                    d, mo, y = int(m[1]), _MONTHS.get(m[2].lower()), int(m[3])
                else:
                    mo, d, y = _MONTHS.get(m[1].lower()), int(m[2]), int(m[3])
                if mo:
                    return date(y, mo, d), m[0]
            except ValueError:
                continue
    return None, None


def _value_after(label_rx, text):
    """Text after the label, up to the next label on the same line."""
    m = label_rx.search(text)
    if not m:
        return None
    rest = text[m.end():].lstrip(" :.")
    # bilingual labels come in pairs ("发票号 Invoice No.:"): skip the second one
    while (m2 := label_rx.match(rest)) and m2.end() > 0:
        rest = rest[m2.end():].lstrip(" :.")
    stop = _STOP.search(rest)
    val = (rest[:stop.start()] if stop else rest).strip(" :;,")
    return val or None


def _last_money(text):
    vals = _MONEY.findall(text)
    return to_number(vals[-1]) if vals else None


def _tokens(text):
    return text.split(" ")


def _numeric_tail(tokens):
    """Split tokens into (description tokens, trailing numeric tokens)."""
    i = len(tokens)
    while i > 0 and (_NUM.match(tokens[i - 1]) or _PCT.match(tokens[i - 1])):
        i -= 1
    return tokens[:i], tokens[i:]


def _close(a, b):
    return abs(a - b) <= max(0.011, abs(b) * 1e-6)


# ----------------------------------------------------------------------------- items
def _match_item(nums, price_first):
    """nums: numbers in the row's numeric tail (percentages removed). The last one is
    the amount; find quantity and unit price among the rest."""
    amount, rest = nums[-1], nums[:-1]
    if len(rest) >= 2:
        a, b = rest[-2], rest[-1]
        if _close(a * b, amount):
            return (b, a, amount, True) if price_first else (a, b, amount, True)
        for i in range(len(rest)):
            for j in range(i + 1, len(rest)):
                if _close(rest[i] * rest[j], amount):
                    q, p = (rest[j], rest[i]) if price_first else (rest[i], rest[j])
                    return q, p, amount, True
        q, p = (b, a) if price_first else (a, b)
        return q, p, amount, False
    return None, rest[0] if rest else None, amount, False


def extract_items(lines, money_lines, field_lines):
    header = next((i for i, l in enumerate(lines)
                   if _HEADER_QTY.search(l.text) and _HEADER_AMT.search(l.text)), None)
    price_first = False
    if header is not None:
        h = lines[header].text
        q, p = _HEADER_QTY.search(h), _HEADER_PRICE.search(h)
        price_first = bool(q and p and p.start() < q.start())
    totals_at = [i for i in money_lines if i > (header if header is not None else -1)]
    end = min(totals_at) if totals_at else len(lines)
    start = header + 1 if header is not None else 0

    items, prev_text, prev_conf = [], None, 100.0
    for i in range(start, end):
        line = lines[i]
        text = line.text
        if (_HEADER_QTY.search(text) and _HEADER_AMT.search(text)) or _PAGE_FOOTER.search(text):
            prev_text = None
            continue
        if i in field_lines:
            prev_text = None
            continue
        toks = _tokens(text)
        desc_toks, tail = _numeric_tail(toks)
        nums = [to_number(_NUM.match(t)[1]) for t in tail if _NUM.match(t)]
        # a leading row number ("3" or "3.") is not part of the description
        if len(desc_toks) > 1 and re.fullmatch(r"\d{1,3}\.?", desc_toks[0]):
            desc_toks = desc_toks[1:]
        desc = " ".join(desc_toks).strip()
        conf = line.conf
        if len(nums) < 2:
            prev_text, prev_conf = desc or None, line.conf
            continue
        if not desc and prev_text:             # receipt style: name on the line above
            desc, conf = prev_text, min(conf, prev_conf)
        order = price_first
        if header is None and len(nums) >= 3:
            # no readable header: a "1,299.00"-style token is the price, a bare
            # integer the quantity
            raw = [t for t in tail if _NUM.match(t)][-3:-1]
            if len(raw) == 2:
                dec = [bool(re.search(r"\.\d{2}$", t)) for t in raw]
                if dec == [True, False]:
                    order = True
                elif dec == [False, True]:
                    order = False
        qty, price, amount, ok = _match_item(nums, order)
        items.append(Item(len(items) + 1, desc, qty, price, amount, ok, round(conf, 1),
                          line.page))
        prev_text = None
    return items


# ----------------------------------------------------------------------------- invoice
_CUR_SYMBOLS = [("USD", r"USD|US\$|\$"), ("EUR", r"EUR|€"), ("CNY", r"CNY|RMB|¥")]


def _currency(lines, label_lines):
    """Currency from the symbols on the amount lines first (most reliable), then from
    explicit codes anywhere, then Chinese text implies CNY."""
    money = " ".join(lines[i].text for i, ks in label_lines.items() if ks & {"total", "tax", "subtotal"})
    for code, rx in _CUR_SYMBOLS:
        if re.search(rx, money):
            return code
    alltext = " ".join(l.text for l in lines)
    for code, rx in [("USD", r"\bUSD\b"), ("EUR", r"\bEUR\b"), ("CNY", r"\b(?:CNY|RMB)\b")]:
        if re.search(rx, alltext):
            return code
    if re.search(r"[\u4e00-\u9fff]", alltext):
        return "CNY"
    return "USD" if "$" in alltext else None


def parse(doc: Document) -> Invoice:
    for l in doc.lines:
        l.text = normalize(l.text)
    lines = [l for l in doc.lines if l.text]
    inv = Invoice(Path(doc.path).name, doc.method, doc.pages, doc.ocr_conf)

    label_lines = {}            # line index -> set of labels found there
    for i, l in enumerate(lines):
        for k, rx in _LBL.items():
            if rx.search(l.text):
                label_lines.setdefault(i, set()).add(k)

    for i, l in enumerate(lines):
        t = l.text
        found = label_lines.get(i, set())
        if "invoice_no" in found and not inv.invoice_no:
            v = _value_after(_LBL["invoice_no"], t)
            m = re.search(r"[A-Z0-9][A-Z0-9-]{3,}", (v or "").replace("No.", "").replace(" ", ""))
            if m:
                inv.invoice_no = m[0]
        if "date" in found and not inv.date:
            inv.date, inv.date_raw = parse_date(t, compact=True)
            if not inv.date:
                inv.date_raw = _value_after(_LBL["date"], t)
        for k in ("seller", "buyer"):
            if k in found and not getattr(inv, k):
                v = _value_after(_LBL[k], t)
                if v and not _MONEY.fullmatch(v):
                    setattr(inv, k, v)
        money = _last_money(t)
        if money is not None:
            # one amount per line: the most specific label wins (total > tax > subtotal)
            for k in ("total", "tax", "subtotal"):
                if k in found and getattr(inv, k) is None:
                    if k == "total" and "subtotal" in found and re.search(r"Sub-?\s*total", t, re.I):
                        continue
                    setattr(inv, k, money)
                    if k == "tax":
                        m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", t)
                        if m:
                            inv.tax_rate = float(m[1]) / 100
                    break

    # seller fallback: the first company name in the document (letterhead / title line)
    if not inv.seller:
        for l in lines[:6]:
            m = _COMPANY.search(l.text)
            if m and (not inv.buyer or (m[1] or m[2]).strip() not in inv.buyer):
                inv.seller = (m[1] or m[2]).strip()
                break
    for k in ("seller", "buyer"):
        v = getattr(inv, k)
        if v:
            m = _COMPANY.search(v)
            setattr(inv, k, (m[1] or m[2]).strip() if m else v)
    if not inv.date:            # a date anywhere on the page, if no labelled one
        for l in lines:
            d, raw = parse_date(l.text)
            if d:
                inv.date, inv.date_raw = d, raw
                break
    if not inv.tax_rate:
        for l in lines:
            m = re.search(r"(?:税率|Tax|VAT)\D{0,12}?(\d{1,2})\s*%", l.text, re.I) or \
                (re.search(r"\s(\d{1,2})%\s", f" {l.text} ") if "%" in l.text else None)
            if m:
                inv.tax_rate = float(m[1]) / 100
                break

    inv.currency = _currency(lines, label_lines)

    money_lines = sorted(i for i, ks in label_lines.items()
                         if ks & {"total", "tax", "subtotal"} and _last_money(lines[i].text) is not None)
    field_lines = {i for i, ks in label_lines.items() if ks & {"invoice_no", "date", "seller", "buyer"}}
    if inv.total is None and None not in (inv.subtotal, inv.tax) and money_lines:
        # label unreadable (e.g. OCR garbled it): an unlabelled amount right below the
        # other totals that equals subtotal + tax is the total
        for i in range(money_lines[-1] + 1, min(money_lines[-1] + 3, len(lines))):
            v = _last_money(lines[i].text)
            if v is not None and _close(inv.subtotal + inv.tax, v):
                inv.total = v
                inv.notes.append(("total", f"价税合计标签未识别，按位置推断（原文：{lines[i].text}）"))
                break
    inv.items = extract_items(lines, money_lines, field_lines)
    return inv
