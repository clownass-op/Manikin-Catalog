#!/usr/bin/env python3
"""Snapshot the Supabase `swfs` catalog to catalog.json.gz (+ catalog.meta.json).

The catalog is read-only and the app fetches all of it at launch, so a gzipped
copy in a public repo is a free mirror: raw.githubusercontent.com has no egress
quota, while every launch against PostgREST spends the project's 5 GB/month.

The output is the exact row shape the app already parses (`Wardrobe.AddRow`):

    [{"name": "...", "path": "items/helms/x.swf", "category": "Helm",
      "item_group": "Helm", "link": "...", "meta": "{...}"}, ...]

Stdlib only - no pip install, no jq - so the workflow is one `python snapshot.py`.

Environment:
    SUPABASE_URL   project URL (https://xxxx.supabase.co) or the REST URL
    SUPABASE_KEY   publishable/anon key (the table is SELECT-only for anon)

Usage:
    python snapshot.py [--out DIR]
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE = 1000          # PostgREST rows per request (Supabase's default max)
RETRIES = 4          # transient 5xx/timeouts per page
MAX_PAGES = 400      # sanity cap: 400k rows
COLUMNS = "name,path,category,item_group,link,meta"


def fail(message):
    print("error: " + message, file=sys.stderr)
    sys.exit(1)


def endpoint():
    base = (os.environ.get("SUPABASE_URL") or "").strip()
    if not base:
        fail("SUPABASE_URL is not set")
    base = base.rstrip("/")
    if base.endswith("/rest/v1/swfs"):
        return base
    if base.endswith("/rest/v1"):
        return base + "/swfs"
    return base + "/rest/v1/swfs"


def fetch_page(ep, key, offset):
    query = urllib.parse.urlencode({
        "select": COLUMNS,
        "order": "path.asc",      # stable order: unchanged data => identical bytes
        "limit": PAGE,
        "offset": offset,
    })
    url = ep + "?" + query
    last = None
    for attempt in range(RETRIES):
        try:
            request = urllib.request.Request(url, headers={
                "apikey": key,
                "Authorization": "Bearer " + key,
                "Accept": "application/json",
                "User-Agent": "manikin-catalog-snapshot/1.0",
            })
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, list):
                raise ValueError("expected a JSON array, got %r" % type(body).__name__)
            return body
        except Exception as exc:            # noqa: BLE001 - retry any transient fault
            last = exc
            if attempt == RETRIES - 1:
                break
            wait = 2 ** attempt
            print("  page at %d failed (%s), retrying in %ds" % (offset, exc, wait),
                  file=sys.stderr)
            time.sleep(wait)
    fail("page at offset %d failed: %s" % (offset, last))


def read_previous_count(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("count") or 0)
    except Exception:
        return 0


def read_existing_rows(gz_path):
    try:
        with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def read_existing_raw(gz_path):
    try:
        with gzip.open(gz_path, "rb") as handle:
            return handle.read()
    except Exception:
        return b""


def main():
    parser = argparse.ArgumentParser(description="Snapshot the Supabase swfs catalog.")
    parser.add_argument("--out", default=".", help="directory to write into (default: .)")
    args = parser.parse_args()

    key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not key:
        fail("SUPABASE_KEY is not set")
    ep = endpoint()

    rows = []
    offset = 0
    while offset < MAX_PAGES * PAGE:
        page = fetch_page(ep, key, offset)
        rows.extend(page)
        print("  %d rows" % len(rows), flush=True)
        if len(page) < PAGE:
            break
        offset += PAGE

    if not rows:
        fail("the API returned no rows - refusing to commit an empty catalog")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    gz_path = os.path.join(out_dir, "catalog.json.gz")
    meta_path = os.path.join(out_dir, "catalog.meta.json")

    previous = read_previous_count(meta_path)
    if previous and len(rows) < previous * 0.8:
        fail("only %d rows, down from %d (%.0f%%) - refusing what looks like a "
             "truncated read" % (len(rows), previous, 100.0 * len(rows) / previous))

    # Union with what the file already holds: items added straight to the repo
    # (CatalogSync, Settings > Catalog) must survive a refresh from Supabase. On
    # a shared path the table wins, so edits made there still land here.
    merged = {}
    for row in read_existing_rows(gz_path):
        if row.get("path"):
            merged[row["path"]] = row
    for row in rows:
        if row.get("path"):
            merged[row["path"]] = row
    if not merged:
        fail("the mirror and the API are both empty - nothing to publish")

    # Fixed order so unchanged data is byte-identical and commits stay quiet.
    rows = sorted(merged.values(), key=lambda r: ((r.get("path") or ""), (r.get("category") or ""),
                                                  (r.get("name") or "")))

    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Compare uncompressed content: Python and the app gzip differently, so the
    # compressed bytes alone would look like a change after a GitHub-side edit.
    if raw == read_existing_raw(gz_path):
        print("catalog unchanged (%d rows) - nothing to write" % len(rows))
        return

    tmp_path = gz_path + ".tmp"
    with open(tmp_path, "wb") as handle:
        # filename="" and mtime=0 keep the gzip header reproducible.
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle,
                           compresslevel=9, mtime=0) as gz:
            gz.write(raw)
    os.replace(tmp_path, gz_path)

    meta = {
        "count": len(rows),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": "public.swfs",
    }
    with open(meta_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print("wrote %d rows: %.1f MB raw -> %.2f MB gzipped"
          % (len(rows), len(raw) / 1e6, os.path.getsize(gz_path) / 1e6))


if __name__ == "__main__":
    main()
