"""Score extraction against samples/truth.json.

    python demo/evaluate.py samples output/extracted.json

Fields are compared with the values printed on the invoice (a planted arithmetic
error is printed, so extracting it faithfully is correct; the checks must then
flag it). Reports field accuracy for text and scanned PDFs separately, line-item
accuracy, and whether every planted problem ended up on the review list.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

FIELDS = ["invoice_no", "date", "seller", "buyer", "currency", "subtotal", "tax", "tax_rate", "total"]
NAMES = dict(invoice_no="发票号码", date="开票日期", seller="销售方", buyer="购买方", currency="币种",
             subtotal="不含税金额", tax="税额", tax_rate="税率", total="价税合计")
PLANTED_KIND = dict(total_mismatch="合计不符", subtotal_mismatch="合计不符", duplicate="重复发票",
                    missing_date="缺少字段", tax_mismatch="税额不符")


def _norm(s):
    return re.sub(r"\s+", "", str(s)) if s is not None else None


def expected(t, f):
    if f in ("subtotal", "tax", "total"):
        return t["printed"][f]
    if f == "tax_rate" and t["template"] == "cn_multipage":
        return None                       # rate is not printed on this layout
    return t[f]


def main(sample_dir="samples", extracted="output/extracted.json"):
    truth = {t["file"]: t for t in json.loads(Path(sample_dir, "truth.json").read_text())}
    data = json.loads(Path(extracted).read_text())
    got = {i["file"]: i for i in data["invoices"]}
    issues = defaultdict(list)
    for iss in data["issues"]:
        issues[iss["file"]].append(iss)

    score = {m: defaultdict(lambda: [0, 0]) for m in ("text", "ocr")}
    items = {m: [0, 0, 0] for m in ("text", "ocr")}   # exact rows, true rows, extracted rows
    misses, item_misses = [], []
    for f, t in truth.items():
        g = got.get(f, {})
        m = "ocr" if t["scanned"] else "text"
        for fld in FIELDS:
            e = expected(t, fld)
            if fld == "tax_rate" and e is None:
                continue
            v = g.get(fld)
            ok = _norm(v) == _norm(e) if isinstance(e, str) or e is None else \
                (v is not None and abs(v - e) < 0.005)
            score[m][fld][0] += ok
            score[m][fld][1] += 1
            if not ok:
                misses.append((f, NAMES[fld], v, e))
        gi = g.get("items", [])
        items[m][1] += len(t["items"])
        items[m][2] += len(gi)
        # pair rows by content, not position, so one missed row does not shift the rest
        unused = list(gi)
        for b in t["items"]:
            hit = next((a for a in unused if _norm(a["desc"]) == _norm(b["desc"])
                        and a["qty"] == b["qty"] and a["unit_price"] == b["unit_price"]
                        and a["amount"] == b["amount"]), None)
            if hit:
                unused.remove(hit)
                items[m][0] += 1
            else:
                item_misses.append((f, b["desc"], b["amount"]))

    print("## 字段准确率\n")
    print("| 字段 | 文字版 PDF | 扫描件 (OCR) |\n|---|---|---|")
    tot = {m: [0, 0] for m in score}
    for fld in FIELDS:
        cells = []
        for m in ("text", "ocr"):
            c, n = score[m][fld]
            tot[m][0] += c
            tot[m][1] += n
            cells.append(f"{c}/{n}")
        print(f"| {NAMES[fld]} | {' | '.join(cells)} |")
    print(f"| **全部字段** | **{tot['text'][0]}/{tot['text'][1]} ({tot['text'][0] / tot['text'][1]:.0%})** "
          f"| **{tot['ocr'][0]}/{tot['ocr'][1]} ({tot['ocr'][0] / tot['ocr'][1]:.0%})** |")
    print("\n## 明细行\n")
    for m, label in (("text", "文字版"), ("ocr", "扫描件")):
        ok, n, ext = items[m]
        print(f"- {label}：{ok}/{n} 行完全正确（品名、数量、单价、金额），共识别 {ext} 行")
    by_file = defaultdict(int)
    for f, _, _ in item_misses:
        by_file[f] += 1
    if by_file:
        print("- 有误的行：" + "，".join(f"{f} {k} 行" + ("（已标记待复核）" if issues[f] else "（未标记）")
                                   for f, k in sorted(by_file.items())))

    print("\n## 埋设问题是否被发现\n")
    print("| 文件 | 埋设的问题 | 是否进入待复核 |\n|---|---|---|")
    caught = 0
    planted = {f: t["planted"] for f, t in truth.items() if t["planted"]}
    for f, p in sorted(planted.items()):
        hit = any(i["kind"] == PLANTED_KIND[p] for i in issues[f])
        caught += hit
        print(f"| {f} | {p} | {'✅' if hit else '❌'} |")
    print(f"\n{caught}/{len(planted)} 个埋设问题被发现。")

    clean_flagged = sorted(f for f in truth if f not in planted and issues[f])
    print("\n## 其他进入待复核的发票（OCR 误读等）\n")
    for f in clean_flagged:
        kinds = "、".join(sorted({i["kind"] for i in issues[f]}))
        wrong = [m for m in misses if m[0] == f]
        print(f"- {f}（{'扫描件' if truth[f]['scanned'] else '文字版'}）：{kinds}；"
              f"实际识别错误字段 {len(wrong)} 个")
    silent = sorted({m[0] for m in misses if not issues[m[0]]})
    print(f"\n识别有误但未被标记的发票：{len(silent)} 张 {silent if silent else ''}")
    if misses:
        print("\n<details><summary>全部字段差异</summary>\n")
        for f, fld, v, e in misses:
            print(f"- {f} {fld}: 识别 `{v}`，应为 `{e}`")
        print("\n</details>")


if __name__ == "__main__":
    main(*sys.argv[1:])
