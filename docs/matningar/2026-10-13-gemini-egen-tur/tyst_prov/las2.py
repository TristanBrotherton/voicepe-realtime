import json, statistics as st
plan = {p["id"]: p for p in json.load(open("plan2.json"))}; sett = set(); r = []
for l in open("ut2.jsonl"):
    u = json.loads(l)
    if u["id"] in sett or "fel" in u: continue
    sett.add(u["id"]); r.append(u)
def av(u): return [h[0] for h in u["hand"] if h[1] == "avbruten"]
for g, lag in (("A-varm", True), ("A-kall", True), ("G-A", False)):
    x = [u for u in r if u["id"].startswith(g) and u["id"].endswith("-lag") == lag]
    if x: print(g + ("-lag" if lag else ""), len(x), "svar, självavbrott:", sum(1 for u in x if av(u)), [(u["id"], u["inn"]) for u in x if av(u)])
for g in ("B-", "D-", "G-B"):
    x = [u for u in r if u["id"].startswith(g)]; stp = []; fore = ingen = 0
    for u in x:
        p = plan[u["id"]]; t = u["klipp_start"] + p["rost_t"]; a = av(u); fore += any(y < t for y in a); e = [y for y in a if y >= t]
        if e: stp.append(round(e[0] - t, 2))
        else: ingen += 1
        print("   ", u["id"], p["kalla"], "stopp", round(e[0] - t, 2) if e else None, repr(u["inn"]), "|", repr(u["sa"][-70:]))
    if x: print(g, len(x), "försök, avbruten före rösten:", fore, "aldrig avbruten:", ingen, "median", st.median(stp) if stp else None, "tider:", sorted(stp))
print(len(r), "klara av", len(plan), "| min:", round(sum(u.get("langd", 0) for u in r) / 60, 1))
