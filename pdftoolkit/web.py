"""Local web interface with two tools:

  搜索          keyword search with highlighted snippets; links open the PDF at the
                matching page; batch import by folder path or browser upload
  发票转 Excel   run the invoice extraction on a folder or uploaded files, show the
                summary and review list, download the workbook; optionally add the
                invoices to the search index as well

Meant for local use (binds to 127.0.0.1 by default); it can read any folder the
server process can read, so do not expose it to untrusted networks.
"""
import os
import re
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from .invoices import pipeline
from .search import index

_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def _safe_name(part):
    """File-name component that cannot escape the upload folder. Unlike werkzeug's
    secure_filename it keeps non-ASCII (e.g. Chinese) names."""
    part = _UNSAFE.sub("_", part).strip(" .")
    return part[:200] or "_"


def _save_uploads(files, root):
    """Save uploaded PDFs under root/<timestamp>/, keeping sub-folders of folder uploads."""
    batch = Path(root) / time.strftime("%Y%m%d-%H%M%S")
    n = 0
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        parts = [_safe_name(p) for p in Path(f.filename.replace("\\", "/")).parts]
        dest = batch.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(dest)
        n += 1
    return (str(batch), n) if n else (None, 0)


class Job:
    """One background task at a time per tool; the page polls .state."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = dict(running=False, done=0, total=0, current="", report=None)

    def start(self, fn):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = dict(running=True, done=0, total=0, current="", report=None,
                              stage="", started=time.time())
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()
        return True

    def progress(self, done, total, path):
        self.state.update(done=done, total=total, current=os.path.basename(str(path)))

    def _run(self, fn):
        try:
            self.state["report"] = fn(self)
        except Exception as e:
            self.state["report"] = dict(error=f"{type(e).__name__}: {e}")
        finally:
            self.state["running"] = False


def _index_report(rep):
    return dict(found=rep.found, indexed=rep.indexed, pages=rep.pages, ocr_pages=rep.ocr_pages,
                unchanged=rep.unchanged, failed=rep.failed, seconds=round(rep.seconds, 1),
                errors=rep.errors[:20])


def create_app(db_path, data_dir=None):
    app = Flask(__name__)
    db_path = os.path.abspath(db_path)
    data_dir = Path(data_dir or os.path.dirname(db_path))
    uploads, exports = data_dir / "uploads", data_dir / "exports"
    index_job, invoice_job = Job(), Job()
    index.connect(db_path).close()          # create schema up front

    def con():
        return index.connect(db_path)

    def run_index(paths):
        def fn(job):
            job.state["stage"] = "建立索引"
            c = con()
            try:
                return _index_report(index.import_paths(c, paths, progress=job.progress))
            finally:
                c.close()
        return fn

    def run_invoices(paths, also_index):
        def fn(job):
            job.state["stage"] = "识别发票"
            name = f"发票汇总-{time.strftime('%Y%m%d-%H%M%S')}.xlsx"
            res = pipeline.process(paths, exports / name, progress=job.progress)
            res["download"] = name
            if also_index:
                job.state.update(stage="加入搜索索引", done=0, total=0)
                c = con()
                try:
                    res["indexed"] = _index_report(index.import_paths(c, paths, progress=job.progress))
                finally:
                    c.close()
            return res
        return fn

    def _folder():
        folder = request.form.get("folder", "").strip()
        return folder if folder and os.path.exists(folder) else None

    # ------------------------------------------------------------------ search
    @app.get("/")
    def search_page():
        q = request.args.get("q", "").strip()
        any_mode = request.args.get("mode") == "any"
        path = request.args.get("path", "").strip() or None
        hits, elapsed = [], None
        c = con()
        if q:
            t0 = time.perf_counter()
            hits = index.search(c, index.parse_query(q, any_mode), limit=100, path_filter=path)
            elapsed = (time.perf_counter() - t0) * 1000
        st = index.stats(c, db_path)
        c.close()
        return render_template("search.html", tab="search", q=q, any_mode=any_mode,
                               path=path or "", hits=hits, elapsed=elapsed, stats=st)

    @app.get("/pdf/<int:doc_id>")
    def pdf(doc_id):
        c = con()
        d = index.get_document(c, doc_id)
        c.close()
        if d is None or not os.path.isfile(d["path"]):
            abort(404)
        return send_file(d["path"], mimetype="application/pdf")

    @app.post("/index")
    def index_folder():
        folder = _folder()
        if not folder:
            return jsonify(ok=False, message="路径不存在"), 400
        return jsonify(ok=index_job.start(run_index([folder])))

    @app.post("/index/upload")
    def index_upload():
        batch, n = _save_uploads(request.files.getlist("files"), uploads)
        if not batch:
            return jsonify(ok=False, message="没有 PDF 文件"), 400
        return jsonify(ok=index_job.start(run_index([batch])), saved=n)

    @app.get("/index/status")
    def index_status():
        return jsonify(index_job.state)

    # ------------------------------------------------------------------ invoices
    @app.get("/invoices")
    def invoices_page():
        return render_template("invoices.html", tab="invoices", state=invoice_job.state)

    @app.post("/invoices/run")
    def invoices_folder():
        folder = _folder()
        if not folder:
            return jsonify(ok=False, message="路径不存在"), 400
        also = request.form.get("also_index") == "1"
        return jsonify(ok=invoice_job.start(run_invoices([folder], also)))

    @app.post("/invoices/upload")
    def invoices_upload():
        batch, n = _save_uploads(request.files.getlist("files"), uploads)
        if not batch:
            return jsonify(ok=False, message="没有 PDF 文件"), 400
        also = request.form.get("also_index") == "1"
        return jsonify(ok=invoice_job.start(run_invoices([batch], also)), saved=n)

    @app.get("/invoices/status")
    def invoices_status():
        return jsonify(invoice_job.state)

    @app.get("/invoices/download/<name>")
    def invoices_download(name):
        path = exports / name
        if name != os.path.basename(name) or not name.endswith(".xlsx") or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=name)

    return app
