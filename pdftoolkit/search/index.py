"""SQLite full-text index of PDF pages.

Storage
  documents   one row per PDF file (path, hash, size, mtime, page counts, error)
  page_text   one row per page with its normalised text
  pages_fts   FTS5 index over page_text (external content, trigram tokenizer)

The trigram tokenizer indexes every 3-character substring, so it needs no word
segmentation and works the same for Chinese, Japanese and English. Terms shorter
than 3 characters (e.g. a two-character Chinese word) cannot use the index and
fall back to a LIKE scan, which is slower but still correct.
"""
import multiprocessing as mp
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import functools

from ..ocr import ocr_available
from .extract import extract

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    sha256      TEXT,
    size        INTEGER,
    mtime       REAL,
    title       TEXT,
    n_pages     INTEGER DEFAULT 0,
    n_empty     INTEGER DEFAULT 0,      -- pages without text even after OCR
    n_ocr       INTEGER DEFAULT 0,      -- pages whose text came from OCR
    error       TEXT,
    imported_at REAL
);
CREATE TABLE IF NOT EXISTS page_text (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page    INTEGER NOT NULL,           -- 1-based
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS page_text_doc ON page_text(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    text, content='page_text', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS page_text_ai AFTER INSERT ON page_text BEGIN
    INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS page_text_ad AFTER DELETE ON page_text BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

FTS_MIN_LEN = 3          # trigram tokenizer: shorter terms cannot use the index
COMMIT_EVERY = 50        # documents per transaction during import


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.executescript(SCHEMA)
    return con


# ----------------------------------------------------------------------------- import
@dataclass
class ImportReport:
    found: int = 0
    indexed: int = 0
    unchanged: int = 0
    failed: int = 0
    removed: int = 0
    pages: int = 0
    ocr_pages: int = 0
    seconds: float = 0.0
    errors: list = field(default_factory=list)      # (path, message)


def find_pdfs(paths):
    """Expand files and directories (recursively) into absolute PDF paths."""
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() == ".pdf")
        elif p.is_file():
            out.append(p)
    return sorted({str(f.resolve()) for f in out})


def _store(con, doc_id, res, st):
    pages = res["pages"]
    values = dict(path=res["path"], sha256=res["sha256"], size=st.st_size,
                  mtime=st.st_mtime, title=res["title"], n_pages=len(pages),
                  n_empty=sum(1 for t in pages if not t), n_ocr=res.get("ocr_pages", 0),
                  error=res["error"],
                  imported_at=time.time())
    if doc_id is None:
        cur = con.execute(
            "INSERT INTO documents(path, sha256, size, mtime, title, n_pages, n_empty, n_ocr,"
            " error, imported_at) VALUES (:path, :sha256, :size, :mtime, :title, :n_pages,"
            " :n_empty, :n_ocr, :error, :imported_at)", values)
        doc_id = cur.lastrowid
    else:
        con.execute("DELETE FROM page_text WHERE doc_id = ?", (doc_id,))
        con.execute(
            "UPDATE documents SET sha256=:sha256, size=:size, mtime=:mtime, title=:title,"
            " n_pages=:n_pages, n_empty=:n_empty, n_ocr=:n_ocr, error=:error,"
            " imported_at=:imported_at"
            " WHERE id=:id", dict(values, id=doc_id))
    con.executemany("INSERT INTO page_text(doc_id, page, text) VALUES (?, ?, ?)",
                    [(doc_id, i + 1, t) for i, t in enumerate(pages) if t])


def import_paths(con, paths, workers=None, prune=False, progress=None, ocr="auto"):
    """Index all PDFs under `paths`. Unchanged files (same size and mtime) are skipped,
    so re-running on a growing folder only processes new or modified files.

    prune: also drop indexed documents under `paths` whose files no longer exist.
    progress: optional callback(done, total, path) called after each processed file.
    ocr: "auto" (OCR scanned pages if Tesseract is installed), "off" or "force".
    """
    t0 = time.time()
    rep = ImportReport()
    files = find_pdfs(paths)
    rep.found = len(files)
    known = {r["path"]: r for r in con.execute(
        "SELECT id, path, sha256, size, mtime, error, n_empty, n_ocr FROM documents")}
    # files indexed without OCR that have text-less pages get another try once OCR is on
    retry_scans = ocr != "off" and ocr_available()

    todo = []
    for f in files:
        st = os.stat(f)
        k = known.get(f)
        if (k and k["size"] == st.st_size and k["mtime"] == st.st_mtime and not k["error"]
                and not (retry_scans and k["n_empty"] and not k["n_ocr"])):
            rep.unchanged += 1
        else:
            todo.append(f)

    workers = workers or os.cpu_count() or 1
    if workers > 1 and len(todo) > 1:
        pool = mp.get_context("spawn").Pool(min(workers, len(todo)))
        results = pool.imap_unordered(functools.partial(extract, ocr=ocr), todo, chunksize=1)
    else:
        pool, results = None, map(functools.partial(extract, ocr=ocr), todo)
    try:
        for n, res in enumerate(results, 1):
            path = res["path"]
            st = os.stat(path)
            k = known.get(path)
            if (k and res["sha256"] == k["sha256"] and not res["error"] and not k["error"]
                    and res.get("ocr_pages", 0) == (k["n_ocr"] or 0)):
                # touched but identical content: only refresh the stat fingerprint
                con.execute("UPDATE documents SET size=?, mtime=? WHERE id=?",
                            (st.st_size, st.st_mtime, k["id"]))
                rep.unchanged += 1
            else:
                _store(con, k["id"] if k else None, res, st)
                if res["error"]:
                    rep.failed += 1
                    rep.errors.append((path, res["error"]))
                else:
                    rep.indexed += 1
                    rep.pages += len(res["pages"])
                    rep.ocr_pages += res.get("ocr_pages", 0)
            if n % COMMIT_EVERY == 0:
                con.commit()
            if progress:
                progress(n, len(todo), path)
    finally:
        if pool:
            pool.terminate()
    con.commit()

    if prune:
        roots = [str(Path(p).resolve()) for p in paths]
        present = set(files)
        for path, k in known.items():
            under = any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)
            if under and path not in present:
                con.execute("DELETE FROM page_text WHERE doc_id = ?", (k["id"],))
                con.execute("DELETE FROM documents WHERE id = ?", (k["id"],))
                rep.removed += 1
        con.commit()
    rep.seconds = time.time() - t0
    return rep


# ----------------------------------------------------------------------------- search
@dataclass
class Query:
    include: list
    exclude: list
    any: bool = False


_TOKEN = re.compile(r'(-?)"([^"]+)"|(\S+)')


def parse_query(text, any_mode=False):
    """Space-separated keywords; "quoted phrases" stay together; -word excludes."""
    inc, exc = [], []
    for m in _TOKEN.finditer(text):
        if m.group(2) is not None:
            neg, term = m.group(1) == "-", m.group(2)
        else:
            tok = m.group(3)
            neg, term = tok.startswith("-") and len(tok) > 1, tok
            if neg:
                term = tok[1:]
        term = " ".join(term.split())
        if term:
            (exc if neg else inc).append(term)
    return Query(inc, exc, any_mode)


def _term_sql(term, alias="p"):
    if len(term) >= FTS_MIN_LEN:
        return (f"{alias}.id IN (SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?)",
                '"' + term.replace('"', '""') + '"')
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{alias}.text LIKE ? ESCAPE '\\'", f"%{esc}%"


@dataclass
class Hit:
    doc_id: int
    path: str
    title: str
    n_pages: int         # pages that match
    pages: list = field(default_factory=list)
    # pages: first matching pages, [(page_no, n_matches, snippets)],
    #        snippets = [[(text, is_match), ...], ...]


class Results(list):
    """Hits for the top documents; .total_docs counts every matching document."""
    total_docs = 0


def search(con, query, limit=50, path_filter=None, show_pages=5):
    """Documents containing the keywords, those with the most matching pages first.

    Ranking only needs page counts, which SQLite computes from the index; page text
    is fetched (and snippets built) only for the documents and pages that are shown,
    so common words stay fast on large collections.
    """
    q = parse_query(query) if isinstance(query, str) else query
    out = Results()
    if not q.include:
        return out
    conds, params = [], []
    inc = [_term_sql(t) for t in q.include]
    conds.append("(" + (" OR " if q.any else " AND ").join(c for c, _ in inc) + ")")
    params += [p for _, p in inc]
    for t in q.exclude:
        # excluded words drop the whole document, not just the page that has them
        c, p = _term_sql(t, alias="x")
        conds.append(f"p.doc_id NOT IN (SELECT x.doc_id FROM page_text x WHERE {c})")
        params.append(p)
    if path_filter:
        conds.append("d.path LIKE ?")
        params.append(f"%{path_filter}%")
    where = " FROM page_text p JOIN documents d ON d.id = p.doc_id WHERE " + " AND ".join(conds)

    docs = con.execute("SELECT p.doc_id, d.path, d.title, COUNT(*) n" + where +
                       " GROUP BY p.doc_id", params).fetchall()
    out.total_docs = len(docs)
    docs = sorted(docs, key=lambda r: (-r["n"], r["path"]))[:limit]
    if not docs:
        return out
    hits = {r["doc_id"]: Hit(r["doc_id"], r["path"], r["title"] or "", r["n"]) for r in docs}

    ids = ",".join(str(i) for i in hits)
    rows = con.execute(
        "SELECT doc_id, page, text FROM (SELECT p.doc_id, p.page, p.text, ROW_NUMBER() OVER"
        " (PARTITION BY p.doc_id ORDER BY p.page) rn" + where + f" AND p.doc_id IN ({ids}))"
        " WHERE rn <= ? ORDER BY doc_id, page", params + [show_pages])
    pat = _match_pattern(q.include)
    for r in rows:
        hits[r["doc_id"]].pages.append(
            (r["page"], len(pat.findall(r["text"])), snippets(r["text"], pat)))
    out.extend(hits[r["doc_id"]] for r in docs)
    return out


def _match_pattern(terms):
    return re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)),
                      re.IGNORECASE)


def snippets(text, pat, width=60, max_snippets=3):
    """Context windows around matches, as [[(segment, is_match), ...], ...]."""
    spans = [m.span() for m in pat.finditer(text)]
    windows = []
    for s, e in spans:
        a, b = max(0, s - width), min(len(text), e + width)
        if windows and a <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], b)
        elif len(windows) < max_snippets:
            windows.append([a, b])
    out = []
    for a, b in windows:
        segs, pos = [], a
        if a > 0:
            segs.append(("…", False))
        for s, e in spans:
            if s >= a and e <= b:
                if s > pos:
                    segs.append((text[pos:s], False))
                segs.append((text[s:e], True))
                pos = e
        if pos < b:
            segs.append((text[pos:b], False))
        if b < len(text):
            segs.append(("…", False))
        out.append(segs)
    return out


def stats(con, db_path=None):
    r = con.execute(
        "SELECT COUNT(*) docs, COALESCE(SUM(n_pages),0) pages, COALESCE(SUM(n_empty),0) empty,"
        " COALESCE(SUM(n_ocr),0) ocr,"
        " SUM(error IS NOT NULL) failed FROM documents").fetchone()
    out = dict(documents=r["docs"], pages=r["pages"], empty_pages=r["empty"], ocr_pages=r["ocr"],
               failed=r["failed"] or 0)
    if db_path and os.path.exists(db_path):
        out["db_mb"] = round(os.path.getsize(db_path) / 1e6, 2)
    out["errors"] = [dict(x) for x in con.execute(
        "SELECT path, error FROM documents WHERE error IS NOT NULL ORDER BY path LIMIT 100")]
    out["scanned"] = [dict(x) for x in con.execute(
        "SELECT path, n_pages, n_empty FROM documents WHERE n_pages > 0 AND n_empty = n_pages"
        " ORDER BY path LIMIT 100")]
    return out


def get_document(con, doc_id):
    return con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
