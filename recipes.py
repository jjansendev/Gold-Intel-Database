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
    try:                                   # never let the gear export cost the recipe database
        gear, gear_fields = build_gear(t, sparse, item_cls)
        print("recipes: gear fields:", gear_fields, "| probes:", {i: gear.get(i) for i in (8193, 4249, 2139, 897)})
    except Exception as e:  # noqa: BLE001
        print(f"gear export failed, skipped: {type(e).__name__}: {e}")
        gear, gear_fields = {}, {"error": f"{type(e).__name__}: {e}"}
    for i, g in gear.items():
        if str(i) in item_info:
            item_info[str(i)].update(g)
    by_prof = defaultdict(int)
    for r in recipes:
        by_prof[r["profession"]] += 1
    return {"format": 1, "items_scope": "all", "build": BUILD, "professions": {str(k): v for k, v in PROFESSIONS.items()},
            "recipes": recipes, "items": item_info, "counts": dict(by_prof), "skipped": dict(skipped),
            "gear_fields": gear_fields,
            "source": f"WoW: Forever client tables (build {BUILD}) via wago.tools; Blizzard Entertainment's game data"}


# ── gear stats (for Gold Intel's "Upgrades for you") ─────────────────────────────────────────────
# Per weapon/armour item: inventory type, allowed classes, required level, armour, stats, weapon damage
# and speed, and whether its stats come from a random suffix. Written against the DB2 column names of
# the Classic-era client tables (ItemSparse StatModifier_bonusStat_N / StatModifier_bonusAmount_N,
# Resistances_0 = armour, MinDamage_0 / MaxDamage_0, ItemDelay, RandomSelect / ItemRandomSuffixGroupID);
# a column the client doesn't have is left out (unknown), never filled with a guess. gear_fields in the
# payload says which were found.
GEAR_CLASSES = (2, 4)                       # weapon, armour
ITEM_MOD = {3: "agi", 4: "str", 5: "int", 6: "spi", 7: "sta", 0: "mana", 1: "health",
            12: "def_rating", 13: "dodge_rating", 14: "parry_rating", 15: "block_rating",
            16: "hit_rating", 17: "hit_rating", 18: "spellhit_rating", 19: "crit_rating", 20: "crit_rating",
            21: "spellcrit_rating", 31: "hit_rating", 32: "crit_rating", 35: "resilience", 36: "haste_rating",
            38: "ap", 39: "rap", 41: "heal", 42: "sp", 43: "mp5", 45: "sp", 46: "hp5", 48: "blockval"}
APPLY_AURA, TRIGGER_ON_EQUIP = 6, 1
SCHOOLS = {2: "holy", 4: "fire", 8: "nature", 16: "frost", 32: "shadow", 64: "arcane"}


def _aura_stat(aura: int, misc: int) -> str | None:
    """Equip-spell aura -> stat key (Classic aura ids). Unknown auras -> None (not counted)."""
    if aura == 13:                           # MOD_DAMAGE_DONE: school mask
        if misc & 126 == 126:
            return "sp"
        return f"sp_{SCHOOLS[misc]}" if misc in SCHOOLS else None
    if aura == 30:                           # MOD_SKILL
        return "def" if misc == 95 else None
    if aura == 85:                           # MOD_POWER_REGEN
        return "mp5" if misc == 0 else None
    return {99: "ap", 124: "rap", 135: "heal", 52: "crit", 54: "hit", 57: "spellcrit", 55: "spellhit",
            49: "dodge", 47: "parry", 51: "block", 158: "blockval", 115: "heal"}.get(aura)


GEAR_VERSION = 3              # bump to rebuild the gear fields on the next run
DAMAGE_TABLES = ("ItemDamageOneHand", "ItemDamageOneHandCaster", "ItemDamageTwoHand", "ItemDamageTwoHandCaster",
                 "ItemDamageRanged", "ItemDamageThrown", "ItemDamageWand")
CASTER_WEAPON_FLAG = 0x200                   # ItemSparse Flags_1


def _damage_table(inv: int, sub: int, caster: bool) -> str:
    if sub == 19:
        return "ItemDamageWand"
    if inv == 25:
        return "ItemDamageThrown"
    if inv in (15, 26):
        return "ItemDamageRanged"
    if inv == 17:
        return "ItemDamageTwoHandCaster" if caster else "ItemDamageTwoHand"
    return "ItemDamageOneHandCaster" if caster else "ItemDamageOneHand"


_BUDGET_SLOT = {**{v: 0 for v in (1, 5, 7, 17, 20)}, **{v: 1 for v in (3, 6, 8, 10, 12)},
                **{v: 2 for v in (2, 9, 11, 14, 16, 23)}, **{v: 3 for v in (13, 21, 22)}, **{v: 4 for v in (15, 25, 26)}}
_BUDGET_QUALITY = {2: "Good", 3: "Superior", 4: "Epic", 5: "Epic"}


def _budget(rpp: dict, ilvl: int, quality: int, inv: int) -> float | None:
    row, q, slot = rpp.get(ilvl), _BUDGET_QUALITY.get(quality), _BUDGET_SLOT.get(inv)
    if row is None or q is None or slot is None:
        return None
    v = row.get(f"{q}F_{slot}")
    if v in (None, ""):
        v = row.get(f"{q}_{slot}")
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def build_gear(t: dict[str, list[dict]], sparse: dict[int, dict], item_cls: dict) -> tuple[dict, dict]:
    """item id -> gear fields (short keys), plus which client columns were present."""
    cols = set(next(iter(sparse.values())).keys()) if sparse else set()
    # The run's log names the client's real ItemSparse columns, so the stat mapping can be checked
    # against Forever's build rather than assumed.
    print("recipes: ItemSparse columns:", ",".join(sorted(cols)))
    for probe in (8193, 4249, 2139, 897):
        if probe in sparse:
            print(f"recipes: ItemSparse {probe}:", {k: v for k, v in sparse[probe].items() if v not in ("", "0", 0, None)})
    rpp = {_i(r.get("ID")): r for r in t.get("RandPropPoints") or []}
    if t.get("RandPropPoints"):
        print("recipes: RandPropPoints columns:", ",".join(sorted(t["RandPropPoints"][0].keys())))
        print("recipes: RandPropPoints 25/46:", {k: v for k, v in (rpp.get(25) or {}).items()}, {k: v for k, v in (rpp.get(46) or {}).items()})
    for n in BUDGET_TABLES[1:]:
        if t.get(n):
            print(f"recipes: {n} columns:", ",".join(sorted(t[n][0].keys())), "| first row:", t[n][0])
    has = {"stats": "StatModifier_bonusStat_0" in cols and "StatModifier_bonusAmount_0" in cols,
           "stats_budget": "StatModifier_bonusStat_0" in cols and "StatPercentEditor_0" in cols and bool(rpp),
           "armor": "Resistances_0" in cols, "damage_columns": "MinDamage_0" in cols and "MaxDamage_0" in cols,
           "speed": "ItemDelay" in cols, "random": bool({"RandomSelect", "ItemRandomSuffixGroupID"} & cols),
           "damage_tables": [n for n in DAMAGE_TABLES if t.get(n)], "equip_effects": bool(t.get("ItemEffect"))}
    dmg_tab = {n: {_i(r.get("ItemLevel")): r for r in t.get(n) or []} for n in DAMAGE_TABLES}
    # equip spells -> stat auras
    fx_of_spell: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for r in t.get("SpellEffect") or []:
        if _i(r.get("DifficultyID")) or _i(r.get("Effect")) != APPLY_AURA:
            continue
        key = _aura_stat(_i(r.get("EffectAura")), _i(r.get("EffectMiscValue_0")))
        if key:
            v = _i(r.get("EffectBasePoints")) + (1 if _i(r.get("EffectDieSides")) > 0 else 0)
            if v:
                fx_of_spell[_i(r["SpellID"])].append((key, v))
    equip_spells: dict[int, list[int]] = defaultdict(list)
    effects = {_i(r["ID"]): r for r in t.get("ItemEffect") or []}
    links = [(_i(r["ItemID"]), _i(r["ItemEffectID"])) for r in t.get("ItemXItemEffect") or []]
    links += [(_i(r.get("ParentItemID")), _i(r["ID"])) for r in effects.values() if _i(r.get("ParentItemID"))]
    for item, eid in links:
        e = effects.get(eid)
        if e and _i(e.get("TriggerType")) == TRIGGER_ON_EQUIP and _i(e.get("SpellID")):
            if _i(e["SpellID"]) not in equip_spells[item]:
                equip_spells[item].append(_i(e["SpellID"]))
    out = {}
    for i, s in sparse.items():
        cls, sub = item_cls.get(i) or (None, None)
        inv = _i(s.get("InventoryType"))
        if cls not in GEAR_CLASSES or inv <= 0:
            continue
        g: dict = {"inv": inv}
        ac = _i(s.get("AllowableClass"), None)
        if ac is not None and ac not in (0, -1):
            g["acls"] = ac
        if has["stats"]:
            st: dict[str, int] = {}
            for k in range(10):
                typ, amt = _i(s.get(f"StatModifier_bonusStat_{k}"), -1), _i(s.get(f"StatModifier_bonusAmount_{k}"))
                if typ >= 0 and amt and typ in ITEM_MOD:
                    st[ITEM_MOD[typ]] = st.get(ITEM_MOD[typ], 0) + amt
                elif typ >= 0 and amt:
                    st[f"mod{typ}"] = st.get(f"mod{typ}", 0) + amt       # an unknown stat: kept, not scored
            g["st"] = st
        elif has["stats_budget"]:
            budget = _budget(rpp, _i(s.get("ItemLevel")), _i(s.get("OverallQualityID")), inv)
            st = {}
            for k in range(10):
                typ, pct = _i(s.get(f"StatModifier_bonusStat_{k}"), -1), _i(s.get(f"StatPercentEditor_{k}"))
                if typ < 0 or not pct:
                    continue
                if budget is None:
                    st = None                     # a stat we can't size: unknown, not 0
                    break
                amt = int(budget * pct / 10000 + 0.5)
                key = ITEM_MOD.get(typ, f"mod{typ}")
                st[key] = st.get(key, 0) + amt
            if st is not None:
                g["st"] = st
                g["st_src"] = "budget"
        fx: dict[str, int] = {}
        for sp in equip_spells.get(i, []):
            for key, v in fx_of_spell.get(sp, []):
                fx[key] = fx.get(key, 0) + v
        if fx:
            g["fx"] = fx
        if has["armor"]:
            g["armor"] = _i(s.get("Resistances_0"))
        if cls == 2:
            delay = _i(s.get("ItemDelay")) if has["speed"] else 0
            if delay > 0:
                g["spd"] = delay
            lo, hi = (_i(s.get("MinDamage_0")), _i(s.get("MaxDamage_0"))) if has["damage_columns"] else (0, 0)
            if hi > 0:
                g["dmg"] = [lo, hi]
            elif delay > 0:
                caster = bool(_i(s.get("Flags_1")) & CASTER_WEAPON_FLAG)
                row = dmg_tab.get(_damage_table(inv, sub or 0, caster), {}).get(_i(s.get("ItemLevel")))
                q = _i(s.get("OverallQualityID"), -1)
                dps = float(row.get(f"Quality_{q}") or 0) if row and q >= 0 else 0.0
                if dps > 0:
                    avg = dps * delay / 1000
                    var = float(s.get("DmgVariance") or 0)
                    g["dmg"] = [round(avg * (1 - var / 2), 1), round(avg * (1 + var / 2), 1)]
                    g["dmg_src"] = "table"
        if has["random"]:
            g["rnd"] = 1 if (_i(s.get("RandomSelect")) > 0 or _i(s.get("ItemRandomSuffixGroupID")) > 0) else 0
        out[i] = g
    return out, has


TABLES = {"Item": None, "SpellCooldowns": None, "SkillLine": None, "SpellName": None, "SpellEffect": None, "SkillLineAbility": None, "SpellReagents": None,
          "ItemSparse": None, "ItemEffect": None, "ItemXItemEffect": None}
# Forever's ItemSparse holds each stat's type and share of a budget (StatPercentEditor, 1/10000), not
# the amount: amount = RandPropPoints[item level][quality][slot group] x share. Armour likewise comes
# from per-level tables (ItemArmorTotal x ArmorLocation x ItemArmorQuality; shields ItemArmorShield).
BUDGET_TABLES = ("RandPropPoints", "ItemArmorTotal", "ArmorLocation", "ItemArmorQuality", "ItemArmorShield")
OPTIONAL_TABLES = DAMAGE_TABLES + BUDGET_TABLES


def run(out: Path, cache: Path | None = None, force: bool = False, now: int | None = None) -> dict | None:
    """Write out/recipes.json.gz and its manifest entry when due. Returns the manifest entry."""
    now = now or int(time.time() * 1000)
    man_path = out / "manifest.json"
    man = json.loads(man_path.read_text()) if man_path.exists() else {}
    cur = man.get("recipes") or {}
    if not force and cur.get("gear") == GEAR_VERSION and cur.get("item_class") and cur.get("cooldowns") and cur.get("items_scope") == "all" and cur.get("build") == BUILD and now - (cur.get("generated_at") or 0) < MAX_AGE_MS \
            and (out / "recipes.json.gz").exists():
        print(f"recipes: up to date ({cur.get('count')} recipes, build {BUILD})")
        return cur
    tables = {name: fetch(name, BUILD, cache) for name in TABLES}
    for name in OPTIONAL_TABLES:            # weapon damage by item level (only some clients need them)
        try:
            tables[name] = fetch(name, BUILD, cache)
        except Exception as e:  # noqa: BLE001
            print(f"recipes: optional table {name} not available ({e}); weapon damage from it left out")
    payload = build_recipes(tables)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    (out / "recipes.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    entry = {"path": "recipes.json.gz", "build": BUILD, "items_scope": "all", "cooldowns": True, "item_class": True, "gear": GEAR_VERSION, "items": len(payload["items"]), "generated_at": now, "count": len(payload["recipes"]),
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
