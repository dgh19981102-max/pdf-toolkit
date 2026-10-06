"""Command line: pdftool index | search | stats | invoices | serve."""
import argparse
import json
import os
import sys

from .invoices import pipeline
from .search import index

DEFAULT_DB = os.environ.get("PDFTOOL_DB", "pdfindex.db")
_HL, _DIM, _RESET = "\033[1;31m", "\033[2m", "\033[0m"


def _color():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _progress_printer():
    last = [-1]

    def progress(done, total, path):
        pct = done * 100 // total
        if pct != last[0] or done == total:
            last[0] = pct
            print(f"\r  {done}/{total} ({pct}%) {os.path.basename(str(path))[:40]:<40}",
                  end="", file=sys.stderr, flush=True)
    return progress


def cmd_index(a):
    con = index.connect(a.db)
    rep = index.import_paths(con, a.paths, workers=a.workers, prune=a.prune, ocr=a.ocr,
                             progress=None if a.quiet else _progress_printer())
    if not a.quiet:
        print(file=sys.stderr)
    print(f"found {rep.found} PDFs: {rep.indexed} indexed ({rep.pages} pages, {rep.ocr_pages} via OCR), "
          f"{rep.unchanged} unchanged, {rep.failed} failed, {rep.removed} removed "
          f"in {rep.seconds:.1f}s")
    for path, err in rep.errors[:20]:
        print(f"  ! {path}: {err}")
    if len(rep.errors) > 20:
        print(f"  ... and {len(rep.errors) - 20} more (see `pdftool stats`)")


def _render(segs, color):
    if not color:
        return "".join(f"[{t}]" if m else t for t, m in segs)
    return "".join(f"{_HL}{t}{_RESET}" if m else t for t, m in segs)


def cmd_search(a):
    if not a.query:
        sys.exit("pdftool search: give at least one keyword")
    con = index.connect(a.db)
    q = index.parse_query(" ".join(a.query), any_mode=a.any)
    hits = index.search(con, q, limit=a.limit, path_filter=a.path, show_pages=a.pages)
    if a.json:
        print(json.dumps([dict(path=h.path, title=h.title, matching_pages=h.n_pages,
                               pages=[dict(page=p, matches=n,
                                           snippets=["".join(t for t, _ in s) for s in sn])
                                      for p, n, sn in h.pages])
                          for h in hits], ensure_ascii=False, indent=2))
        return
    if not hits:
        print("no matches")
        return
    color = _color()
    for h in hits:
        head = f"{h.path}  ({h.n_pages} matching pages)"
        print(f"\033[1m{head}{_RESET}" if color else head)
        for page, n, sn in h.pages:
            for s in sn[:1]:
                tag = f"p.{page}"
                print(f"  {_DIM if color else ''}{tag:>6}{_RESET if color else ''}  {_render(s, color)}")
        if h.n_pages > len(h.pages):
            print(f"         ... {h.n_pages - len(h.pages)} more pages")
        print()
    more = f" (showing {len(hits)})" if hits.total_docs > len(hits) else ""
    print(f"{hits.total_docs} documents{more}")


def cmd_stats(a):
    con = index.connect(a.db)
    s = index.stats(con, a.db)
    print(f"documents: {s['documents']}  pages: {s['pages']}  failed: {s['failed']}  "
          f"OCR'd pages: {s['ocr_pages']}  pages without text: {s['empty_pages']}  "
          f"db: {s.get('db_mb', 0)} MB")
    if s["scanned"]:
        print("\nno extractable text (scanned, and OCR unavailable or failed):")
        for r in s["scanned"]:
            print(f"  {r['path']} ({r['n_pages']} pages)")
    if s["errors"]:
        print("\nfailed to read:")
        for r in s["errors"]:
            print(f"  {r['path']}: {r['error']}")


def cmd_invoices(a):
    res = pipeline.process(a.inputs, a.output, a.json, a.workers, _progress_printer())
    print(file=sys.stderr)
    print(f"{res['invoices']} invoices ({res['ocr']} OCR) -> {a.output} in {res['seconds']:.0f}s; "
          f"{res['passed']} passed, {res['review']} need review ({res['issues']} issues)")
    for f in res["failures"]:
        print("  failed:", f)
    if a.index:
        rep = index.import_paths(index.connect(a.db), a.inputs, workers=a.workers)
        print(f"added to search index {a.db}: {rep.indexed} indexed, {rep.unchanged} unchanged")


def cmd_serve(a):
    from .web import create_app
    print(f"open http://{a.host}:{a.port}  (index: {os.path.abspath(a.db)})")
    create_app(a.db).run(host=a.host, port=a.port, threaded=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pdftool",
        description="PDF toolkit: keyword search over many PDFs, and invoices to a checked Excel.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"index file (default {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", aliases=["import"],
                       help="index PDF files or folders for search (recursive, incremental)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--ocr", choices=["auto", "off", "force"], default="auto",
                   help="OCR pages without a text layer (default auto: if tesseract is installed)")
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel workers (default: CPUs)")
    p.add_argument("--prune", action="store_true", help="drop indexed files that no longer exist")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help='search keywords: word "a phrase" -excluded')
    p.add_argument("query", nargs="*")
    p.add_argument("--any", action="store_true", help="match any keyword instead of all")
    p.add_argument("-n", "--limit", type=int, default=20, help="max documents")
    p.add_argument("--pages", type=int, default=3, help="pages shown per document")
    p.add_argument("--path", help="only documents whose path contains this text")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("stats", help="index summary, failed and scanned files")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("invoices", help="extract invoices into a checked Excel workbook")
    p.add_argument("inputs", nargs="+", help="PDF files or folders")
    p.add_argument("-o", "--output", default="发票汇总.xlsx")
    p.add_argument("--json", help="also write the extracted data as JSON")
    p.add_argument("-j", "--workers", type=int)
    p.add_argument("--index", action="store_true", help="also add the files to the search index")
    p.set_defaults(fn=cmd_invoices)

    p = sub.add_parser("serve", help="local web page (search + invoices)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    a, extra = ap.parse_known_args(argv)
    if a.cmd == "search":
        # "-word" excludes a keyword; argparse would otherwise reject it as an unknown option
        bad = [x for x in extra if not x.startswith("-") or x.startswith("--")]
        if bad:
            ap.error(f"unrecognized arguments: {' '.join(bad)}")
        a.query += extra
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    a.fn(a)


if __name__ == "__main__":
    main()
