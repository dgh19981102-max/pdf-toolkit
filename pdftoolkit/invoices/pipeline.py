"""Invoice batch: PDFs -> parsed invoices -> checks -> Excel (and optional JSON)."""
import json
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .excel import write_workbook
from .parse import parse
from .reader import read_pdf
from .validate import check_batch


def find_pdfs(paths):
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += [f for f in p.rglob("*") if f.suffix.lower() == ".pdf"]
        elif p.suffix.lower() == ".pdf":
            out.append(p)
    return sorted(set(out))


def extract_one(path):
    try:
        return parse(read_pdf(path)), None
    except Exception as e:                       # unreadable file: report, keep going
        return None, f"{Path(path).name}: {type(e).__name__}: {e}"


def run(paths, workers=None, progress=None):
    """Parse and check every PDF under paths -> (invoices, issues, failures)."""
    files = find_pdfs(paths)
    invoices, failures = [], []
    if files:
        # spawn, not fork: this also runs from a web server thread, and forking a
        # multi-threaded process can deadlock the children
        with ProcessPoolExecutor(max_workers=min(workers or os.cpu_count(), len(files)),
                                 mp_context=mp.get_context("spawn")) as ex:
            for k, (inv, err) in enumerate(ex.map(extract_one, files), 1):
                (failures.append(err) if err else invoices.append(inv))
                if progress:
                    progress(k, len(files), files[k - 1].name)
    return invoices, check_batch(invoices), failures


def _json_default(o):
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


def to_json(invoices, issues, failures):
    return json.dumps(dict(invoices=[asdict(i) for i in invoices], issues=[asdict(i) for i in issues],
                           failures=failures), ensure_ascii=False, indent=1, default=_json_default)


def process(inputs, xlsx_path, json_path=None, workers=None, progress=None):
    """Full batch; returns a summary dict (also used by the web page)."""
    t0 = time.time()
    invoices, issues, failures = run(inputs, workers, progress)
    Path(xlsx_path).parent.mkdir(parents=True, exist_ok=True)
    write_workbook(xlsx_path, invoices, issues, source=", ".join(map(str, inputs)))
    if json_path:
        Path(json_path).write_text(to_json(invoices, issues, failures))
    review = {i.file for i in issues}
    return dict(
        invoices=len(invoices), ocr=sum(i.method == "ocr" for i in invoices),
        passed=len(invoices) - len(review), review=len(review), issues=len(issues),
        failures=failures, seconds=round(time.time() - t0, 1), xlsx=str(xlsx_path),
        rows=[dict(file=i.file, invoice_no=i.invoice_no, date=i.date.isoformat() if i.date else None,
                   seller=i.seller, currency=i.currency, total=i.total, method=i.method,
                   status="待复核" if i.file in review else "通过") for i in invoices],
        problems=[asdict(i) for i in sorted(issues, key=lambda x: (x.file, x.severity != "错误"))],
    )
