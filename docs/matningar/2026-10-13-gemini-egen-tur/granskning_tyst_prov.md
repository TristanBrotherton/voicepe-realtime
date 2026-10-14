# adversarial-critic om det tysta provet (2026-10-13), i sammandrag

Granskaren körde `las.py`, `las2.py` och egna uträkningar på `ut*.jsonl` och klippen. Dom per påstående:

1. "START HIGH avbröt sig själv 9 av 20, LOW 0 av 20": talen håller. Som "eko" håller det inte: 10 filer två gånger, inte 20 oberoende; klippet
   A-varm-150122 ligger på −55 till −59 dBFS rakt igenom och avbröts ändå vid 0,93 s. Ingen kontroll med tystnad.
2. "Röst avbryter 20 av 20 med HIGH, 7 av 10 med LOW": håller. Median för röst + medhåll samlat 0,18 s. `bestamd` missades också en gång med HIGH efter grind: en miss kan vara slump.
3. "Avbruten av ekot före rösten 7 av 20 (HIGH), 0 av 14 (LOW)": talen håller men är punkt 1 igen: fyra källfiler står för alla sju.
4. "Medhåll avbröt 14 av 14": håller (10 HIGH + 4 LOW; 6 LOW-försök föll på utgiftstaket). Allt syntetiskt på −21 till −23 dBFS.
   "Börjar ofta om sagan eller frågar om han ska fortsätta": går inte att avgöra, syns i 1-2 av 14.
5. Pausprovet: talen håller (2, 3, 0 av 10) men två klipp är samma klipp; "0 av 10 med 1500 ms" kan inte falla på en paus på 1,2 s; ingen kontroll utan paus.
6. Fel språk: 3 av 30 röstförsök över eko + 1 utanför de 30; alla på Henriks riktiga röst. Gäller inte live (annan instruktion).
7. "4 gånger snabbare än realtid": håller för två rader (64 s på 16 s); andra rader 7-10 gånger. "`turn_complete` vid start + längd": håller.
   "Motorns avbrott gäller hela uppspelningen": går inte att avgöra, rösten kom alltid 4-6 s in.
8. "Gemini väntar ut pausen bara för att svaret inte hinner börja": inte visat, en hypotes.
9. Slutsatsen "man måste välja mellan självavbrott (HIGH) och missade ord (LOW)": håller inte som formulerad. Raderna med grind (G-) visar HIGH + grind: 1 av 8 utan röst, 9 av 10 med röst.
10. Kostnad: 123 sessioner, 35,3 min in, 65,1 min ut: håller.

De tre tyngsta invändningarna: (1) ekoklippen är brusgolv, och rösten ligger inte över något eko; (2) rösten är inlagd efter släckaren; (3) pausen är exakta nollor med syntetisk satsmelodi.
