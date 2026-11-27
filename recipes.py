"""Build recipes.json.gz: every WoW: Forever profession recipe with its skill-up thresholds.

Source: the Forever client's own tables (Blizzard Entertainment's game data), as CSV exports from
wago.tools for one pinned client build. Standard library only. For each recipe spell:

  name, profession, reagents, the item it makes (and how many), and the skill-up colours
  orange / yellow / green / grey, plus how it's learned (trainer / automatically / a recipe item,
  with the skill that item asks for).

Thresholds: yellow and grey are SkillLineAbility.TrivialSkillLineRankLow/High; green is their
midpoint (the client's rule). Orange (the skill you can learn/start it at) is MinSkillLineRank when
that's a real value, else the recipe item's required skill; when neither is known it's left out
(null) rather than guessed, and Gold Intel treats the recipe as usable from yellow.

Regenerated at most once a week (or when the build changes); the hourly snapshot job calls it and
carries on if it fails.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

BUILD = os.environ.get("FOREVER_BUILD", "1.60.1.71139")
URL = "https://wago.tools/db2/{name}/csv?build={build}"
UA = "GoldIntelData/1.0 (+https://github.com/jjansendev/gold-intel-database)"
MAX_AGE_MS = 7 * 86_400_000

PROFESSIONS = {129: "First Aid", 164: "Blacksmithing", 165: "Leatherworking", 171: "Alchemy", 185: "Cooking",
               186: "Mining", 197: "Tailoring", 202: "Engineering", 333: "Enchanting", 356: "Fishing", 40: "Poisons"}
CREATE_ITEM, LEARN_SPELL, TRANS_DOOR, ENCHANT_ITEM = 24, 36, 50, 53
RECIPE_EFFECTS = {CREATE_ITEM, ENCHANT_ITEM, TRANS_DOOR}
ITEM_DEPRECATED = 0x10


def fetch(name: str, build: str, cache: Path | None) -> list[dict]:
    path = cache / f"{name}-{build}.csv" if cache else None
    if path and path.exists():
        text = path.read_text(encoding="utf-8-sig")
    else:
        req = urllib.request.Request(URL.format(name=name, build=build), headers={"User-Agent": UA})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(2 ** (attempt + 1))
        if not data.strip() or data.lstrip().startswith(b"<"):
            raise ValueError(f"{name}: expected CSV, got HTML or nothing")
        text = data.decode("utf-8-sig")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"{name}: no rows")
    return rows


def _i(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def build_recipes(t: dict[str, list[dict]]) -> dict:
    """t: table name -> rows. Returns the recipes.json payload (without timestamps)."""
    skill_names = {_i(r["ID"]): r.get("DisplayName_lang") or "" for r in t["SkillLine"]}
    names = {_i(r["ID"]): r.get("Name_lang") or "" for r in t["SpellName"]}
    effects: dict[int, list[dict]] = defaultdict(list)
    for r in t["SpellEffect"]:
        if _i(r.get("DifficultyID")) == 0:
            effects[_i(r["SpellID"])].append(r)

    # abilities: spell -> (skill line, min, yellow, grey, acquire); a spell with conflicting rows is skipped
    ab: dict[int, set] = defaultdict(set)
    for r in t["SkillLineAbility"]:
        sl = _i(r["SkillLine"])
        if sl not in PROFESSIONS:
            continue
        ab[_i(r["Spell"])].add((sl, _i(r.get("MinSkillLineRank")), _i(r.get("TrivialSkillLineRankLow")),
                                _i(r.get("TrivialSkillLineRankHigh")), _i(r.get("AcquireMethod"))))

    # recipe items: item -> required skill; the item's use-spell teaches the recipe (directly, or via LEARN_SPELL)
    sparse = {_i(r["ID"]): r for r in t["ItemSparse"]}
    eff_spell = {_i(r["ID"]): _i(r["SpellID"]) for r in t["ItemEffect"]}
    teaches: dict[int, tuple[int, int]] = {}
    for r in t["ItemXItemEffect"]:
        item, spell = _i(r["ItemID"]), eff_spell.get(_i(r["ItemEffectID"]))
        s = sparse.get(item)
        if not spell or not s or _i(s.get("Flags_0")) & ITEM_DEPRECATED or _i(s.get("RequiredSkill")) == 0:
            continue
        targets = {spell} | {_i(e.get("EffectTriggerSpell")) for e in effects.get(spell, [])
                             if _i(e["Effect"]) == LEARN_SPELL}
        for sp in targets:
            if sp in ab:
                rank = _i(s.get("RequiredSkillRank"))
                if sp not in teaches or rank < teaches[sp][1]:
                    teaches[sp] = (item, rank)

    cooldown: dict[int, int] = {}
    for r in t.get("SpellCooldowns") or []:
        if _i(r.get("DifficultyID")) == 0:
            cd = max(_i(r.get("RecoveryTime")), _i(r.get("CategoryRecoveryTime")))
            if cd > 0:
                cooldown[_i(r["SpellID"])] = cd
    recipes, skipped, items = [], defaultdict(int), set()
    reagents_of: dict[int, list] = {}
    for r in t["SpellReagents"]:
        sp = _i(r["SpellID"])
        out, bad = defaultdict(int), False
        for k in range(8):
            it, q = _i(r.get(f"Reagent_{k}")), _i(r.get(f"ReagentCount_{k}"))
            if it < 0:
                bad = True
            elif it > 0 and q > 0:
                out[it] += q
        reagents_of[sp] = None if bad else sorted(out.items())
    for sp, rows in sorted(ab.items()):
        if len(rows) != 1:
            skipped["conflicting ability rows"] += 1
            continue
        sl, mn, yellow, grey, acquire = next(iter(rows))
        effs = effects.get(sp, [])
        kinds = {_i(e["Effect"]) for e in effs}
        if not kinds & RECIPE_EFFECTS:
            skipped["not a recipe"] += 1
            continue
        if not (0 < yellow <= grey) or grey > 450:
            skipped["no skill-up range"] += 1
            continue
        reagents = reagents_of.get(sp)
        if reagents is None:
            skipped["unresolved reagents"] += 1
            continue
        makes = None
        creates = [e for e in effs if _i(e["Effect"]) == CREATE_ITEM and _i(e.get("EffectItemType")) > 0]
        if creates:
            e = creates[0]
            qty = max(1.0, float(e.get("EffectBasePointsF") or 1) or 1)
            makes = [_i(e["EffectItemType"]), round(qty, 2)]
        teach = teaches.get(sp)
        if 1 < mn <= yellow or (mn == 1 and yellow <= 40):
            orange, orange_src = mn, "client"
        elif teach and 0 < teach[1] <= yellow:
            orange, orange_src = teach[1], "recipe item"
        else:
            orange, orange_src = None, None
        green = (yellow + grey) // 2
        learn = "item" if teach else ("auto" if acquire == 1 else "trainer")
        rec = {"id": sp, "name": names.get(sp) or f"Spell {sp}", "skill": sl, "profession": PROFESSIONS[sl],
               "reagents": [[a, b] for a, b in reagents], "makes": makes,
               "orange": orange, "orange_source": orange_src, "yellow": yellow, "green": green, "grey": grey,
               "learn": learn, "teach_item": teach[0] if teach else None, "teach_skill": teach[1] if teach else None,
               "cooldown_ms": cooldown.get(sp) or None}
        recipes.append(rec)
        items.update(a for a, _ in reagents)
        if makes:
            items.add(makes[0])
        if teach:
            items.add(teach[0])
    # every item in the client (not only recipe ones): names and vendor prices straight from Forever's
    # game data, so the app never uses Retail's vendor prices or shows "Item 14047"
    item_cls = {_i(r["ID"]): (_i(r.get("ClassID"), None), _i(r.get("SubclassID"), None)) for r in t.get("Item") or []}
    item_info = {}
    for i, s in sorted(sparse.items()):
        name = s.get("Display_lang") or None
        if not name and i not in items:
            continue
        e = {"name": name, "buy": _i(s.get("BuyPrice")) or None, "sell": _i(s.get("SellPrice")) or None,
             "q": _i(s.get("OverallQualityID"), None), "ilvl": _i(s.get("ItemLevel"), None) or None,
             "req": _i(s.get("RequiredLevel"), None) or None,
             "cls": (item_cls.get(i) or (None, None))[0], "sub": (item_cls.get(i) or (None, None))[1]}
        item_info[str(i)] = {k: v for k, v in e.items() if v is not None}
    by_prof = defaultdict(int)
    for r in recipes:
        by_prof[r["profession"]] += 1
    return {"format": 1, "items_scope": "all", "build": BUILD, "professions": {str(k): v for k, v in PROFESSIONS.items()},
            "recipes": recipes, "items": item_info, "counts": dict(by_prof), "skipped": dict(skipped),
            "source": f"WoW: Forever client tables (build {BUILD}) via wago.tools; Blizzard Entertainment's game data"}


TABLES = {"Item": None, "SpellCooldowns": None, "SkillLine": None, "SpellName": None, "SpellEffect": None, "SkillLineAbility": None, "SpellReagents": None,
          "ItemSparse": None, "ItemEffect": None, "ItemXItemEffect": None}


def run(out: Path, cache: Path | None = None, force: bool = False, now: int | None = None) -> dict | None:
    """Write out/recipes.json.gz and its manifest entry when due. Returns the manifest entry."""
    now = now or int(time.time() * 1000)
    man_path = out / "manifest.json"
    man = json.loads(man_path.read_text()) if man_path.exists() else {}
    cur = man.get("recipes") or {}
    if not force and cur.get("item_class") and cur.get("cooldowns") and cur.get("items_scope") == "all" and cur.get("build") == BUILD and now - (cur.get("generated_at") or 0) < MAX_AGE_MS \
            and (out / "recipes.json.gz").exists():
        print(f"recipes: up to date ({cur.get('count')} recipes, build {BUILD})")
        return cur
    tables = {name: fetch(name, BUILD, cache) for name in TABLES}
    payload = build_recipes(tables)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    (out / "recipes.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    entry = {"path": "recipes.json.gz", "build": BUILD, "items_scope": "all", "cooldowns": True, "item_class": True, "items": len(payload["items"]), "generated_at": now, "count": len(payload["recipes"]),
             "sha256": hashlib.sha256(raw).hexdigest()}
    man = json.loads(man_path.read_text()) if man_path.exists() else {}
    man["recipes"] = entry
    man_path.write_text(json.dumps(man, indent=1))
    print(f"recipes: wrote {entry['count']} recipes, build {BUILD}; by profession {payload['counts']}; "
          f"skipped {payload['skipped']}")
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="data")
    ap.add_argument("--cache")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    run(Path(a.out), Path(a.cache) if a.cache else None, a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
