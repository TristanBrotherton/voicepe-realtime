# Mätfiler till PR #74 (GEMINI_EGEN_TUR), 2026-10-13

Tre sorters mätning. De får inte blandas ihop, och ingen av dem har en människa som talar till honom i rummet.

## `tyst_prov/`: inspelat ljud direkt till Gemini Live, ingen högtalare
- `sond.py` körde `plan.json` (70 försök) och `plan2.json` (58, varav 6 föll på Googles utgiftstak). Rådata: `ut.jsonl`, `ut2.jsonl`
  (en rad per session: tider för `avbruten`, `tur_klar`, vad Gemini skrev att den hörde, när svar började). `las.py ut.jsonl x` och
  `las2.py` räknar; deras utskrifter ligger i `las_ut.txt` och `las2_ut.txt`. `bygg.py` visar hur klippen gjordes (klippen själva, rå pcm,
  är inte incheckade; ekoinspelningarna ligger på core under `/root/aec_prov1700` och `/root/aec_prov1900`).
- `rum.py` + `rum_ut.jsonl`: en inspelad minut av Kontor (tredje kanalen i `allt_20261013_193145.pcm`, på core under `/root/aec_prov2130`)
  spelad mot Gemini, två filer × START HIGH och START LOW, en gång var.
- Det här KOSTAR pengar och går förbi husets minutspärr. Kör det inte utan ägarens ja.

Vad siffrorna är och inte är (granskat av adversarial-critic, se `granskning_tyst_prov.md`):
- "Eko"-klippen är mest brusgolv (−52 till −59 dBFS) med en läcka i klippets början. Alla avbrott utan röst kom under klippens första 1,7 s
  och från 4 av 10 källfiler. Ingen kontroll med ren tystnad. Det går inte att säga om START HIGH reagerade på eko eller på golvet.
- Rösterna är inlagda digitalt EFTER ekosläckaren. Medhållen är syntetiska och på normal röstnivå. Varje klipp är kört en gång per inställning.
- Sondens instruktion är en mening, inte den riktiga. Språkfelen i avskrifterna säger därför inget om live.

## `slackare/`: offline, ingen motor
Inspelningarna (mikrofon + referens, de ramar släckaren fick live) genom WebRTC AEC3 igen, en släckare för hela kvällen.
- `start.py` → `start_ut.txt`: var i svaret hans eget ljud blir kvar (11 svar med bara eko efter kl 15).
- `ns.py` → `ns_ut.txt`: brusdämparens nivåer 0-3 (10 ekosvar, 4 klipp med Henriks röst; bara toppnivån är jämförd, inte hur rösten låter).
- `sparr.py` → `sparr_ut.txt`: spärren 2 s + brusdämpare 3, vad som då går vidare.
- `mellan.py`, `jamn.py` → två saker som inte tog bort läckan i början.
Svagheter: en kväll, ett rum, en volym; "bara eko" avgörs av en nivåregel i skriptet (inte av journalen); 11-12 svar.

## `live/`: Kontor 21:26-22:05, riktig motor och högtalare, frågan inlagd digitalt
`journal_urval_2126-2205.txt` (UTC, lokal tid − 2 h): faser, motorns avbrott, vad Gemini skrev att den hörde. START HIGH,
`SOFT_AEC_NS=3`, `SOFT_AEC_REST=2`, `GEMINI_EGEN_TUR=kontoret`. Henrik arbetade i rummet. Det här är EN session om sex minuter.
- 5 frågor besvarade, 17 svar påbörjade (han svarade alltså 12 gånger på något annat än en fråga).
- 8 `interrupted by the engine`. Minst 1 av dem (19:32:15) är min egen nästa inlagda fråga som kom medan han talade; kanske 2 (19:29:28).
  Resten har ingen röst till honom bakom sig.
- 13 avskrifter från Gemini; utöver den inlagda frågan står där '¿Qué?', 'No.', 'Claro.', 'Ho fatto il pane ripieno.' med flera.
- Inspelningen av vad motorn fick (`AEC_SPARA_ALLT`) ligger på core, `/root/aec_prov2130/allt_20261013_193145.pcm` (3 kanaler, 6,6 min).
