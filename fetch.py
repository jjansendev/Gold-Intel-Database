#!/usr/bin/env python3
"""Hourly snapshot of the WoW: Forever auction houses from the Blizzard Game Data API.

Runs in GitHub Actions (see .github/workflows/snapshot.yml) and writes into OUT_DIR:
  manifest.json                          what's here, for Gold Intel to download
  snapshots/<house>/<scope>-<ms>.json.gz the Blizzard payloads, byte-for-byte, gzipped
  items.json.gz                          item names/quality/vendor price (refreshed every <25 days)
  history/<house>/<day>.json.gz          one row per item per finished UTC day (prices + demand), kept
                                         --history-days (default 30), so Gold Intel can fill days its
                                         computer was off and new installs start with a month of history
  history/partial/<house>/<day>.json.gz  the day in progress (per-snapshot stats), folded into the above

Only stdlib. Credentials come from BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET and are never
printed. Snapshots older than --keep-hours are dropped (Blizzard's API terms: refresh or delete
data within 30 days), and the publishing step force-pushes a single commit so the repo stays small.

Endpoint shape is undocumented by Blizzard (community-tested 2026-11-24):
  namespace dynamic-forever-{region}; /data/wow/ruleset/{id}/auctions/ (trailing slash required)
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from statistics import median

FORMAT = 1
TOKEN_URL = "https://oauth.battle.net/token"
UA = "ForeverGoldIntel-DataRepo/1.0"
ITEM_TTL_MS = 25 * 86_400_000
DAY_MS = 86_400_000
HISTORY_FORMAT = 1
FINALIZE_GRACE_MS = 3 * 3_600_000      # a day is folded once its last hour has surely been fetched
MAX_PAIR_GAP_MS = 48 * 3_600_000
EARLY = ("LONG", "VERY_LONG")
DAY_FIELDS = ["min_unit", "p10_unit", "median_unit", "mean_unit", "max_unit", "market_value",
              "units_listed", "listings", "snapshots", "outliers_removed", "thin"]
ATTRIBUTION = ("Auction data from the Blizzard Battle.net API (World of Warcraft: Forever). "
               "Not affiliated with or endorsed by Blizzard Entertainment. Free to use; never sold.")


def now_ms() -> int:
    return int(time.time() * 1000)


class Api:
    def __init__(self, region: str, client_id: str, secret: str, opener=urllib.request.urlopen) -> None:
        self.region, self.base = region, f"https://{region}.api.blizzard.com"
        self._cid, self._sec, self._open = client_id, secret, opener
        self._token: str | None = None

    def _req(self, url: str, headers: dict, data: bytes | None = None, tries: int = 4):
        h = {"User-Agent": UA, "Accept-Encoding": "gzip", **headers}
        for attempt in range(tries):
            try:
                with self._open(urllib.request.Request(url, data=data, headers=h), timeout=90) as r:
                    body = r.read()
                    hdrs = {k.lower(): v for k, v in r.headers.items()}
                    if hdrs.get("content-encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body, hdrs
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None, {}
                if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                    raise RuntimeError(f"HTTP {e.code} for {urllib.parse.urlsplit(url).path}") from None
                wait = float(e.headers.get("retry-after", 0) or 0) if e.headers else 0
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == tries - 1:
                    raise RuntimeError(f"network error for {urllib.parse.urlsplit(url).path}: {e}") from None
                wait = 0
            time.sleep(min(60.0, max(wait, 2.0 * 2 ** attempt)))
        raise RuntimeError("unreachable")

    def auth(self) -> dict:
        if not self._token:
            basic = base64.b64encode(f"{self._cid}:{self._sec}".encode()).decode()
            body, _ = self._req(TOKEN_URL, {"Authorization": f"Basic {basic}",
                                            "Content-Type": "application/x-www-form-urlencoded"},
                                b"grant_type=client_credentials")
            if body is None:
                raise RuntimeError("token endpoint returned 404")
            self._token = json.loads(body)["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path: str, namespace: str):
        q = urllib.parse.urlencode({"namespace": namespace, "locale": "en_US"})
        return self._req(f"{self.base}{path}?{q}", self.auth())


def last_modified_ms(v: str | None) -> int | None:
    try:
        return int(parsedate_to_datetime(v).timestamp() * 1000) if v else None
    except (TypeError, ValueError):
        return None


def gz(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def load_manifest(out: Path) -> dict:
    try:
        m = json.loads((out / "manifest.json").read_text())
        return m if m.get("format") == FORMAT else {}
    except (OSError, ValueError):
        return {}


def load_items(out: Path) -> dict:
    try:
        return json.loads(gzip.decompress((out / "items.json.gz").read_bytes()))
    except (OSError, ValueError):
        return {}


def item_ids(payload: bytes) -> set[int]:
    try:
        doc = json.loads(payload)
    except ValueError:
        return set()
    out = set()
    for a in doc.get("auctions") or []:
        iid = (a.get("item") or {}).get("id")
        if isinstance(iid, int):
            out.add(iid)
    return out


def fetch_item(api: Api, iid: int, now: int) -> dict:
    for ns, src in ((f"static-forever-{api.region}", "blizzard_static_forever"),
                    (f"static-{api.region}", "blizzard_static_retail")):
        body, _ = api.get(f"/data/wow/item/{iid}", ns)
        if body is not None:
            d = json.loads(body)
            q = (d.get("quality") or {}).get("type")
            return {"name": d.get("name"), "quality": {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3,
                                                       "EPIC": 4, "LEGENDARY": 5}.get(q),
                    "level": d.get("level"), "required_level": d.get("required_level"),
                    "sell_price": d.get("sell_price"), "source": src, "fetched_at": now,
                    "class": (d.get("item_class") or {}).get("id"), "subclass": (d.get("item_subclass") or {}).get("id")}
    return {"missing": True, "fetched_at": now}


# ── daily history ─────────────────────────────────────────────────────────────────────────────
# Mirrors Gold Intel's own rules exactly (core/goldintel/market/stats.py snapshot_stats,
# history.py rollup_day, demand.py record_pair); core/tests/test_hub_history.py checks parity.

def _variant(item: dict) -> str | None:
    bl, mods = item.get("bonus_lists") or [], item.get("modifiers") or []
    if not bl and not mods:
        return None
    mod_s = ",".join(f"{m.get('type')}={m.get('value')}" for m in sorted(mods, key=lambda m: (m.get("type"), m.get("value"))))
    return "bz:" + ".".join(str(b) for b in sorted(bl)) + ";" + mod_s


def _int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    raise ValueError(v)


def parse_house(raw: bytes) -> tuple[dict, dict]:
    """-> ({item_key: [(unit, qty), ...]} buyout listings, {auction_id: (item_key, qty, time_left)})."""
    listings, refs = {}, {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return listings, refs
    for a in (doc.get("auctions") or []) if isinstance(doc, dict) else []:
        try:
            item = a.get("item") or {}
            aid, iid, qty = _int(a.get("id")), _int(item.get("id")), _int(a.get("quantity"))
            if aid is None or iid is None or qty is None or iid <= 0:
                continue
            unit, buyout = _int(a.get("unit_price")), _int(a.get("buyout"))
            if unit is None and buyout is not None:
                unit = buyout // qty if qty > 0 else None
            v = _variant(item)
            key = str(iid) if not v else f"{iid}|{v}"
            tl = str(a.get("time_left") or "UNKNOWN").upper()
        except (ValueError, TypeError, AttributeError):
            continue
        refs[f"bz:{aid}"] = (key, qty, tl)
        if unit is not None:
            listings.setdefault(key, []).append((unit, qty))
    return listings, refs


def _wq(vals, wts, q):
    pairs = sorted(zip(vals, wts))
    total = sum(w for _, w in pairs)
    target, acc = q * total, 0.0
    for v, w in pairs:
        acc += w
        if acc >= target - 1e-12:
            return v
    return pairs[-1][0]


def snapshot_stats(rows, floor_fraction=0.15, min_units=5, min_listings=3):
    """-> [min, p10, median, mean, max, market_value, units, listings, outliers_low+high, thin] or None."""
    import math
    priced = [(u, q) for u, q in rows if u and u > 0 and q > 0]
    if not priced:
        return None
    units, n_list = sum(q for _, q in priced), len(priced)
    vals, w = [u for u, _ in priced], [q for _, q in priced]
    kept, out = priced, 0
    if len(priced) >= 3:
        logs = [math.log(u) for u in vals]
        med = _wq(logs, w, 0.5)
        mad = _wq([abs(x - med) for x in logs], w, 0.5)
        kept = []
        if mad < 1e-12:
            lim = math.log(10.0)
            for r, x in zip(priced, logs):
                (kept.append(r) if med - lim <= x <= med + lim else None)
        else:
            mad = max(mad, 0.05)
            for r, x in zip(priced, logs):
                if abs(0.6745 * (x - med) / mad) <= 3.5:
                    kept.append(r)
        out = len(priced) - len(kept)
        kept = kept or priced
    ks = sorted(kept)
    take = max(1, math.ceil(sum(q for _, q in ks) * floor_fraction))
    acc_u = acc_v = 0
    for u, q in ks:
        t = min(q, take - acc_u)
        acc_u += t
        acc_v += t * u
        if acc_u >= take:
            break
    return [min(vals), round(_wq(vals, w, 0.10)), round(_wq(vals, w, 0.5)),
            round(sum(v * q for v, q in zip(vals, w)) / units), max(vals), round(acc_v / acc_u),
            units, n_list, out, 1 if (units < min_units or n_list < min_listings) else 0]


def fold_day(stats: list[list[int]]) -> list[int]:
    """Daily row from per-snapshot stats, as Gold Intel's rollup_day does for one source."""
    col = list(zip(*stats))
    return [min(col[0]), round(median(col[1])), round(median(col[2])), round(median(col[3])), max(col[4]),
            round(median(col[5])), round(sum(col[6]) / len(stats)), round(sum(col[7]) / len(stats)),
            len(stats), sum(col[8]), 1 if median(col[9]) >= 0.5 else 0]


def _read_gz_json(p: Path) -> dict:
    try:
        return json.loads(gzip.decompress(p.read_bytes()))
    except (OSError, ValueError):
        return {}


def update_history(out: Path, snaps: list[dict], houses: list[dict], old: list[dict], t0: int,
                   history_days: int, errors: list[str]) -> list[dict]:
    """Fold new snapshots into each house's day-in-progress, finish days that are over, expire old ones.
    Returns the manifest's history list."""
    names = {h["id"]: h["name"] for h in houses}
    hist = {(h["house_id"], h["day"]): h for h in old if (out / h["path"]).exists()}
    partials: dict[tuple[int, int], dict] = {}

    def partial(house: int, day: int) -> dict:
        k = (house, day)
        if k not in partials:
            p = out / f"history/partial/{house}/{day}.json.gz"
            partials[k] = _read_gz_json(p) or {"day": day, "house_id": house, "snaps": [], "items": {}, "demand": {}}
        return partials[k]

    for f in (out / "history/partial").glob("*/*.json.gz") if (out / "history/partial").exists() else []:
        partial(int(f.parent.name), int(f.name.split(".")[0]))

    parsed: dict[str, tuple[dict, dict]] = {}

    def load(s: dict) -> tuple[dict, dict]:
        if s["path"] not in parsed:
            parsed[s["path"]] = parse_house(gzip.decompress((out / s["path"]).read_bytes()))
        return parsed[s["path"]]

    by_series: dict[tuple[int, str], list[dict]] = {}
    for s in sorted(snaps, key=lambda s: s["last_modified"]):
        by_series.setdefault((s["house_id"], s["scope"]), []).append(s)
    for series in by_series.values():
        for i, s in enumerate(series):
            day = s["last_modified"] // DAY_MS
            sid = f"{s['scope']}-{s['last_modified']}"
            if (s["house_id"], day) in hist:
                continue                                    # that day is already finished
            d = partial(s["house_id"], day)
            if sid in d["snaps"]:
                continue
            try:
                listings, refs = load(s)
                for key, rows in listings.items():
                    st = snapshot_stats(rows)
                    if st:
                        d["items"].setdefault(key, []).append(st)
                prev = series[i - 1] if i else None
                if prev and 0 < s["last_modified"] - prev["last_modified"] <= MAX_PAIR_GAP_MS:
                    gap = s["last_modified"] - prev["last_modified"]
                    _, before = load(prev)
                    removed: dict[str, int] = {}
                    for ref, (key, qty, tl) in before.items():
                        removed[key] = removed.get(key, 0) + (qty if ref not in refs and tl in EARLY else 0)
                    for key, _q, _t in refs.values():
                        removed.setdefault(key, 0)
                    for key, n in removed.items():
                        r = d["demand"].setdefault(key, [0, 0, 0])
                        r[0] += n; r[1] += gap; r[2] += 1
                d["snaps"].append(sid)
            except (OSError, ValueError) as e:
                errors.append(f"history {s['path']}: {e}")

    today = (t0 - FINALIZE_GRACE_MS) // DAY_MS
    for (house, day), d in sorted(partials.items()):
        p = out / f"history/partial/{house}/{day}.json.gz"
        if day < today:
            doc = {"format": HISTORY_FORMAT, "house_id": house, "house_name": names.get(house, ""), "day": day,
                   "snapshots": len(d["snaps"]), "fields": DAY_FIELDS,
                   "items": {k: fold_day(v) for k, v in sorted(d["items"].items()) if v},
                   "demand": {k: v for k, v in sorted(d["demand"].items())}}
            raw = json.dumps(doc, separators=(",", ":")).encode()
            rel = f"history/{house}/{day}.json.gz"
            data = gz(raw)
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_bytes(data)
            hist[(house, day)] = {"path": rel, "house_id": house, "house_name": doc["house_name"], "day": day,
                                  "snapshots": doc["snapshots"], "items": len(doc["items"]),
                                  "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(data)}
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(gz(json.dumps(d, separators=(",", ":")).encode()))

    keep_from = t0 // DAY_MS - history_days
    for k in [k for k in hist if k[1] < keep_from]:
        (out / hist.pop(k)["path"]).unlink(missing_ok=True)
    for f in (out / "history").rglob("*.json.gz") if (out / "history").exists() else []:
        if f.parent.parent.name == "partial":
            if int(f.name.split(".")[0]) < keep_from:
                f.unlink()
        elif f.relative_to(out).as_posix() not in {h["path"] for h in hist.values()}:
            f.unlink()
    return sorted(hist.values(), key=lambda h: (h["day"], h["house_id"]))


def run(api: Api, out: Path, ruleset: int, keep_hours: float, item_limit: int, now=now_ms,
        history_days: int = 30) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    old = load_manifest(out)
    t0 = now()
    cutoff = t0 - int(keep_hours * 3_600_000)
    snaps = [s for s in old.get("snapshots", []) if s["last_modified"] >= cutoff and (out / s["path"]).exists()]
    have = {(s["house_id"], s["scope"], s["last_modified"]) for s in snaps}
    errors, new, seen_items = [], 0, set()

    body, _ = api.get(f"/data/wow/ruleset/{ruleset}/auctions/", f"dynamic-forever-{api.region}")
    if body is None:
        raise RuntimeError(f"ruleset {ruleset} auction index not found")
    houses = [{"id": int(h["id"]), "name": str(h.get("name") or "")} for h in json.loads(body).get("auctions", [])]

    for h in houses:
        for scope in ("items", "commodities"):
            path = f"/data/wow/ruleset/{ruleset}/auctions/{h['id']}" + ("/commodities" if scope == "commodities" else "")
            try:
                raw, hdrs = api.get(path, f"dynamic-forever-{api.region}")
            except RuntimeError as e:
                errors.append(f"house {h['id']} {scope}: {e}")
                continue
            if raw is None:
                errors.append(f"house {h['id']} {scope}: 404")
                continue
            lm = last_modified_ms(hdrs.get("last-modified")) or t0
            seen_items |= item_ids(raw)
            if (h["id"], scope, lm) in have:
                continue                                   # Blizzard hasn't refreshed this house yet
            rel = f"snapshots/{h['id']}/{scope}-{lm}.json.gz"
            data = gz(raw)
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_bytes(data)
            snaps.append({"path": rel, "house_id": h["id"], "house_name": h["name"], "scope": scope,
                          "last_modified": lm, "fetched_at": t0, "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(data)})
            have.add((h["id"], scope, lm))
            new += 1

    history = update_history(out, snaps, houses, old.get("history", []), t0, history_days, errors)

    # Delete files no longer listed (expired).
    keep = {s["path"] for s in snaps}
    for f in (out / "snapshots").rglob("*.json.gz") if (out / "snapshots").exists() else []:
        if f.relative_to(out).as_posix() not in keep:
            f.unlink()

    items = load_items(out)
    todo = sorted(i for i in seen_items if str(i) not in items)
    # expired, or fetched before item class was recorded (disenchant values need it)
    todo += sorted((int(k) for k, v in items.items()
                    if t0 - v.get("fetched_at", 0) > ITEM_TTL_MS or (not v.get("missing") and "class" not in v)),
                   key=lambda k: items[str(k)].get("fetched_at", 0))
    fetched = 0
    for iid in todo[:item_limit]:
        try:
            items[str(iid)] = fetch_item(api, iid, t0)
            fetched += 1
        except RuntimeError as e:
            errors.append(f"item {iid}: {e}")
            if len(errors) > 50:
                break
    items_raw = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    (out / "items.json.gz").write_bytes(gz(items_raw))

    snaps.sort(key=lambda s: (s["last_modified"], s["house_id"], s["scope"]))
    manifest = {
        "format": FORMAT, "generated_at": t0, "region": api.region, "ruleset": ruleset,
        "keep_hours": keep_hours, "attribution": ATTRIBUTION, "houses": houses, "snapshots": snaps,
        "history_days": history_days, "history": history,
        "items": {"path": "items.json.gz", "count": len(items), "sha256": hashlib.sha256(items_raw).hexdigest(),
                  "pending": max(0, len(todo) - fetched)},
        "errors": errors[:50],
    }
    prev = load_manifest(out)
    if prev.get("recipes"):                      # written by recipes.py (weekly); keep it listed
        manifest["recipes"] = prev["recipes"]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"houses": len(houses), "new_snapshots": new, "kept": len(snaps), "items_fetched": fetched,
            "items_pending": manifest["items"]["pending"], "errors": len(errors), "history_days": len({h["day"] for h in history}),
            "total_mb": round(sum(s["bytes"] for s in snaps) / 1e6, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data")
    ap.add_argument("--region", default=os.environ.get("REGION", "us"))
    ap.add_argument("--ruleset", type=int, default=int(os.environ.get("RULESET", "1")))
    ap.add_argument("--keep-hours", type=float, default=float(os.environ.get("KEEP_HOURS", "24")))
    ap.add_argument("--history-days", type=int, default=int(os.environ.get("HISTORY_DAYS", "30")))
    ap.add_argument("--item-limit", type=int, default=int(os.environ.get("ITEM_LIMIT", "400")))
    a = ap.parse_args()
    cid, sec = os.environ.get("BLIZZARD_CLIENT_ID"), os.environ.get("BLIZZARD_CLIENT_SECRET")
    missing = [n for n, v in (("BLIZZARD_CLIENT_ID", cid), ("BLIZZARD_CLIENT_SECRET", sec)) if not v]
    if missing:
        print(f"::error::Missing {', '.join(missing)}. Add it under Settings -> Secrets and variables -> "
              "Actions -> 'Repository secrets' (not 'Environment secrets'), with exactly that name in capitals.",
              file=sys.stderr)
        return 2
    summary = run(Api(a.region, cid, sec), Path(a.out), a.ruleset, a.keep_hours, a.item_limit,
                  history_days=a.history_days)
    print(json.dumps(summary))
    return 1 if summary["houses"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
