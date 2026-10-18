"""Repeatable search benchmark using synthetic data in a disposable database only.

Run from the repository root: python -B scripts/benchmark_material_search.py
No production databases, network requests or schema changes are involved.
"""

import argparse
import gc
import json
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coursedeck.knowledge import KnowledgeStore  # noqa: E402


@contextmanager
def synthetic_directory():
    # mkdir's inherited permissions also work under Windows sandbox identities.
    folder = Path(tempfile.gettempdir()) / ("coursedeck-search-benchmark-" + uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        shutil.rmtree(folder)


def benchmark(count, body_chars, repeats):
    with synthetic_directory() as folder:
        store = KnowledgeStore(Path(folder) / "knowledge.sqlite3")
        text = "Linear algebra lecture calculus matrix methods. 微积分课程内容。 "
        body = (text * (body_chars // len(text) + 1))[:body_chars]
        try:
            store.upsert_documents(
                [
                    {
                        "id": f"document-{index:05}",
                        "provider": "brightspace",
                        "course_id": f"course-{index % 20}",
                        "title": f"Lecture {index}",
                        "body": body + (" tailmarker" if index % 100 == 0 else ""),
                        "complete": True,
                        "fetched_at": "2026-01-01T00:00:00+00:00",
                    }
                    for index in range(count)
                ]
            )
            results = {}
            for query in ("linear algebra", "tailmarker", "absentword", '"matrix methods"'):
                store._search_cache = None
                started = time.perf_counter()
                documents = store.library(query)
                cold_ms = (time.perf_counter() - started) * 1000
                samples = []
                for _ in range(repeats):
                    started = time.perf_counter()
                    store.library(query)
                    samples.append((time.perf_counter() - started) * 1000)
                store._search_cache = None
                gc.collect()
                tracemalloc.start()
                store.library(query)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                results[query] = {
                    "cold_ms": round(cold_ms, 2),
                    "repeat_median_ms": round(statistics.median(samples), 2),
                    "cold_python_peak_mib": round(peak / 1024**2, 2),
                    "matches": documents["total"],
                }
            return {"documents": count, "body_chars": body_chars, "queries": results}
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    for count, size in ((100, 6000), (2000, 20000)):
        print(json.dumps(benchmark(count, size, args.repeats), ensure_ascii=True), flush=True)
