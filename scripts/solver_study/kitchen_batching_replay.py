"""
kitchen_batching_replay.py - READ-ONLY replay of real meal plans through the real
production planner code (services/daily_menu_service.py +
services/mealplan_service.optimize_subrecipes), comparing the current code
against "option A" batching fixes. Never writes to Supabase: the client is
wrapped so any insert/upsert/update/delete raises, and daily_menu is served
from an in-memory fake.

Variants:
  prod      - the code as it is now, untouched (pass batch_counts like the route)
  daily4    - prod + weekly fixed (3 L/D, 1 B, 1 S) and per day 4 L/D, 2 B, 2 S
  hybrid    - "classics always on, specials 2 weekdays" menu
              (CORE_SHARE env = share of each menu kept always-on, default 0.5)
  base, a_template, a_full, a_strong - monkeypatched emulations used for the
              2026-11-19 study, run against the PRE-change code (current code
              already has option A built in, so they no longer mean the same).

Results 2026-11-19 (49 plans, Nov 9-20 cook dates, 689 meals):
  old code 133 batches / 5.2 avg portions; option A 98 / 7.0;
  hybrid (1/3 core) 88 / 7.8, batches of <=2 portions 71 -> 54 -> 26.

Usage (from mealplanner-flask):
  venv/Scripts/python.exe scripts/solver_study/kitchen_batching_replay.py --variants prod --out data/kb_prod.json
"""
import argparse, json, os, random, sys, time
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

# ---- creds from akli-web/.env.local (never printed) ----
ENV = os.path.join(REPO, "..", "..", "FrontEnd", "akli-web", ".env.local")
for line in open(ENV, encoding="utf-8"):
    k, _, v = line.strip().partition("=")
    if k == "NEXT_PUBLIC_SUPABASE_URL":
        os.environ["SUPABASE_URL"] = v.strip('"')
    if k == "SUPABASE_SERVICE_ROLE_KEY":
        os.environ["SUPABASE_KEY"] = v.strip('"')

from flask import Flask
import utils.supabase_client as sc
import services.daily_menu_service as dms
import services.mealplan_service as mps
from utils.supabase_paging import fetch_all

REAL = sc.supabase


# ---- read-only
class _RO:
    def __init__(self, inner): self._i = inner
    def __getattr__(self, name):
        if name in ("insert", "upsert", "update", "delete", "rpc"):
            raise RuntimeError(f"WRITE BLOCKED: {name}")
        a = getattr(self._i, name)
        if callable(a):
            def w(*args, **kw):
                r = a(*args, **kw)
                return _RO(r) if hasattr(r, "execute") or hasattr(r, "select") else r
            return w
        return a


SIM = {"now": datetime(2026, 11, 1), "store": {}, "ttl_off": False, "batch": {}, "bonus": 0.0, "full": 3}


class _Resp:
    def __init__(self, data): self.data = data


class FakeDailyMenu:
    """Just enough of the PostgREST chain build_week_templates uses."""
    def __init__(self): self._dates = None; self._mts = None; self._up = None
    def select(self, *_a, **_k): return self
    def in_(self, col, vals):
        if col == "date": self._dates = set(vals)
        if col == "meal_type": self._mts = set(vals)
        return self
    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self._up = (rows, ignore_duplicates); return self
    def execute(self):
        st = SIM["store"]
        if self._up is not None:
            rows, ign = self._up
            for r in rows:
                key = (r["date"], r["meal_type"])
                if key in st and ign:
                    continue
                st[key] = {"recipe_id": r["recipe_id"], "created_at": SIM["now"].strftime("%Y-%m-%dT%H:%M:%S")}
            return _Resp(rows)
        out = []
        for (d, mt), v in st.items():
            if (self._dates is None or d in self._dates) and (self._mts is None or mt in self._mts):
                out.append({"date": d, "meal_type": mt, **v})
        return _Resp(out)


class _Proxy:
    def table(self, name):
        if name == "daily_menu":
            return FakeDailyMenu()
        return _RO(REAL.table(name))


ro = _Proxy()
dms.supabase = ro
mps.supabase = _RO(REAL)


class _SimDT(datetime):
    @classmethod
    def utcnow(cls):
        if SIM["ttl_off"]:
            return datetime(2000, 1, 1)  # nothing ever looks stale
        return SIM["now"]
dms.datetime = _SimDT

# ---- batch-aware swap: wrap candidate scoring ----
_orig_cands = dms._client_candidates
def _batch_cands(*args, **kw):
    # Emulation hook for the a_*/hybrid variants; a pass-through for prod.
    pairs = _orig_cands(*args, **kw)
    if SIM["bonus"] <= 0:
        return pairs
    cnt = SIM["batch"]
    return [(rid, s + SIM["bonus"] * min(1.0, cnt.get(rid, 0) / SIM["full"])) for rid, s in pairs]
dms._client_candidates = _batch_cands

_orig_merge = dms._apply_kitchen_batching


def P(s): return datetime.strptime(s[:10], "%Y-%m-%d").date()


# ---- load data
def load(win_lo, win_hi):
    days = fetch_all(lambda lo, hi: REAL.table("meal_plan_day")
        .select("id, date, status, meal_plan_id, delivery_id, meal_plan(id, user_id, created_at)")
        .gte("date", str(win_lo - timedelta(days=21))).lte("date", str(win_hi + timedelta(days=21)))
        .range(lo, hi))
    days = [d for d in days if (d.get("status") or "") != "cancelled"]
    plan_ids_in_win = {d["meal_plan_id"] for d in days if win_lo <= P(d["date"]) <= win_hi}
    days = [d for d in days if d["meal_plan_id"] in plan_ids_in_win]
    day_ids = [d["id"] for d in days]
    recs = []
    for i in range(0, len(day_ids), 150):
        recs += fetch_all(lambda lo, hi, ch=day_ids[i:i+150]: REAL.table("meal_plan_day_recipe")
            .select("meal_plan_day_id, meal_type, recipe_id").in_("meal_plan_day_id", ch).range(lo, hi))
    deliv_ids = [d["delivery_id"] for d in days if d.get("delivery_id")]
    deliv = {}
    for i in range(0, len(deliv_ids), 150):
        for r in REAL.table("deliveries").select("id, delivery_date").in_("id", deliv_ids[i:i+150]).execute().data:
            deliv[r["id"]] = r["delivery_date"]
    mts_by_day = defaultdict(set); actual = defaultdict(list)
    for r in recs:
        mts_by_day[r["meal_plan_day_id"]].add(r["meal_type"]); actual[r["meal_plan_day_id"]].append(r)
    plans = {}
    for d in days:
        mp = d["meal_plan"]
        p = plans.setdefault(mp["id"], {"plan_id": mp["id"], "user_id": mp["user_id"], "created_at": mp["created_at"], "days": []})
        p["days"].append({"date": d["date"][:10], "mts": sorted(mts_by_day[d["id"]]),
                          "cook": (deliv.get(d.get("delivery_id")) or d["date"])[:10],
                          "actual": [(r["meal_type"], r["recipe_id"]) for r in actual[d["id"]]]})
    for p in plans.values():
        p["days"].sort(key=lambda x: x["date"])
        c = Counter(tuple(x["mts"]) for x in p["days"] if x["mts"])
        p["mts"] = list(c.most_common(1)[0][0]) if c else []
    plans = [p for p in plans.values() if p["mts"]]
    plans.sort(key=lambda p: p["created_at"])
    users = list({p["user_id"] for p in plans})
    prefs = defaultdict(dict); targets = defaultdict(list)
    for i in range(0, len(users), 100):
        for r in fetch_all(lambda lo, hi, ch=users[i:i+100]: REAL.table("user_recipe_preferences")
                .select("user_id, recipe_id, like, dislike, dont_include").in_("user_id", ch).range(lo, hi)):
            prefs[r["user_id"]][r["recipe_id"]] = r
        for r in fetch_all(lambda lo, hi, ch=users[i:i+100]: REAL.table("daily_macro_target")
                .select("user_id, protein_g, carbs_g, fat_g, kcal_target, created_at").in_("user_id", ch).range(lo, hi)):
            targets[r["user_id"]].append(r)
    lo_all = min(P(x["date"]) for p in plans for x in p["days"])
    hi_all = max(P(x["date"]) for p in plans for x in p["days"])
    wms = REAL.table("weekly_menu").select("id, week_start_date, week_end_date, weekly_menu_recipe(recipe(*))") \
        .lte("week_start_date", str(hi_all)).gte("week_end_date", str(lo_all)).execute().data
    return plans, prefs, targets, wms


def target_for(rows, created_at):
    rows = sorted(rows, key=lambda r: r["created_at"])
    pick = [r for r in rows if r["created_at"] <= created_at] or rows
    t = pick[-1]
    pg, cg, fg = float(t["protein_g"] or 0), float(t["carbs_g"] or 0), float(t["fat_g"] or 0)
    k = float(t.get("kcal_target") or (4 * (pg + cg) + 9 * fg))
    return {"protein_g": pg, "carbs_g": cg, "fat_g": fg, "kcal": k}


# ---- run
def run_variant(name, plans, prefs, targets, wms, seed, shared):
    SIM["store"] = {}
    SIM["ttl_off"] = name not in ("base", "prod", "daily4")
    SIM["bonus"] = {"a_full": 1.5, "a_strong": 3.0}.get(name, 0.0)
    dms._apply_kitchen_batching = (_orig_merge if name in ("base", "prod", "daily4") else
                                   (lambda schedule, *a, **k: {d: dict(c) for d, c in schedule.items()}))
    allowed_by_date = defaultdict(set); recipes_by_id = {}; menu_id_by_date = {}
    for wm in sorted(wms, key=lambda w: w["week_start_date"]):
        d = P(wm["week_start_date"])
        while d <= P(wm["week_end_date"]):
            menu_id_by_date.setdefault(d, wm["id"]); d += timedelta(days=1)
    for wm in wms:
        for wmr in wm.get("weekly_menu_recipe") or []:
            r = (wmr or {}).get("recipe")
            if not r: continue
            recipes_by_id[r["id"]] = r
            d = P(wm["week_start_date"])
            while d <= P(wm["week_end_date"]):
                allowed_by_date[d].add(r["id"]); d += timedelta(days=1)
    if name.startswith("hybrid"):
        # core = best sellers (top half by real portions, per menu period) always on;
        # the rest only on 2 fixed weekdays each (Mon+Thu, Tue+Fri, Wed+Sun, round-robin)
        # Runs on the real current code path (like prod).
        SIM["ttl_off"] = False; SIM["bonus"] = 0.0
        dms._apply_kitchen_batching = _orig_merge
        sales = shared["sales"]
        pairs = [(0, 3), (1, 4), (2, 6)]
        for wm in wms:
            ids = sorted({(wmr.get("recipe") or {}).get("id") for wmr in wm.get("weekly_menu_recipe") or []} - {None},
                         key=lambda r: -sales.get(r, 0))
            core_n = int(len(ids) * shared["core_share"])
            specials = ids[core_n:]
            d = P(wm["week_start_date"])
            while d <= P(wm["week_end_date"]):
                for j, rid in enumerate(specials):
                    if d.weekday() not in pairs[j % 3]:
                        allowed_by_date[d].discard(rid)
                d += timedelta(days=1)
    if name == "daily4":
        # Weekly fixed (3 lunch/dinner + 1 breakfast + 1 snack, all week, a
        # different set each ISO week) + per weekday 4 lunch/dinner, 2
        # breakfast, 2 snack rotating through the rest of that menu.
        # Runs on the real current code (prod path, batch_counts passed).
        sales = shared["sales"]
        quota = {"LD": (3, 4), "B": (1, 2), "S": (1, 2)}  # (weekly fixed, daily rotating)

        def cat(r):
            if r.get("could_be_lunch") or (r.get("could_be_dinner") and not r.get("could_be_breakfast")):
                return "LD"
            return "B" if r.get("could_be_breakfast") else "S"
        for wm in wms:
            ids = sorted({(wmr.get("recipe") or {}).get("id") for wmr in wm.get("weekly_menu_recipe") or []} - {None},
                         key=lambda r: -sales.get(r, 0))
            by_cat = {c: [r for r in ids if cat(recipes_by_id[r]) == c] for c in quota}
            ws, we = P(wm["week_start_date"]), P(wm["week_end_date"])
            weeks = sorted({(d.isocalendar()[0], d.isocalendar()[1]) for d in
                            (ws + timedelta(days=i) for i in range((we - ws).days + 1))})
            d = ws
            while d <= we:
                wk_idx = weeks.index((d.isocalendar()[0], d.isocalendar()[1]))
                keep = set()
                for c, (nf, nd) in quota.items():
                    pool = by_cat[c]
                    if not pool: continue
                    off = (wk_idx * nf) % len(pool)
                    rot = pool[off:] + pool[:off]
                    fixed, rest = rot[:nf], rot[nf:]
                    keep |= set(fixed)
                    if rest:
                        i = d.weekday()
                        keep |= {rest[(i * nd + k) % len(rest)] for k in range(nd)}
                allowed_by_date[d] &= keep
                d += timedelta(days=1)
    all_recipes = list(recipes_by_id.values()); all_ids = [r["id"] for r in all_recipes]
    flex, rmac, ref, pop = shared["flex"], shared["rmac"], shared["ref"], shared["pop"]

    booked = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # eat date -> mt -> rid -> n (by user)
    booked_users = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))
    cook_counts = defaultdict(lambda: defaultdict(int))  # cook date -> rid -> n
    out_days = []; fails = 0
    for p in plans:
        SIM["now"] = datetime.strptime(p["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
        uid = p["user_id"]
        meals_map = {mt: mt for mt in ["breakfast", "lunch", "snack", "dinner"] if mt in p["mts"]}
        dates = [P(x["date"]) for x in p["days"]]
        cook_of = {P(x["date"]): P(x["cook"]) for x in p["days"]}
        tgt = target_for(targets[uid], p["created_at"]) if targets[uid] else None
        if not tgt: continue
        high = tgt["kcal"] > dms.HIGH_KCAL_THRESHOLD
        other = {}
        for d in dates:
            m = {}
            for mt, rids in booked[d].items():
                c = {rid: n - booked_users[d][mt][rid].get(uid, 0) for rid, n in rids.items()}
                c = {k: v for k, v in c.items() if v > 0}
                if c: m[mt] = c
            if m: other[d] = m
        templates = dms.build_week_templates(meals_map=meals_map, available_dates=dates,
            allowed_recipe_ids_by_date=allowed_by_date, all_recipes=all_recipes, flex_stats=flex,
            popularity=pop, recipe_macros=rmac, reference_target=ref, other_orders=other,
            menu_id_by_date=menu_id_by_date)
        elig = defaultdict(list)
        for r in all_recipes:
            for mt in set(meals_map.values()):
                if r.get(f"could_be_{mt}"): elig[mt].append(r["id"])
        rng = random.Random(f"{seed}-{p['plan_id']}")
        plan_counts = defaultdict(int); prev = set()
        cum = {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}; wk = None
        trials = ("dinner",) if len(dates) > 7 else ("dinner", "lunch")
        for i, d in enumerate(dates):
            if (d - dates[0]).days // 7 != wk:
                wk = (d - dates[0]).days // 7; cum = {k: 0.0 for k in cum}
            tpl = {} if high else templates.get(d, {})
            # batch signal = what's already being cooked that cook-day + today's template
            cd = cook_of[d]
            SIM["batch"] = dict(cook_counts[cd])
            for info in tpl.values():
                SIM["batch"][info["recipe_id"]] = max(SIM["batch"].get(info["recipe_id"], 0), SIM["full"])
            rbm = dms.apply_client_overrides(template_for_date=tpl, meals_map=meals_map,
                allowed_ids_today=allowed_by_date[d], user_prefs=prefs[uid], recipe_lookup=recipes_by_id,
                recipe_macros=rmac, macro_target=tgt, plan_counts=plan_counts, previous_day_recipes=prev, rng=rng,
                batch_counts=dms.build_batch_counts(other.get(d), tpl) if name in ("prod", "daily4") or name.startswith("hybrid") else None)
            if not rbm:
                fails += 1; continue
            day_t = tgt if i == 0 else mps.apply_weekly_carryover(tgt, cum)
            subs, loss, tot = mps.optimize_subrecipes(rbm, day_t, user_id=uid)
            r2, t2, sw, s2, l2 = dms.repair_day_if_needed(recipes_by_meal=rbm, meals_map=meals_map,
                day_totals=tot, macro_target=day_t, allowed_ids_today=allowed_by_date[d],
                eligible_by_meal_type=elig, recipe_macros=rmac, plan_counts=plan_counts,
                previous_day_recipes=prev, last_eaten={}, flex_stats=flex, recipe_lookup=recipes_by_id,
                trial_meal_types=trials, user_id=uid)
            if sw is not None:
                rbm, tot = r2, t2
            cum = mps.update_cumulative_deviation(cum, tgt, tot)
            tmatch = sum(1 for mk, info in rbm.items() if tpl.get(mk, {}).get("recipe_id") == info["recipe_id"])
            for mk, info in rbm.items():
                rid = info["recipe_id"]; plan_counts[rid] += 1
                booked[d][meals_map[mk]][rid] += 1; booked_users[d][meals_map[mk]][rid][uid] += 1
                cook_counts[cd][rid] += 1
            prev = {info["recipe_id"] for info in rbm.values()}
            kdev = abs(tot.get("kcal", 0) - tgt["kcal"]) / tgt["kcal"] if tgt["kcal"] else 0
            out_days.append({"plan": p["plan_id"], "user": uid, "date": str(d), "cook": str(cd),
                             "recipes": [rbm[mk]["recipe_id"] for mk in rbm], "tmatch": tmatch, "n": len(rbm),
                             "kdev": kdev, "repair_needed": dms.needs_repair(tot, tgt), "high": high})
    return out_days, fails


def actual_days(plans):
    out = []
    for p in plans:
        for x in p["days"]:
            out.append({"plan": p["plan_id"], "user": p["user_id"], "date": x["date"], "cook": x["cook"],
                        "recipes": [rid for _, rid in x["actual"]], "n": len(x["actual"])})
    return out


def metrics(days, win_lo, win_hi, names):
    W = [d for d in days if win_lo <= P(d["cook"]) <= win_hi]
    per = defaultdict(Counter)
    for d in W:
        for rid in d["recipes"]: per[d["cook"]][rid] += 1
    batches = [n for c in per.values() for n in c.values()]
    weekly = defaultdict(lambda: defaultdict(set))
    for cd, c in per.items():
        wk = P(cd).isocalendar()[1]
        for rid in c: weekly[wk][rid].add(cd)
    days_per_recipe = [len(s) for w in weekly.values() for s in w.values()]
    m = {"meals": sum(batches), "cook_days": len(per), "batches": len(batches),
         "avg_portions_per_batch": round(sum(batches) / max(1, len(batches)), 2),
         "single_portion_batches": sum(1 for n in batches if n == 1),
         "batches_le2": sum(1 for n in batches if n <= 2),
         "pct_meals_in_batches_ge5": round(sum(n for n in batches if n >= 5) / max(1, sum(batches)), 3),
         "distinct_recipes_per_day": round(len(batches) / max(1, len(per)), 1),
         "cook_days_per_recipe_wk": round(sum(days_per_recipe) / max(1, len(days_per_recipe)), 2)}
    # variety per plan inside window
    by_plan = defaultdict(list)
    for d in W: by_plan[d["plan"]].append(d)
    reps = []; distinct_ratio = []; consec = 0; consec_tot = 0
    for pdays in by_plan.values():
        pdays.sort(key=lambda x: x["date"]); allr = [r for x in pdays for r in x["recipes"]]
        if allr:
            reps.append(max(Counter(allr).values())); distinct_ratio.append(len(set(allr)) / len(allr))
        for a, b in zip(pdays, pdays[1:]):
            consec_tot += 1; consec += bool(set(a["recipes"]) & set(b["recipes"]))
    m["avg_distinct_ratio_per_client"] = round(sum(distinct_ratio) / max(1, len(distinct_ratio)), 3)
    m["max_same_recipe_per_client_avg"] = round(sum(reps) / max(1, len(reps)), 2)
    m["pct_days_repeating_yesterday"] = round(consec / max(1, consec_tot), 3)
    if W and "kdev" in W[0]:
        m["template_match"] = round(sum(d["tmatch"] for d in W) / max(1, sum(d["n"] for d in W)), 3)
        m["mean_kcal_dev"] = round(sum(d["kdev"] for d in W) / len(W), 4)
        m["pct_days_off_target"] = round(sum(d["repair_needed"] for d in W) / len(W), 3)
    m["top_singles"] = Counter(names.get(rid, rid) for c in per.values() for rid, n in c.items() if n == 1).most_common(5)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="base,a_template,a_full")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--lo", default="2026-11-09"); ap.add_argument("--hi", default="2026-11-20")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="sim_results.json")
    a = ap.parse_args()
    lo, hi = P(a.lo), P(a.hi)
    app = Flask("sim")
    t0 = time.time()
    plans, prefs, targets, wms = load(lo, hi)
    if a.limit: plans = plans[:a.limit]
    names = {}
    for wm in wms:
        for wmr in wm.get("weekly_menu_recipe") or []:
            r = (wmr or {}).get("recipe")
            if r: names[r["id"]] = r["name"]
    all_ids = list(names)
    weekdays = list(range(7))
    shared = {"flex": dms.prefetch_flex_stats(all_ids), "rmac": dms.prefetch_recipe_macros(all_ids),
              "ref": dms.population_reference_target(), "pop": dms.prefetch_weekday_popularity(all_ids, weekdays),
              "sales": Counter(rid for p in plans for x in p["days"] for _, rid in x["actual"]),
              "core_share": float(os.environ.get("CORE_SHARE", "0.5"))}
    print(f"loaded {len(plans)} plans, {sum(len(p['days']) for p in plans)} plan-days in {time.time()-t0:.0f}s", flush=True)
    results = {"actual": metrics(actual_days(plans), lo, hi, names)}
    print("actual", json.dumps(results["actual"]), flush=True)
    for v in a.variants.split(","):
        for s in range(a.seeds):
            t1 = time.time()
            with app.app_context():
                days, fails = run_variant(v, plans, prefs, targets, wms, s, shared)
            m = metrics(days, lo, hi, names); m["fails"] = fails
            results[f"{v}#{s}"] = m
            print(v, s, f"{time.time()-t1:.0f}s", json.dumps(m), flush=True)
    json.dump(results, open(a.out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
