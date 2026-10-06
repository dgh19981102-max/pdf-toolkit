import os

import pytest

from pdfgen import make_pdf
from pdftoolkit.search import index
from pdftoolkit.cli import main
from pdftoolkit.search.extract import normalize
from pdftoolkit.web import _safe_name


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    make_pdf(root / "ml.pdf", [
        ["Introduction to machine learn-", "ing and Gaussian processes."],
        ["机器学习是人工", "智能的一个分支。贝叶斯优化"],
        ["Bayesian optimisation tunes PID gains safely."],
    ], title="ML notes")
    make_pdf(root / "sub" / "控制.pdf", [
        ["PID 控制器参数整定", "比例 积分 微分"],
        ["机械臂 负载变化 PID"],
    ])
    make_pdf(root / "sub" / "other.PDF", [["Nothing relevant here, only cooking recipes."]])
    (root / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    (root / "notes.txt").write_text("machine learning")          # not a PDF: ignored
    return root


@pytest.fixture
def db(tmp_path, corpus):
    con = index.connect(str(tmp_path / "idx.db"))
    rep = index.import_paths(con, [corpus], workers=1)
    assert (rep.found, rep.indexed, rep.failed) == (4, 3, 1)
    return con


def names(hits):
    return [os.path.basename(h.path) for h in hits]


def test_normalize_joins_wrapped_lines():
    assert normalize("机器学习是人工\n智能") == "机器学习是人工智能"
    assert normalize("learn-\ning  is\nfun") == "learning is fun"
    assert normalize("PID\n控制") == "PID 控制"


def test_english_phrase_across_line_break(db):
    hits = index.search(db, '"machine learning"')
    assert names(hits) == ["ml.pdf"]
    page, n, snips = hits[0].pages[0]
    assert page == 1 and n == 1
    assert index.search(db, "machine learning")[0].pages[0][1] == 2   # two separate keywords
    assert ("machine learning", True) in snips[0]


def test_case_insensitive(db):
    assert names(index.search(db, "GAUSSIAN")) == ["ml.pdf"]


def test_chinese_long_and_short_terms(db):
    assert names(index.search(db, "人工智能")) == ["ml.pdf"]        # wrapped across lines
    assert names(index.search(db, "机械臂")) == ["控制.pdf"]
    assert sorted(names(index.search(db, "机器"))) == ["ml.pdf"]   # 2 chars: LIKE fallback
    assert names(index.search(db, "积分")) == ["控制.pdf"]


def test_and_or_exclude(db):
    assert sorted(names(index.search(db, "PID"))) == ["ml.pdf", "控制.pdf"]
    assert names(index.search(db, "PID 机械臂")) == ["控制.pdf"]
    assert names(index.search(db, "PID -机械臂")) == ["ml.pdf"]   # excludes whole document
    assert names(index.search(db, "PID -机器")) == ["控制.pdf"]   # short excluded term
    assert sorted(names(index.search(db, index.parse_query("cooking 机械臂", any_mode=True)))) \
        == ["other.PDF", "控制.pdf"]


def test_quoted_phrase_and_path_filter(db):
    assert names(index.search(db, '"PID gains"')) == ["ml.pdf"]
    assert names(index.search(db, "PID", path_filter="sub")) == ["控制.pdf"]


def test_ranking_by_match_count(db):
    hits = index.search(db, "PID")
    assert names(hits)[0] == "控制.pdf" and hits[0].n_pages == 2 and hits.total_docs == 2
    assert [p for p, _, _ in hits[0].pages] == [1, 2]


def test_limit_and_show_pages(db):
    hits = index.search(db, "PID", limit=1, show_pages=1)
    assert len(hits) == 1 and hits.total_docs == 2
    assert hits[0].n_pages == 2 and [p for p, _, _ in hits[0].pages] == [1]


def test_special_characters_do_not_break_queries(db):
    for q in ['"', "a%b", "_", "50%", "OR", "NOT AND", "(x)", "*", "'"]:
        index.search(db, q)


def test_parse_query():
    q = index.parse_query('alpha "beta gamma" -delta -"eps zeta" -')
    assert q.include == ["alpha", "beta gamma", "-"]
    assert q.exclude == ["delta", "eps zeta"]


def test_incremental_reimport_and_prune(db, corpus):
    rep = index.import_paths(db, [corpus], workers=1)
    assert (rep.indexed, rep.unchanged, rep.failed) == (0, 3, 1)   # broken file retried

    target = corpus / "sub" / "other.PDF"
    make_pdf(target, [["Now about Kalman filters."]])
    st = os.stat(target)
    os.utime(target, (st.st_atime, st.st_mtime + 5))
    rep = index.import_paths(db, [corpus], workers=1)
    assert rep.indexed == 1
    assert names(index.search(db, "kalman")) == ["other.PDF"]
    assert index.search(db, "cooking") == []

    # touching a file without changing it does not re-index
    os.utime(corpus / "ml.pdf", None)
    rep = index.import_paths(db, [corpus], workers=1)
    assert rep.indexed == 0 and rep.unchanged == 3

    (corpus / "ml.pdf").unlink()
    rep = index.import_paths(db, [corpus], workers=1, prune=True)
    assert rep.removed == 1
    assert index.search(db, "Gaussian") == []
    assert db.execute("SELECT COUNT(*) FROM page_text").fetchone()[0] == 3


def test_parallel_import_matches_serial(tmp_path, corpus):
    con = index.connect(str(tmp_path / "par.db"))
    rep = index.import_paths(con, [corpus], workers=3)
    assert (rep.indexed, rep.failed, rep.pages) == (3, 1, 6)
    assert names(index.search(con, "人工智能")) == ["ml.pdf"]


def test_stats_reports_failed_files(db):
    s = index.stats(db)
    assert s["documents"] == 4 and s["pages"] == 6 and s["failed"] == 1
    assert s["errors"][0]["path"].endswith("broken.pdf")


def test_cli(tmp_path, corpus, capsys):
    dbp = str(tmp_path / "cli.db")
    main(["--db", dbp, "import", str(corpus), "-q", "-j", "1"])
    assert "3 indexed" in capsys.readouterr().out
    main(["--db", dbp, "search", "贝叶斯优化"])
    out = capsys.readouterr().out
    assert "ml.pdf" in out and "[贝叶斯优化]" in out and "p.2" in out
    main(["--db", dbp, "search", "PID", "-机械臂", "--any"])      # "-word" is a keyword
    out = capsys.readouterr().out
    assert "ml.pdf" in out and "控制.pdf" not in out
    main(["--db", dbp, "search", "nothingmatches"])
    assert "no matches" in capsys.readouterr().out


def test_upload_keeps_chinese_names_and_cannot_escape(tmp_path):
    assert _safe_name("报告 2024.pdf") == "报告 2024.pdf"
    assert _safe_name("..") == "_"
    assert _safe_name('a<b>:c|.pdf') == "a_b__c_.pdf"
