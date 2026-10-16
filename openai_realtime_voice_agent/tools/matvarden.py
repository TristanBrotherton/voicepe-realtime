"""Measures for a day of speaker sessions, from the agent's journal (raawr US-032 AC-10).

    journalctl -u raawr-rostagent --since today --no-pager | python tools/matvarden.py [--json]

Per room: sessions, time from the end of speech to sound (`⏱ tider` lines, US-047; tool turns apart), time to first sound (THINKING -> SPEAKING) and reflex
(THINKING -> a bana 0 hit) as P50/P95, connected minutes (WAKE -> back to IDLE),
interruptions (SPEAKING -> LISTENING) and close reasons. A tider line with a bana 0
hit since the room's previous tider line is a personal reply when it carries modell=
(P50/P95 of that value, ms) and a short reply otherwise (P50/P95 of enhet, ms).

Only timestamps, states and the code's own close reasons are read. What was said
(the "heard" and "hit" lines) is matched on its prefix and never copied.
A "hit" line carries no room: it is given to the room that last went THINKING.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3}) - \S+ - \w+ - (.*)")
TIDER = re.compile(r"⏱ tider (\S+) ((?:\w+=\d+ ?)+)(utan_ljud)?")  # US-047 AC-1: ms after the end of speech
KEDJA = re.compile(r"🧭 (\S+): (\w+) -> (\w+) \((\w+): (.*)\)$")


def tid(s):
    return datetime.strptime(s.replace(",", "."), "%Y-%m-%d %H:%M:%S.%f").timestamp()


def pct(v, p):
    """Nearest rank; None when empty."""
    if not v:
        return None
    v = sorted(v)
    return round(v[max(0, -(-len(v) * p // 100) - 1)], 3)


def mat(rader):
    rum = defaultdict(lambda: {"sessioner": 0, "forsta_ljud": [], "reflex": [], "minuter": 0.0,
                               "avbrott": 0, "stangning": Counter(), "efter_tal": [], "efter_tal_verktyg": [],
                               "traff_personligt": [], "traff_kort": []})
    vak, tank, senast_tank, traff_oppen = {}, {}, None, set()
    for rad in rader:
        m = LINE.match(rad.strip().split(" env[", 1)[-1].split("]: ", 1)[-1])
        if not m:
            continue
        t, text = tid(m.group(1)), m.group(2)
        tm = TIDER.match(text)
        if tm and not tm.group(3):  # a turn with sound; the steps are ms after the end of speech
            steg = dict(x.split("=") for x in tm.group(2).split())
            namn = tm.group(1)
            if "enhet" in steg:
                rum[namn]["efter_tal_verktyg" if "verktyg" in steg else "efter_tal"].append(int(steg["enhet"]) / 1000)
            if namn in traff_oppen:  # a bana 0 hit since this room's previous tider line
                traff_oppen.discard(namn)
                if "modell" in steg:
                    rum[namn]["traff_personligt"].append(int(steg["modell"]))
                elif "enhet" in steg:
                    rum[namn]["traff_kort"].append(int(steg["enhet"]))
            continue
        if tm:  # utan_ljud still ends the hit window; nothing was played
            traff_oppen.discard(tm.group(1))
            continue
        if text.startswith("bana0: hit "):  # the text after the prefix is never read
            if senast_tank and senast_tank in tank:
                rum[senast_tank]["reflex"].append(t - tank[senast_tank])
                traff_oppen.add(senast_tank)  # same room as the reflex
            continue
        k = KEDJA.match(text)
        if not k:
            continue
        r, fran, till, handelse, orsak = k.groups()
        d = rum[r]
        if till == "WAKE" and fran in ("IDLE", "CLOSING"):
            d["sessioner"] += 1
            vak[r] = t
        if till == "THINKING":
            tank[r], senast_tank = t, r
        if till == "SPEAKING" and r in tank:  # once per THINKING: a VAD flap THINKING -> LISTENING -> SPEAKING still
            d["forsta_ljud"].append(t - tank.pop(r))  # counts, a later SPEAKING of the same reply does not
        if fran == "SPEAKING" and till == "LISTENING":
            d["avbrott"] += 1
        if till == "CLOSING" and handelse == "close":
            d["stangning"][orsak] += 1
        if till == "IDLE" and fran == "CLOSING" and r in vak:
            d["minuter"] += (t - vak.pop(r)) / 60
        if till in ("IDLE", "WAKE"):
            tank.pop(r, None)
    return {r: {"sessioner": d["sessioner"], "uppkopplade_minuter": round(d["minuter"], 1),
                "forsta_ljud_s": {"p50": pct(d["forsta_ljud"], 50), "p95": pct(d["forsta_ljud"], 95), "n": len(d["forsta_ljud"])},
                "reflex_s": {"p50": pct(d["reflex"], 50), "p95": pct(d["reflex"], 95), "n": len(d["reflex"])},
                "efter_talets_slut_s": {"p50": pct(d["efter_tal"], 50), "p95": pct(d["efter_tal"], 95), "n": len(d["efter_tal"]),
                                        "med_verktyg_p50": pct(d["efter_tal_verktyg"], 50), "med_verktyg_n": len(d["efter_tal_verktyg"])},
                "traff_personligt": {"n": len(d["traff_personligt"]), "p50": pct(d["traff_personligt"], 50),
                                     "p95": pct(d["traff_personligt"], 95)},
                "traff_kort": {"n": len(d["traff_kort"]), "p50": pct(d["traff_kort"], 50),
                               "p95": pct(d["traff_kort"], 95)},
                "avbrott": d["avbrott"], "stangningsorsaker": dict(d["stangning"])}
            for r, d in sorted(rum.items())}


if __name__ == "__main__":
    ut = mat(sys.stdin)
    if "--json" in sys.argv:
        print(json.dumps(ut, ensure_ascii=False, indent=1))
    else:
        for r, d in ut.items():
            print(f"{r}: {d['sessioner']} sessioner, {d['uppkopplade_minuter']} min uppkopplad, "
                  f"första ljud P50/P95 {d['forsta_ljud_s']['p50']}/{d['forsta_ljud_s']['p95']} s (n={d['forsta_ljud_s']['n']}), "
                  f"reflex P50/P95 {d['reflex_s']['p50']}/{d['reflex_s']['p95']} s (n={d['reflex_s']['n']}), "
                  f"efter talets slut (utan verktyg) P50/P95 {d['efter_talets_slut_s']['p50']}/{d['efter_talets_slut_s']['p95']} s (n={d['efter_talets_slut_s']['n']}), "
                  f"{d['avbrott']} avbrott, stängning: {d['stangningsorsaker']}")
            p, k = d["traff_personligt"], d["traff_kort"]
            print(f"  personligt svar P50/P95 {p['p50']}/{p['p95']} ms (n={p['n']})")
            print(f"  kort svar P50/P95 {k['p50']}/{k['p95']} ms (n={k['n']})")
