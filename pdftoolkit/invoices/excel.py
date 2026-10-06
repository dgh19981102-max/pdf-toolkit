"""Write the extraction results to a formatted Excel workbook.

Sheets
  发票汇总   one row per invoice; reconciliation columns are live formulas, so
             correcting a value by hand updates the check and the status
  明细       one row per line item, with qty x price recomputed by formula
  待复核     every issue found, with yellow columns for the reviewer to fill in
  合计核对   totals per currency, cross-checked against the line items
  说明       legend
"""
from datetime import datetime

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
REVIEW_FILL = PatternFill("solid", fgColor="FCE4D6")
OK_FONT = Font(name=FONT, color="2E7D32")
BAD_FONT = Font(name=FONT, color="C00000", bold=True)
THIN = Side(style="thin", color="D9D9D9")
MONEY = "#,##0.00"


def _sheet(wb, title, headers, widths, first=False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for c, w in enumerate(widths, 1):
        cell = ws.cell(1, c)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    return ws


def _finish(ws, money_cols=(), date_cols=(), pct_cols=(), wrap_cols=(), center_cols=()):
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            col = cell.column_letter
            cell.font = Font(name=FONT, bold=cell.font.bold, color=cell.font.color)
            cell.border = Border(bottom=THIN)
            if col in money_cols:
                cell.number_format = MONEY
            elif col in date_cols:
                cell.number_format = "yyyy-mm-dd"
            elif col in pct_cols:
                cell.number_format = "0%"
            cell.alignment = Alignment(vertical="top", wrap_text=col in wrap_cols,
                                       horizontal="center" if col in center_cols else None)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_title_rows = "1:1"
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"


def write_workbook(path, invoices, issues, source=""):
    wb = Workbook()
    n = len(invoices)

    # ------------------------------------------------------------------ 发票汇总
    ws = _sheet(wb, "发票汇总",
                ["序号", "文件名", "发票号码", "开票日期", "销售方", "购买方", "币种",
                 "不含税金额", "税率", "税额", "价税合计", "明细合计", "明细差额", "价税差额",
                 "合计核对", "提取方式", "OCR置信度", "问题数", "状态"],
                [6, 16, 16, 12, 34, 32, 7, 14, 7, 12, 14, 14, 11, 11, 10, 9, 10, 8, 9], first=True)
    for i, inv in enumerate(invoices, 2):
        ws.append([i - 1, inv.file, inv.invoice_no, inv.date, inv.seller, inv.buyer, inv.currency,
                   inv.subtotal, inv.tax_rate, inv.tax, inv.total,
                   f"=SUMIFS(明细!$H:$H,明细!$A:$A,B{i})",
                   f'=IF(H{i}="","",ROUND(L{i}-H{i},2))',
                   f'=IF(OR(H{i}="",J{i}="",K{i}=""),"",ROUND(H{i}+J{i}-K{i},2))',
                   f'=IF(OR(H{i}="",J{i}="",K{i}=""),"缺数据",'
                   f'IF(AND(ABS(L{i}-H{i})<0.01,ABS(H{i}+J{i}-K{i})<0.01),"一致","不一致"))',
                   "OCR" if inv.method == "ocr" else "文字层", inv.ocr_conf,
                   f"=COUNTIF(待复核!$B:$B,B{i})",
                   f'=IF(R{i}>0,"待复核","通过")'])
    _finish(ws, money_cols="HJKLMN", date_cols="D", pct_cols="I", wrap_cols="EF",
            center_cols="AGIOPQRS")
    last = n + 1
    ws.conditional_formatting.add(f"A2:S{last}", FormulaRule(formula=['$S2="待复核"'], fill=REVIEW_FILL))
    ws.conditional_formatting.add(f"O2:O{last}", FormulaRule(formula=['$O2<>"一致"'], font=BAD_FONT))
    ws.conditional_formatting.add(f"S2:S{last}", FormulaRule(formula=['$S2="通过"'], font=OK_FONT))
    ws["L1"].comment = Comment("明细表中同一文件的金额之和（公式）", "pdftoolkit")
    ws["O1"].comment = Comment("明细合计 = 不含税金额，且 不含税金额 + 税额 = 价税合计 时为“一致”",
                               "pdftoolkit")
    ws["S1"].comment = Comment("待复核表中有该文件的问题时为“待复核”；处理后删除对应行即可变为“通过”",
                               "pdftoolkit")

    # ------------------------------------------------------------------ 明细
    wd = _sheet(wb, "明细", ["文件名", "发票号码", "币种", "行号", "品名", "数量", "单价", "金额",
                           "计算金额", "核对", "页码", "OCR置信度"],
                [16, 16, 7, 6, 30, 9, 12, 14, 14, 9, 6, 10])
    r = 2
    for inv in invoices:
        for it in inv.items:
            wd.append([inv.file, inv.invoice_no, inv.currency, it.line_no, it.desc, it.qty,
                       it.unit_price, it.amount,
                       f'=IF(OR(F{r}="",G{r}=""),"",ROUND(F{r}*G{r},2))',
                       f'=IF(I{r}="","缺数据",IF(ABS(I{r}-H{r})<0.01,"一致","不一致"))',
                       it.page, it.conf if inv.method == "ocr" else None])
            r += 1
    _finish(wd, money_cols="GHI", wrap_cols="E", center_cols="CDJKL")
    for row in wd.iter_rows(min_row=2, min_col=6, max_col=6):
        row[0].number_format = "#,##0.##"
    wd.conditional_formatting.add(f"J2:J{max(r - 1, 2)}", FormulaRule(formula=['$J2<>"一致"'], font=BAD_FONT))

    # ------------------------------------------------------------------ 待复核
    wr = _sheet(wb, "待复核", ["序号", "文件名", "发票号码", "级别", "问题类型", "说明", "识别值",
                             "参考值", "处理结果", "备注"],
                [6, 16, 16, 7, 11, 44, 22, 16, 14, 24])
    for k, iss in enumerate(sorted(issues, key=lambda x: (x.file, x.severity != "错误")), 2):
        wr.append([k - 1, iss.file, iss.invoice_no, iss.severity, iss.kind, iss.detail,
                   iss.found, iss.expected, None, None])
    _finish(wr, wrap_cols="FGJ", center_cols="ADE")
    end = max(wr.max_row, 2)
    for row in wr.iter_rows(min_row=2, max_row=end, min_col=9, max_col=10):
        for cell in row:
            cell.fill = INPUT_FILL
    dv = DataValidation(type="list", formula1='"已更正,原件如此,作废,待确认"', allow_blank=True)
    wr.add_data_validation(dv)
    dv.add(f"I2:I{max(end, 200)}")
    wr.conditional_formatting.add(f"D2:D{end}", FormulaRule(formula=['$D2="错误"'], font=BAD_FONT))
    wr["I1"].comment = Comment("请复核人填写（下拉选择）", "pdftoolkit")

    # ------------------------------------------------------------------ 合计核对
    wc = _sheet(wb, "合计核对", ["币种", "发票张数", "不含税金额", "税额", "价税合计", "明细合计",
                              "明细差额", "通过", "待复核"], [10, 10, 16, 14, 16, 16, 12, 8, 8])
    currencies = sorted({inv.currency or "" for inv in invoices})
    for k, cur in enumerate(currencies, 2):
        wc.append([cur, f"=COUNTIF(发票汇总!$G:$G,A{k})",
                   f"=SUMIF(发票汇总!$G:$G,A{k},发票汇总!$H:$H)",
                   f"=SUMIF(发票汇总!$G:$G,A{k},发票汇总!$J:$J)",
                   f"=SUMIF(发票汇总!$G:$G,A{k},发票汇总!$K:$K)",
                   f"=SUMIF(明细!$C:$C,A{k},明细!$H:$H)",
                   f"=ROUND(F{k}-C{k},2)",
                   f'=COUNTIFS(发票汇总!$G:$G,A{k},发票汇总!$S:$S,"通过")',
                   f'=COUNTIFS(发票汇总!$G:$G,A{k},发票汇总!$S:$S,"待复核")'])
    t = len(currencies) + 2
    wc.append(["合计", f"=SUM(B2:B{t - 1})", None, None, None, None, None,
               f"=SUM(H2:H{t - 1})", f"=SUM(I2:I{t - 1})"])
    _finish(wc, money_cols="CDEFG")
    for cell in wc[t]:
        cell.font = Font(name=FONT, bold=True)
    wc.conditional_formatting.add(f"G2:G{t - 1}", FormulaRule(formula=["ABS($G2)>=0.01"], font=BAD_FONT))
    wc.cell(t + 2, 1, "说明：金额按币种分别汇总，不跨币种相加。明细差额不为 0 说明有发票的明细与"
                      "不含税金额对不上，见“待复核”。重复发票会被重复计入，请先处理“重复发票”问题。")
    wc.cell(t + 2, 1).font = Font(name=FONT, italic=True, color="595959")

    # ------------------------------------------------------------------ 说明
    wi = _sheet(wb, "说明", ["项目", "内容"], [18, 90])
    n_ocr = sum(inv.method == "ocr" for inv in invoices)
    n_review = len({iss.file for iss in issues})
    rows = [
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("来源", source),
        ("发票数量", f"{n} 张（文字层 {n - n_ocr}，扫描件 OCR {n_ocr}）"),
        ("待复核", f"{n_review} 张发票，共 {len(issues)} 个问题"),
        ("发票汇总", "每张发票一行。明细合计、差额、合计核对、问题数、状态均为公式，手工修正数值后自动重算。"),
        ("明细", "每个明细行一行；“计算金额”= 数量 × 单价，与识别出的金额不一致时标红。"),
        ("待复核", "所有自动核对未通过的项目。黄色列（处理结果、备注）由复核人填写；"
                 "问题处理完毕后删除该行，汇总表状态自动变为“通过”。"),
        ("合计核对", "按币种汇总金额与张数，并与明细合计交叉核对。"),
        ("颜色", "浅橙色行 = 待复核；红字 = 核对不一致；黄色单元格 = 需人工填写。"),
        ("核对规则", "必填字段（发票号码、开票日期、销售方、价税合计）；数量×单价=金额；"
                   "明细之和=不含税金额；不含税金额+税额=价税合计；税额=不含税金额×税率；"
                   "发票号码+销售方重复；扫描件 OCR 置信度。金额容差 0.01。"),
    ]
    for row in rows:
        wi.append(row)
    _finish(wi, wrap_cols="B")
    for row in wi.iter_rows(min_row=2, max_col=1):
        row[0].font = Font(name=FONT, bold=True)

    wb.save(path)
    return path
