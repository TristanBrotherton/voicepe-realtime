import json, sys, statistics as st
plan = {p["id"]: p for p in json.load(open("plan.json"))}
res = [json.loads(l) for l in open(sys.argv[1])]
visa = len(sys.argv) > 2
def av(u): return [h[0] for h in u.get("hand", []) if h[1] == "avbruten"]
def med(x): return f"{st.median(x):.2f}" if x else "-"
for grupp in ("A-varm", "A-kall"):
    r = [u for u in res if u["id"].startswith(grupp) and "fel" not in u]
    sj = [u for u in r if av(u)]; hort = [u for u in r if u["inn"].strip()]
    print(f"{grupp}: {len(r)} svar med bara eget eko. Avbröt sig själv: {len(sj)}. Ekot togs som tal: {len(hort)}", [(u['id'][-6:], round(av(u)[0] - u['klipp_start'], 2), u['inn']) for u in sj] if visa else "")
for grupp, namn in (("B-", "röst över ekot"), ("D-", "medhåll över ekot")):
    r = [u for u in res if u["id"].startswith(grupp) and "fel" not in u]; stopp = []; svar = []; fore = 0; ingen = 0
    for u in r:
        p = plan[u["id"]]; t = u["klipp_start"] + p["rost_t"]; a = av(u)
        if any(x < t for x in a): fore += 1
        eft = [x for x in a if x >= t]
        if not eft: ingen += 1
        else: stopp.append(eft[0] - t)
        ny = [x["start"] for x in u["turer"] if x["start"] > t + 0.05]
        if ny: svar.append(ny[0] - (t + p["rost_s"]))
        if visa: print("  ", u["id"], p["kalla"], "stopp", f"{eft[0]-t:.2f}" if eft else "-", "| nytt svar", f"{ny[0]-(t+p['rost_s']):.2f}" if ny else "-", "| hört:", repr(u["inn"]), "| sa sist:", repr(u["sa"][-90:]))
    print(f"{grupp} {namn}: {len(r)} försök. Avbruten före rösten (eget eko): {fore}. Aldrig avbruten: {ingen}. Tid röststart->avbruten median {med(stopp)} max {max(stopp, default=0):.2f}. Nytt svar efter röstens slut median {med(svar)}")
for aad in ("forval", "idag", "talig"):
    r = [u for u in res if u["id"].startswith("C-") and u["id"].endswith(aad) and "fel" not in u]; mitt = 0; lat = []; inget = 0
    for u in r:
        p = plan[u["id"]]; k = u["klipp_start"]; slut = k + p["slut"]
        tidig = [x["start"] for x in u["turer"] if x["start"] < slut]
        if tidig: mitt += 1
        sen = [x["start"] for x in u["turer"] if x["start"] >= slut]
        if sen: lat.append(sen[0] - slut)
        else: inget += 1
        if visa: print("  ", u["id"], "svar i pausen" if tidig else "väntade", "| svar efter slut", f"{sen[0]-slut:.2f}" if sen else "-", "| hört:", repr(u["inn"]), "| sa:", repr(u["sa"][:100]))
    print(f"C paus 1,2 s, inställning {aad}: {len(r)} försök. Svarade mitt i meningen: {mitt}. Inget svar alls: {inget}. Första ljud efter meningens slut median {med(lat)} max {max(lat, default=0):.2f}")
fel = [u["id"] for u in res if "fel" in u]
print("fel:", fel, "| minuter mot Gemini:", round(sum(u.get("langd", 0) for u in res) / 60, 1))
