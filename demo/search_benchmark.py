"""Generate a synthetic corpus and measure import throughput and search latency.

    python scripts/benchmark.py --docs 1000 --pages 20 --out bench
"""
import argparse
import os
import random
import shutil
import statistics
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from pdfgen import make_pdf          # noqa: E402
from pdftoolkit.search import index         # noqa: E402

EN = ("control system robot arm gain tuning safety optimisation gaussian process kernel model "
      "data experiment payload torque sensor noise filter trajectory error estimate").split()
ZH = list("机器学习控制系统机械臂参数整定安全优化高斯过程模型数据实验负载力矩传感器噪声滤波轨迹误差估计")


def _line(rng):
    if rng.random() < 0.5:
        return " ".join(rng.choice(EN) for _ in range(10))
    return "".join(rng.choice(ZH) for _ in range(30))


def _make(args):
    i, out, pages = args
    rng = random.Random(i)
    body = [[_line(rng) for _ in range(40)] for _ in range(pages)]
    if i % 100 == 0:
        body[rng.randrange(pages)].append(f"needle-{i} 独特标记{i}")
    make_pdf(Path(out) / f"d{i // 100:03d}" / f"doc{i:05d}.pdf", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=1000)
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--out", default="bench")
    ap.add_argument("--workers", type=int, default=None)
    a = ap.parse_args()
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    for d in range(a.docs // 100 + 1):
        (out / "pdfs" / f"d{d:03d}").mkdir(parents=True, exist_ok=True)

    t = time.time()
    with Pool() as p:
        p.map(_make, [(i, out / "pdfs", a.pages) for i in range(a.docs)], chunksize=8)
    size = sum(f.stat().st_size for f in (out / "pdfs").rglob("*.pdf")) / 1e6
    print(f"generated {a.docs} PDFs x {a.pages} pages ({size:.0f} MB) in {time.time() - t:.0f}s")

    db = str(out / "bench.db")
    con = index.connect(db)
    rep = index.import_paths(con, [out / "pdfs"], workers=a.workers)
    print(f"import: {rep.indexed} docs, {rep.pages} pages in {rep.seconds:.1f}s "
          f"({rep.pages / rep.seconds:.0f} pages/s, {os.cpu_count()} CPUs), "
          f"index {os.path.getsize(db) / 1e6:.0f} MB")
    rep = index.import_paths(con, [out / "pdfs"], workers=a.workers)
    print(f"re-import (nothing changed): {rep.seconds:.2f}s")

    queries = ["needle-500", "独特标记300", "gaussian process", "机械臂", "kalman",
               "torque -noise", "误差"]
    for q in queries:
        times = []
        for _ in range(5):
            t = time.perf_counter()
            hits = index.search(con, q, limit=20)
            times.append((time.perf_counter() - t) * 1000)
        print(f"  {q!r:22} {len(hits):4d} docs  median {statistics.median(times):7.1f} ms")


if __name__ == "__main__":
    main()
