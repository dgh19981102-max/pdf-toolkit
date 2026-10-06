"""Integration of the two tools: OCR'd search, web pages for both, invoices -> index."""
import random
import time

import pytest

import generate
from pdfgen import make_pdf
from pdftoolkit.cli import main
from pdftoolkit.ocr import ocr_available
from pdftoolkit.search import index
from pdftoolkit.web import create_app

needs_ocr = pytest.mark.skipif(not ocr_available(), reason="tesseract not installed")


def _wait(client, url, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get(url).json
        if not s["running"] and s["report"]:
            return s["report"]
        time.sleep(0.2)
    raise TimeoutError(url)


@pytest.fixture(scope="module")
def invoices(tmp_path_factory):
    """Three generated invoices: two text PDFs and one scan."""
    out = tmp_path_factory.mktemp("inv")
    rng = random.Random(11)
    truth = []
    for i in (1, 2, 4):
        t = generate.make_data(i, rng)
        pdf = generate.render_pdf(t)
        if i == 4 and ocr_available():
            pdf = generate.to_scan(pdf, rng)
            t["scanned"] = True
        (out / t["file"]).write_bytes(pdf)
        truth.append(t)
    return out, truth


@needs_ocr
def test_scanned_pdf_becomes_searchable(tmp_path):
    rng = random.Random(5)
    t = generate.make_data(2, rng)                       # English layout, rendered as a scan
    (tmp_path / "scan.pdf").write_bytes(generate.to_scan(generate.render_pdf(t), rng))
    con = index.connect(str(tmp_path / "i.db"))
    rep = index.import_paths(con, [tmp_path], workers=1)
    assert rep.ocr_pages == 1
    assert [h.path.split("/")[-1] for h in index.search(con, t["invoice_no"])] == ["scan.pdf"]
    # without OCR the same file has no searchable text
    con2 = index.connect(str(tmp_path / "j.db"))
    index.import_paths(con2, [tmp_path], workers=1, ocr="off")
    assert index.search(con2, t["invoice_no"]) == []
    # an index built without OCR picks the scan up once OCR is enabled
    rep = index.import_paths(con2, [tmp_path], workers=1)
    assert rep.indexed == 1 and index.search(con2, t["invoice_no"])


def test_short_text_page_is_not_ocrd(tmp_path):
    make_pdf(tmp_path / "short.pdf", [["机械臂 PID"]])
    con = index.connect(str(tmp_path / "i.db"))
    rep = index.import_paths(con, [tmp_path], workers=1)
    assert rep.ocr_pages == 0 and index.search(con, "机械臂")


def test_cli_invoices_with_index(tmp_path, invoices, capsys):
    src, truth = invoices
    db, xlsx = str(tmp_path / "i.db"), str(tmp_path / "out.xlsx")
    main(["--db", db, "invoices", str(src), "-o", xlsx, "--index", "-j", "2"])
    out = capsys.readouterr().out
    assert f"{len(truth)} invoices" in out and "added to search index" in out
    con = index.connect(db)
    seller = truth[0]["seller"]
    assert any(h.path.endswith(truth[0]["file"]) for h in index.search(con, f'"{seller}"'))


def test_web_both_tools(tmp_path, invoices):
    src, truth = invoices
    app = create_app(str(tmp_path / "web.db"))
    c = app.test_client()
    assert "关键词搜索" in c.get("/").get_data(as_text=True)
    assert "发票转 Excel" in c.get("/invoices").get_data(as_text=True)

    # invoices: run on a folder, also index it
    assert c.post("/invoices/run", data={"folder": str(src), "also_index": "1"}).json["ok"]
    rep = _wait(c, "/invoices/status")
    assert rep["invoices"] == len(truth) and rep["indexed"]["indexed"] == len(truth)
    assert {r["file"] for r in rep["rows"]} == {t["file"] for t in truth}
    r = c.get(f"/invoices/download/{rep['download']}")
    assert r.status_code == 200 and r.data[:2] == b"PK"            # xlsx is a zip
    assert c.get("/invoices/download/..%2Fweb.db").status_code == 404
    # the page shows the last result after a reload
    page = c.get("/invoices").get_data(as_text=True)
    assert 'show({"' in page and '"passed":' in page

    # the invoices are now searchable from the search tab
    page = c.get("/", query_string={"q": truth[1]["invoice_no"]}).get_data(as_text=True)
    assert "1 个文件匹配" in page and truth[1]["file"] in page

    # search tab: upload and index a file through the browser path
    data = {"files": [(open(src / truth[0]["file"], "rb"), "上传/发票.pdf")]}
    assert c.post("/index/upload", data=data, content_type="multipart/form-data").json["ok"]
    assert _wait(c, "/index/status")["indexed"] == 1
    assert (tmp_path / "uploads").is_dir()


def test_web_rejects_missing_folder(tmp_path):
    c = create_app(str(tmp_path / "w.db")).test_client()
    assert c.post("/invoices/run", data={"folder": "/no/such/dir"}).status_code == 400
    assert c.post("/index", data={"folder": ""}).status_code == 400
