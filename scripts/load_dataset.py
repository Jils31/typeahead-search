"""Dataset ingestion -> Postgres.

Primary dataset: AOL search query log (Kaggle: dineshydv/aol-user-session-
collection-500k). Columns include `Query`; we derive counts by AGGREGATION
(COUNT(*) per normalized query) — exactly the assignment's "derive counts if
the dataset has none". The query-popularity distribution is naturally Zipf,
which lets the performance report demonstrate the Pareto caching argument.

Usage (from project root, venv active):
  python -m scripts.load_dataset --file files/user-ct-test-collection-01.txt
  python -m scripts.load_dataset --synthetic 120000        # no download needed

The loader auto-detects delimiter (AOL files are TAB-separated) and the Query
column from the header.
"""
import argparse
import asyncio
import csv
import glob
import gzip
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import store  # noqa: E402


def _open(path: str):
    """Open .gz transparently."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore", newline="")
    return open(path, "r", encoding="utf-8", errors="ignore", newline="")


def _aggregate_into(path: str, counter: Counter) -> None:
    with _open(path) as f:
        sample = f.read(4096)
        f.seek(0)
        delim = "\t" if sample.count("\t") >= sample.count(",") else ","
        reader = csv.reader(f, delimiter=delim)
        header = next(reader, None)
        if not header:
            return
        lowered = [h.strip().lower() for h in header]
        try:
            qi = lowered.index("query")
        except ValueError:
            qi = 1 if len(header) > 1 else 0  # AOL: AnonID, Query, ...
        n = 0
        for row in reader:
            if len(row) <= qi:
                continue
            q = row[qi].strip().lower()
            if not q or q == "-":
                continue
            counter[q] += 1
            n += 1
        print(f"  {Path(path).name}: {n:,} query rows (delim={delim!r}, col={qi})")


def aggregate_paths(paths: list[str], min_count: int) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for p in paths:
        _aggregate_into(p, counter)
    return [(q, c) for q, c in counter.items() if c >= min_count]


def write_tsv(rows: list[tuple[str, int]], path: str) -> None:
    """Persist aggregated (query, count) so the expensive aggregation is reusable."""
    with open(path, "w", encoding="utf-8") as f:
        for q, c in rows:
            f.write(f"{q}\t{c}\n")


def read_agg(path: str, top: int | None, min_count: int) -> list[tuple[str, int]]:
    """Stream a pre-aggregated query<TAB>count file (low memory). Optionally keep
    only the top-N by count."""
    rows: list[tuple[str, int]] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue
            q, c = parts[0], int(parts[1])
            if q and c >= min_count:
                rows.append((q, c))
    if top:
        rows.sort(key=lambda x: x[1], reverse=True)
        rows = rows[:top]
    return rows


def synthetic(n: int) -> list[tuple[str, int]]:
    """Generate ~n unique queries with Zipf-distributed counts (no download)."""
    heads = ["how to", "best", "buy", "cheap", "free", "download", "what is",
             "iphone", "samsung", "java", "python", "amazon", "google", "weather",
             "news", "movie", "song", "recipe", "near me", "online", "review"]
    tails = ["tutorial", "price", "2026", "review", "online", "near me", "for sale",
             "guide", "vs", "app", "login", "meaning", "today", "free", "pro max",
             "case", "charger", "stock", "results", "live", "download", "lyrics"]
    mids = ["", "best ", "new ", "cheap ", "top ", "the "]
    out: dict[str, int] = {}
    rank = 1
    i = 0
    while len(out) < n:
        h = heads[i % len(heads)]
        m = mids[(i // len(heads)) % len(mids)]
        t = tails[(i // (len(heads) * len(mids))) % len(tails)]
        suffix = f" {i // 5000}" if i >= 5000 else ""
        q = f"{h} {m}{t}{suffix}".strip()
        if q not in out:
            count = max(1, int(10_000_000 / (rank ** 1.1)))  # Zipf-ish
            out[q] = count
            rank += 1
        i += 1
    return list(out.items())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="path to a single AOL query log (.txt or .txt.gz)")
    ap.add_argument("--dir", help="directory of AOL files (loads all *.txt and *.txt.gz)")
    ap.add_argument("--synthetic", type=int, help="generate N synthetic queries instead")
    ap.add_argument("--agg-file", help="load a pre-aggregated query<TAB>count file (low memory)")
    ap.add_argument("--out", help="aggregate to this TSV file and exit (no DB write)")
    ap.add_argument("--top", type=int, help="keep only the top-N queries by count")
    ap.add_argument("--min-count", type=int, default=1, help="drop queries below this count")
    ap.add_argument("--no-truncate", action="store_true", help="append instead of replace")
    args = ap.parse_args()

    if not any([args.file, args.dir, args.synthetic, args.agg_file]):
        raise SystemExit("provide --file, --dir, --agg-file, or --synthetic")

    print("aggregating...")
    if args.synthetic:
        rows = synthetic(args.synthetic)
    elif args.agg_file:
        rows = read_agg(args.agg_file, args.top, args.min_count)
    else:
        if args.dir:
            paths = sorted(glob.glob(f"{args.dir}/*.txt.gz") + glob.glob(f"{args.dir}/*.txt"))
            if not paths:
                raise SystemExit(f"no .txt/.txt.gz files in {args.dir}")
        else:
            paths = [args.file]
        rows = aggregate_paths(paths, args.min_count)
        if args.top:
            rows.sort(key=lambda x: x[1], reverse=True)
            rows = rows[: args.top]

    # aggregate-only mode: persist to TSV and stop (DB not required)
    if args.out:
        write_tsv(rows, args.out)
        print(f"wrote {len(rows):,} rows to {args.out}")
        return
    print(f"{len(rows):,} distinct queries to load")
    if len(rows) < 100_000:
        print(f"WARNING: only {len(rows):,} queries (< 100k assignment minimum)")

    await store.init_pool()
    try:
        await store.init_schema()
        if not args.no_truncate:
            await store.truncate()
        await store.bulk_load(rows)
        total = await store.count_rows()
        print(f"done. rows in DB: {total:,}")
    finally:
        await store.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
