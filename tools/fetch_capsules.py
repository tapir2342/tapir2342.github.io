#!/usr/bin/env python3
"""Harvest Steam capsule references and bake them into capsules.html.

Two Steam endpoints, each for what it is authoritative about:

  * store search  -- which appids sit in each store-front tab. Returns the rows
    the store itself renders, so the tab membership is Steam's, not a guess.
  * IStoreBrowseService/GetItems -- the capsule asset path per app. This is the
    same data the store client reads, and it is the only reliable source: the
    path carries a per-asset revision hash that cannot be computed, some apps
    use an alternate filename (capsule_231x87_alt_assets_N.jpg), and a 2x
    variant exists for only some apps. Scraping the URL out of search HTML gets
    the first case wrong and cannot answer the third at all.

Neither endpoint sends CORS headers, so the page cannot call them at runtime --
hence baking the result in here.

Every run ADDS to the baked pool; nothing is ever removed. A capsule that has
rotated off the store front is still one you may have rated, and its rating is
keyed by appid in the browser, so dropping it from the pool would make that
rating invisible. Apps seen in a run get their asset path refreshed, because
reissued art changes the hash and the old path starts 404ing.

Usage:  python3 tools/fetch_capsules.py [--per-preset 200] [--dry-run]
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SEARCH = "https://store.steampowered.com/search/results/"
GETITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "capsules.html"

BEGIN = "/*BEGIN_POOL*/"
END = "/*END_POOL*/"

# The store front's own tabs. Note that search silently ignores an unknown
# filter value and returns the whole catalogue instead of erroring, so these
# names were each verified by their distinct total_count -- do not add one
# without checking it actually narrows the result set.
PRESETS = [
    ("popnew", {"filter": "popularnew", "sort_by": "Released_DESC"}),
    ("top", {"filter": "topsellers"}),
    ("upcoming", {"filter": "popularcomingsoon"}),
    ("specials", {"specials": "1"}),
    ("free", {"maxprice": "free"}),
]

# category1=998 restricts to games (no DLC, soundtracks, videos, hardware).
BASE = {"query": "", "dynamic_data": "", "category1": "998", "infinite": "1", "json": "1"}

RE_APPID = re.compile(r'data-ds-appid="(\d+)"')


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "capsule-review-tool/1.0"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search(params):
    return get_json(SEARCH + "?" + urllib.parse.urlencode({**BASE, **params}))


def collect_ids(per_preset):
    """Appids per store-front tab, in store order."""
    # Baseline: an unfiltered query. A preset whose total matches this one was
    # silently ignored by Steam and would harvest the whole catalogue.
    baseline = search({"start": "0", "count": "1"}).get("total_count")
    ids = []
    seen = set()
    for name, params in PRESETS:
        got = 0
        for start in range(0, per_preset, 100):
            data = search({**params, "start": str(start), "count": "100"})
            if start == 0 and data.get("total_count") == baseline:
                print(f"{name}: filter ignored by Steam (total == unfiltered) -- skipped", file=sys.stderr)
                break
            found = RE_APPID.findall(data.get("results_html", ""))
            if not found:
                break
            for raw in found:
                appid = int(raw)
                if appid not in seen:     # a game can sit in several tabs
                    seen.add(appid)
                    ids.append(appid)
            got += len(found)
            time.sleep(1.5)               # be a polite client
        print(f"{name}: {got} rows", file=sys.stderr)
    return ids


def fetch_assets(ids, batch=100):
    """appid -> (small_capsule path, has 2x variant), straight from Steam."""
    out = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        payload = {
            "ids": [{"appid": a} for a in chunk],
            "context": {"language": "english", "country_code": "DE"},
            "data_request": {"include_assets": True},
        }
        data = get_json(GETITEMS + "?" + urllib.parse.urlencode({"input_json": json.dumps(payload)}))
        for item in data.get("response", {}).get("store_items", []):
            assets = item.get("assets") or {}
            small = assets.get("small_capsule")
            if small:
                out[item["appid"]] = (small, bool(assets.get("small_capsule_2x")))
        print(f"assets {min(i + batch, len(ids))}/{len(ids)}", file=sys.stderr)
        time.sleep(1.0)
    return out


RE_POOL = re.compile(r"const POOL = \[(.*?)\];", re.S)
RE_CAP = re.compile(r"const CAP = \{(.*?)\};", re.S)
RE_HI = re.compile(r"const HI = \[(.*?)\];", re.S)


def read_existing(block):
    """Parse the previously baked pool so a harvest can add to it."""
    ids, cap, hi = [], {}, set()
    m = RE_POOL.search(block)
    if m:
        ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
    m = RE_CAP.search(block)
    if m:
        cap = {int(a): p for a, p in re.findall(r'(\d+):"([^"]+)"', m.group(1))}
    m = RE_HI.search(block)
    if m:
        hi = {int(x) for x in re.findall(r"\d+", m.group(1))}
    return ids, cap, hi


def wrap(values, width=100):
    """Wrap a comma-joined list so the baked block stays diffable."""
    lines, cur = [], ""
    for v in values:
        piece = v + ","
        if len(cur) + len(piece) > width:
            lines.append(cur)
            cur = ""
        cur += piece
    if cur:
        lines.append(cur)
    return "\n".join(lines).rstrip(",")


def bake(ids, assets):
    """Merge this harvest into the baked pool. The pool only ever grows.

    A capsule that has rotated off the store front is still a capsule you may
    have rated, so nothing is ever dropped. Apps seen in this run get their
    asset path refreshed, since reissued art changes the hash and the old path
    starts 404ing.
    """
    text = PAGE.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        sys.exit(f"marker {BEGIN}/{END} not found in {PAGE}")
    head, rest = text.split(BEGIN, 1)
    block, tail = rest.split(END, 1)

    old_ids, cap, hi = read_existing(block)
    known = set(old_ids)
    fresh = [i for i in ids if i in assets and i not in known]
    updated = 0

    for i in ids:
        if i not in assets:
            continue
        path, has_hi = assets[i]
        if i in known and cap.get(i) != path:
            updated += 1
        cap[i] = path
        hi.discard(i)
        if has_hi:
            hi.add(i)

    # Existing order is preserved and new appids are appended, so a refresh
    # touches the tail of the block rather than reflowing all of it.
    merged = old_ids + fresh
    hi_list = [i for i in merged if i in hi]
    body = (
        "\nconst POOL = [\n" + wrap([str(i) for i in merged]) + "\n];\n"
        "const CAP = {\n" + wrap([f'{i}:"{cap[i]}"' for i in merged]) + "\n};\n"
        "const HI = [\n" + wrap([str(i) for i in hi_list]) + "\n];\n"
    )
    PAGE.write_text(head + BEGIN + body + END + tail, encoding="utf-8")

    missing = len([i for i in ids if i not in assets])
    print(f"pool {len(old_ids)} -> {len(merged)} capsules "
          f"(+{len(fresh)} new, {updated} refreshed, {len(hi_list)} with a 2x variant"
          + (f", {missing} skipped for missing assets" if missing else "") + ")", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-preset", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = collect_ids(args.per_preset)
    assets = fetch_assets(ids)
    if args.dry_run:
        for i in ids[:5]:
            print(i, assets.get(i))
        print(f"{len(ids)} ids, {len(assets)} with assets", file=sys.stderr)
    else:
        bake(ids, assets)
