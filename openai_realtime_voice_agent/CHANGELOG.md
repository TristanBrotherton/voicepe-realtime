# Changelog

All notable changes to this add-on. Newest first.

## Unreleased (fork)

- **Loggrader för AC-19 (följdfrågan efter ett avbrott)**, raawr US-060. Ren loggning, ingen ändring av beteende: när rummets ljud hålls tillbaka av porten (`🚪 audio gate was closed: N frames`), den lokala detektorns tal-start och tal-slut (`🎙️ local detector: speech start/end`, med om han svarar, om motorn genererar och om svansen är tystad), motorns första ljud i en tur med tiden sedan sista talslut (`🔈 engine audio starts`), och vad ett avbrutet svar hade hunnit säga (`💬 Björn sa (avbruten, motorn|stoppvakten)`, som annars aldrig loggas). Så att nästa prov med Henrik kan avgöra om frågan inte nådde motorn, om motorn svarade för kort eller om svaret tystades.
- **Stoppordsvakten, rättad efter B:s granskning av #94.** En felstavning av ett stoppord ('stå', 'tis', 'anta') räknas bara som FÖRSTA ord (i en mening är de vanliga ord); ett ljud som STT inte kunde skriva ('inget': en smäll, en dörr) måste vara över -26 dBFS (`STOPP_NIVA_INGET_DBFS`) för att klippa, tal över -30; en gemensam nivåfunktion i stället för två. G:s slutpass (ett anrop) hittade en riktig brist: den tomma turn_complete efter ett verktygsanrop (och själva verktygsanropet) avslutade inte tystnaden av det avbrutna svaret, så svaret på nästa tur kunde sväljas i upp till 8 s; lagat med två prov som faller utan ändringen. Hans två andra fynd (UserStopped efter ett kort stoppord, 0,3 s efter turen) stämde inte: `_avbrott_t` stänger lyssningen igen efter 1,2 s (nytt prov), och nästa svar börjar först efter nästa tal. Hela sviten körs en gång före merge. Offline som förut: stoppord 12/12 (beslut 820-990 ms), medhåll 0/10.
- **Stoppordsvakt och utökad medhållslista** (raawr US-060 AC-17/AC-18, Henrik 2026-10-17; bara där motorn sköter turerna, `GEMINI_EGEN_TUR`; av med `STOPP_VAKT=0`). De första 0,6 s tal över hans svar (0,3 s före + 0,6 s, `STOPP_MIN_S`) skrivs ut lokalt och bedöms av `eko_vakt.stopp_dom`: stoppord och 'Björn' (även som STT stavar dem: stå, stål, tis, välta, änta, anta) klipper över -36 dBFS; annat tal som varken är medhåll eller hans egna ord klipper över -30 dBFS (`STOPP_NIVA_DBFS`); medhåll (ja, okej, mm, aa, åh, precis, och en utdragen 'ååååå'/'mmmm') klipper aldrig. Svansen motorn fortsätter generera tystas bara medan den genererar och tills dess tur är slut (8 s högst), så svaret på nästa fråga sväljs inte. Offline (inrenderad röst genom kedjan, gain 50): stoppord 12/12, beslut 820-890 ms; medhåll 0/10 klipper. Geminis egna avbrott är orörda. 33 nya prov (`tests/test_stopp_vakt.py`), hela sviten 1090 passed, 1 skipped.
- **Frågevakten läser Comms märkning** (raawr US-103). Varje Comms-verktyg säger nu om det bara läser (`readOnlyHint` i tools/list, och per verktyg i `las_skill`-svaret). Dörrens klient sparar märkningen (`LasandeMCPClient.las`), och sessionen får den (`service.lasande`). Vakten lär sig dessutom av varje `las_skill`-svar. På en fråga nekas ett verktyg som Comms säger ändrar något. Ett verktyg som körs genom `anvand_verktyg` och som Comms inte har beskrivit räknas också som ändrande. HA:s egna verktyg bedöms fortfarande efter namnlistan `SKRIV_VERKTYG`.
- **Fler frågor känns igen som frågor** (B:s granskning av #91). En fråga utan frågeord först räknas nu också: den slutar på '?', nämner ett läge (tänt, lyser, brinner, på, funkar) och har inget befallningsverb ('Tänt i kontoret?', 'Brinner det i köket?', 'Funkar lampan?'). 'Kan du säga/berätta/kolla om…' räknas också som en fråga. 'Kan du tända…', 'Tänd lampan?' och 'Kan du se till att det är tänt…' är fortfarande order.
- **En fråga om huset är aldrig en order** (raawr US-098; live 2026-10-17 00:20). 'Är det tänt i kontoret?' fick modellen att köra `HassTurnOn {area: kontoret, domain: light}`: bana 0 hörde frågan rätt, men 'tänt' och 'tänd' låter lika, motorn skrev ingen egen utskrift, och prompten säger att verktygen används direkt. Tre lampor tändes. Nu nekar verktygslagret ett växlande verktyg (`HassTurnOn`, `HassTurnOff`, `HassLightSet`, `styr`, och `anvand_verktyg` som kör `styr`) när turens lokala text är en fråga (`bana0.ar_fraga`: första ordet är ett frågeord och inte en artig begäran som 'Kan du…' eller 'Var snäll och…'). Modellen får en rad om att svara på frågan och inte ändra något. Samma koll i `bana0.verb_ur`, som läste 'Vilka lampor är tända?' och 'Är lampan på i köket?' som order att tända.
- **Mätkitet för skills-vägen** (raawr US-098): `tools/skills_matning.sh` kör samma åtta frågor (`tools/skills_fragor.jsonl`, Alva via `say`, lokal STT förstår alla) genom attrappen mot en sidoinstans per väg (a dagens, b, c), med tid till första ljud, tid per verktyg (`anvand_verktyg` visas som Comms-verktyget det körde) och rätt svar mot facit. Lampfrågorna gäller fönsterlampan i kontoret; varje lampas på/av sparas före och återställs efter (`tools/ljus_lage.py`). En fråga om en lampa som inte finns (trädgårdsstugan) körs inte live: en felgissning kunde tända en annan, kanske i vardagsrummet. `DORR_IDAG` i `app/skills.py` är avsiktligt hårdkodad: den fryser dörrverktygen på dagens åtta tills mätningen valt väg, och tas bort när skills-vägen blir standard.
- **Husets verktyg genom Comms skillkatalog** (raawr US-098, E-07/D-27 punkt 5; av tills mätningen valt väg, `ROST_SKILLS=b|c`). Med `b` har sessionen bara Comms `las_skill` och `anvand_verktyg` (US-089) plus röstens egna skills (follow_up, falsk väckning, röst-ID, timers), i stället för 51 verktyg; `c` lägger till `hus_lage` och `styr` direkt. Prompten är Björnkärnan plus katalogens en rad per skill (`<rummets comms>/skills`, 1 s frist, senast hämtade vid hicka, annars en mening om att huset inte nås). Spärrarna sitter i Comms; ett nej (`isError`) läses upp som sin mening.
- **Full duplex: ett samtal, inte ett kommando** (raawr US-060; bara med `BARGE_IN=1` utom spärren mot tur utan väckning). Henrik 2026-10-13: "ett enda ord som avbryter Björn får inte tvingas till 'vänta'", och "om jag säger vänta så ska den inte stänga av mikrofonen, vänta betyder att jag vill ha ett annat svar".
  - **Vanlig röst avbryter.** Enheten säger till när svarsljudet hackat (`{"type":"underrun"}`, firmware raawr #741; `play_stamp` bär `"ur":1`). Då vet agenten när släckaren kan ha tappat honom (3 s försiktighet), och golvet för nivåklippet kan vara det släckaren lämnar kvar av hans starkaste ramar plus 8 dB (`NIVA_OVER_REST_DB`, lägst −36 dBFS) i stället för hela hans eko. Utan rapport (äldre firmware) gäller det gamla golvet. Simulerat: 42 glapp, 0 falska klipp med rapport och 3 s, 14–16 utan rapport.
  - **Ett ord är ett ord.** Bara tomt, hålla-ord ("vänta lite", "nej tack") eller ett felstavat hålla-ord ('Välta!') räknas som "vänta". "Hjälp", "tänd" och allt annat är en vanlig tur som besvaras.
  - **Efter "vänta" är mikrofonen öppen.** Turen behålls, enheten hålls i `listening` i `VANTA_LYSSNAR_S` (10 s), och det han säger sedan läggs till samma tur och besvaras. Sägs inget mer kastas turen och enheten stänger mikrofonen. Resten av det avbrutna svaret spelas inte.
  - **Fel som lagas:** efter ett avbrott gick `idle` till enheten mitt i personens tur (hans eget "slutade tala" startade idle-klockan), och enheten stänger mikrofonen på `idle` medan den lyssnar. Det var därför "vänta" stängde mikrofonen.
  - **Tur utan väckning kastas** (`TUR_UTAN_VACKNING_S`, 60; gäller alltid). Kontor 18:08 och 18:35: provfirmwaren lämnad på efter ett prov, mikrofonen stod öppen, och han svarade fem gånger på prat i rummet 2944 s och 1430 s efter senaste svaret. En tur som börjar mer än 60 s efter senaste svar, idle och väckning kastas, och enheten får `idle` så att den stänger mikrofonen.
  - Provkrokar (bara `EKO_PROV=1`, loopback): `/rost-prov` lägger en inspelad röst på den levande mikrofonen, `/glapp-prov` håller svarsljudet en gång.

- **Full duplex: ett ensamt "vänta" får honom att tystna och lyssna, inget svar** (raawr US-060; bara med `BARGE_IN=1`). Kontor 2026-10-13 16:20: "vänta" klippte Björn, skrevs 'Välta!' eller 'Tack.', och tre sekunder senare svarade han på ordet (eller så stängde 'Tack.' sessionen). Henrik: "det flyter inte på som ett samtal". Nu: turen som klippte honom och bara är ett ord, inga ord alls eller bara stanna-ord (`eko_vakt.bara_avbrott`) går till motorn, så att den vet att den blev avbruten och slutar generera, men det den svarar spelas inte (`VANTA_TYST_S`, 12 s) och den kör inga verktyg på turen. Inget skickas till huset och sessionen stängs inte. En hel mening över honom besvaras som förut. Medan hans svar är tystat räknas han inte som talande, så det personen säger sedan är en vanlig tur. Inte provat live.

- **Full duplex: spärrarna gäller också turen som ett avbrott startar** (raawr US-060, A på #72). Eko-vetot och verktygsspärren gällde bara medan Björn svarade och 1,5 s efter. Men ett avbrott gör fasen till `listening` och sedan `thinking`, aldrig `idle`, så båda var av just när avbrottsturen tog slut och modellen anropade verktyg. Ett klipp på hans eget eko blev då en tur av hans egen röst med oskyddade verktyg. Nu är fönstret också öppet i `TOOL_VAKT_S` (8 s) efter ett avbrott (`eko_vakt.vakt_oppen`, `veto_tur`, `veto_verktyg`): tur som liknar hans svar kastas, verktyg körs inte på den och inte på tal utan utskrift. En riktig order efter avbrottet går igenom.

- **Full duplex: avbrott på ljudnivån, utan taldetektor och utan utskrift** (raawr US-060; bara med `BARGE_IN=1`, av med `NIVA_KLIPP=0`). Kontor 2026-10-13 12:07: Henrik sa "vänta" och "stopp" sju gånger över fyra svar, och Björn stannade en gång. Tre fel: taldetektorn (Silero) kallade aldrig ett rop på "stopp" för tal (3 av 7 ord fick ingen kandidat), ekosläckarens betyg räknades som energisumma så att hans förra ord dolde att släckaren fungerade (3,9 dB som summa, 30,8 dB som medianram), och spärren "hans egen utskrift ligger efter" kastade "vänta" eftersom Geminis utskrift av svaret ofta kommer efter svaret (0 ord efter 8,4 s). Nu: `MjukAec.person()` klipper när minst 0,12 s av de senaste 0,2 s ut ur släckaren är starkare än hans eget eko i mikrofonen (ekots starkaste ramar, 95:e percentilen, `NIVA_OVER_EKO_DB` −2), släckaren tog minst 20 dB strax före (medianramen), och svaret är 2 s gammalt utan hål i det som skickats. Varför "starkare än hans eget eko": glider enhetens uppspelning mitt i ett svar tappar släckaren honom i en sekund, och det som då kommer ut går inte att skilja från en röst (vågform och spektrum mot referensen, nytt filter, ekots läge: allt överlappar), men det kan inte bli starkare än ekot var. 84 simulerade glapp på rena inspelningar: 37 falska klipp vid fast −36 dBFS, 4 vid −4 dB, 0 vid −2 dB. Medan han talar klipper utskriften bara på ett stoppord (regeln "släckaren fungerar, alltså är allt tal en person" är av, `EKO_TILLIT_TEXT=1` slår på den: den har samma hål vid glapp). Spärren för sen utskrift gäller bara strax efter att han tystnat. Offline, agentens egen kedja med riktig tal-till-text på 37 inspelningar: 0 självavbrott, 0 av hans ord till motorn; Henriks första ord klippte 4 av 5 svar efter 0,29-0,44 s (det femte vid hans andra ord); Mac-röst på −30 dBFS 0 av 5. Obevisat live.

- **Bana 0 rör inte huset på en avskrift som bara är avskriftens egen ledtråd** (säkerhet). STT-bryggan ger Whisper ledtråden `Vad är klockan? Tänd i köket. Björn.` (HEN-362), och Whisper lämnar tillbaka den när den hör brus eller Björns egen röst ur högtalaren. Ur Björns eget eko, där ingen sa det, skrevs 2026-10-12/13 `Tänd i köket.` eller `Tänd i követ.` fyra gånger (tre i replay av liveinspelningar, en live), `Vad är klockan?` två gånger och `Björn.` en gång. Live 2026-10-12 21:40 gav `Tänd i köket.` `Slog på lampan`; om någon sa det är inte känt. Nu: är hela texten en eller flera ledtrådsfraser (på en bokstav när) skickas den inte till HA, och modellen får ljudet, inte texten. Ett riktigt "tänd i köket" görs då av modellen i stället, någon sekund senare. Säger man mer än ledtråden ("tänd i köket och hallen") går det snabba vägen som förut. Klockfrågan går kvar till klippen. `STT_INITIAL_PROMPT` ska sättas lika här som i bryggan om bryggans ändras. Alltid på, ingen flagga.
- **Mjukvaru-AEC och AGC i agenten** (raawr US-060, väg B; av som standard). `SOFT_AEC=1` (kräver `pip install aec-audio-processing` i agentens venv; saknas biblioteket händer inget): WebRTC AEC3 på mikrofonströmmen med det agenten själv skickar till högtalaren som referens (placerad vid sändningstid, utan extra fördröjning, för AEC3 hittar de 0,1-0,25 s själv), följt av en mjukvaru-AGC som bara lyfter (högst `SOFT_AGC_DB`, standard 24, 0 = av) och står still nära brusgolvet. Mätt offline på Kontors levande inspelningar (XMOS-AGC av): eko −22 → −55 dBFS (ERLE ~30 dB), "vänta" tappar 3-6 dB. Simulerat avstånd (talet 9,5 och 14 dB svagare): utan AGC hamnar 3 av 4 under −36 dBFS-gränsen, med AGC 1 av 4. Kräver Kontors firmware med `kanal0_steg: NONE` och `mic_gain: 100` (raawr #740). Obevisat med motor och riktiga avstånd.
  Dessutom i samma PR: `AEC_SPARA=1` sparar mikrofon (vänster) och referens (höger) som 16 kHz stereo-wav per uppspelning (`AEC_SPARA_KATALOG`, standard `/data/aec_spara`, tak `AEC_SPARA_MAX` 30) så ett liveprov kan spelas upp offline (innehåller röst: radera efteråt); en verktygsspärr (`TOOL_VAKT_S`, standard 8 s) som inte kör något verktyg på en tur som låter som Björns egen senaste replik (minst `MIN_ORD` ord) eller som saknar text strax efter ett självavbrott. Syntetisk återskapning av liveloopen 2026-10-12 (20 korta repliker, referensen med verklig rumsimpuls): utan AEC 20 av 20 falska avbrott (12 av 20 även med full text), med AEC3+AGC 1 av 13 (den allra första repliken, innan filtret lärt sig rummet), 7 av 7 "vänta" hörs. AEC:n behålls per enhet över återanslutningar.

## 0.29.8 (fork)

- **Ekonätet: nivågräns och kända hittepå-fraser** (raawr US-060, efter ekoprovet 07:00). Mätt på Kontor med aux (28 s klipp, 3 körningar, bara XMOS-lagret): rummets golv med Björns röst igång låg på −50 till −53 dBFS med toppar −41 till −47, och Geminis utskrift hade inga av hans ord (men "¿Qué tal?" ur bruset). Silero tog en gång brus för tal i 4,8 s, och bana 0 skrev "Björn." och "Tack till elever och personal …" ur golvet; nätet räknade det som en person. Nu: tal över hans svar vars högsta 0,25 s ligger under −36 dBFS (`EKO_NIVA_DBFS`; Henrik ligger på −12 till −20) är brus och ignoreras, och bana 0:s kända brusfraser räknas aldrig som en person.

## 0.29.7 (fork)

- **Full duplex: ekonätet i agenten och ett mätverktyg** (raawr US-060, D-22; allt av som standard). `BARGE_IN=1` slår på lager 2: tal som börjar medan Björn svarar, eller strax efter (`EKO_SVANS_MS`, 1,5 s), skrivs ut lokalt (bana 0) och jämförs med Björns egen utskrift av turen. Hans egna ord ignoreras (`🔊 … his own words, ignored`); annat tal är ett avbrott: turen startar som vanligt, fasen blir `listening` (enheten tömmer sin uppspelningskö, om firmwaren har `barge_in`), och resten av hans svar skickas inte (`tysta_svar`). `ANTI_EKO=0` släpper nätet så XMOS-lagret kan mätas ensamt. Firmware: `barge_in` är en substitution (standard false), Kontors stub sätter true. `EKO_PROV=1` slår på `POST /eko-prov` (loopback): spelar ett klipp med enhetens mikrofon öppen och lämnar tillbaka vad mikrofonen hörde; `tools/eko_prov.py` kör mätningen (AC-1) mot bana 0 och Gemini. Obevisat tills det är flashat och mätt (efter 07:00).

## 0.29.5 (fork)

- **Turens första ord tappas inte lika lätt** (raawr D-97). Av Henriks åtta sparade turer 2026-10-11 började tre redan på full talnivå i första 50 ms: det första ordet hade börjat före ringen. Molnets förljud är nu 1,0 s (var 0,5 s, `ENGINE_PREROLL_MS`) och bana 0:s ring 1,5 s (var 0,8 s, `TURN_PREROLL_MS`). Ringen är fortfarande den längre. Obevisat: att det också tar bort skräpet först i turen ("74 P3", "Tandläkarmottagning"); det mäts efter deploy med `📏`-raden och `tur_ljud`.

## 0.29.4 (fork)

- **Snabblagning efter provet 2026-10-11** (raawr D-97). (1) "Stäng av radion" körde `HassTurnOff` på högtalaren, HA svarade `failed` och radion spelade vidare. Ett `HassTurnOff` mot `media_player` blir nu `HassMediaPause`, och beskrivningen av pausverktyget säger att det gäller för "stäng av/stoppa". (2) Ett HA-svar med `failed` som inte är tomt når modellen som tydligt fel ("Det gick INTE: …", eller "Delvis: …"), så Björn säger ifrån i stället för att kvittera. (3) Journalen får en rad `💬 Björn sa: …` med modellens egna ord per tur (Gemini). (4) Persona-tillägget säger: klockan ovan gäller när samtalet öppnades, kör `GetDateTime` först för "vad är klockan nu", och gissa aldrig tider för matcher, resultat, öppettider eller priser: sök först. Googles egen sökning (`google_search`) var redan på i Live-sessionen på 3.x-modellerna; ändringen är regeln om att söka (texten säger "webbsökningen", eftersom den gäller alla motorer).

## 0.29.3 (fork)

- **Varje turs ljud kan sparas som egen fil** (raawr D-97, för att jämföra Google, xAI och bana 0 på riktigt tal). Av som standard. `TUR_LJUD_SPARA=1` slår på; `TUR_LJUD_KATALOG` (standard `/data/tur_ljud`) och `TUR_LJUD_MAX` (standard 40 filer) styr var och hur mycket. Är katalogen full sparas inget mer och journalen säger det en gång. Filerna är bana 0:s kopia av turen (16 kHz mono) och innehåller röst: slå av flaggan och radera katalogen efter jämförelsen.

## 0.29.2 (fork)

- **Mätrad för turer utan väckning** (raawr D-97). När en tur startar loggas `📏 turn start level X dBFS, Y s since idle, Z s since wake`. Inget beteende ändras. Siffrorna ska visa var gränsen går mellan en följdfråga och radion i rummet.

## 0.29.1 (fork)

- **Morgonfrågan för sparade sena svar** (raawr US-051 AC-6). Svar som inte frågades för att det var natt (00-07) eller ett samtal pågick sparas i minnet. Efter det första modellsvaret vid eller efter 07:00 frågar Björn en gång per rum och dag "Ska jag ge dig det jag sparat?"; ja läser högst tre, det äldsta först, och säger "Det finns fler." om fler finns; nej och tystnad lämnar dem. Samma lokala ja/nej som 0.29.0, ingen molnsession för frågan. Ett snabbspårssvar (tänd/släck) räknas inte som svar: frågan väntar på nästa modellsvar. Sparade notiser ligger bara i minnet (en omstart tappar dem; svaret ligger kvar hos Core).

## 0.29.0 (fork)

- **Ett sent svar frågas först, och läses bara efter ja** (raawr US-051, D-17). Comms postar `POST /sent-svar` (rum, result_id, agent, amne; aldrig texten) på websocket-porten, bara från loopback. Agenten svarar `{"fragar": true|false}` direkt och säger i så fall "<agent> har mer om <ämne>. Vill du höra det?" i motorns röst. Mikrofonen tas från motorn medan frågan lever (`fraga_tap` i serializern), så ingen molnsession öppnas för frågan eller för ja/nej (AC-5); orden hörs av bana 0:s lokala tal-till-text och läses mot två fasta listor (ja: ja, ja tack, kör, gärna, okej, visst, läs, berätta; nej: nej, nej tack, inte nu, senare, strunta i det). Ja hämtar texten hos Comms (`POST /kanal/rost/<rum>/svar`) och läser den; nej, annat och tystnad läser inget. Inget frågas 00:00-07:00 svensk tid eller mitt i ett samtal (`fragar:false`, sparas för morgonfrågan). `SENT_SVAR_NU=HH:MM` fejkar klockan i självprovet. Morgonfrågan (AC-6) och röst-ID (AC-4) är inte byggda.

## 0.28.7 (fork)

- **"Köket" betyder alltid Kök Voice (Voice PE med aux) för radio och musik** (Henrik 2026-10-10). "Kök Voice" och "Kök Sonos" fick samma poäng för "köket", så radion gick till den som kom först i listan;
  Sonos har varken mikrofonens echoreferens eller duckningen. Nu vinner Voice PE vid lika poäng (`valj_spelare`), och Sonos bara när han säger Sonos ("köket sonos"). Gäller varje rum med både en Voice PE och en annan högtalare.

## 0.28.6 (fork)

- **Köket: rumsljud får aldrig kapa Björns svar, men Henrik kan svara medan han pratar** (Henrik 2026-10-10). Tal som börjar medan ett svar spelas blir en KANDIDAT (`app/avbrott.py`) och
  blir ett avbrott först när det varat `AVBROTT_MIN_MS` (500) och har nivån `AVBROTT_MIN_DBFS` (−40 dBFS RMS över talet); annars kastas det (ingen tur, inget avbrott) och Björn pratar vidare.
  Varje beslut är en journalrad med längd och nivå (`🛡️ avbrott godkänt/avfärdat: … ms tal, … dBFS`), så tröskeln kan trimmas ur riktiga rum. `AVBROTT_TROSKEL=0` ger gamla beteendet. Gäller OpenAI Live.
- **Loggrader som saknades:** `🔚 Live-svar slut` (sekunder ljud, tid sedan första ljudet), `🛑 InterruptionFrame` (var i svaret ett avbrott kom) och `🔎 Live-händelse` för varje händelse som säger att ett svar är klart, avbrutet eller trunkerat.
- **Följdfönstret öppnas tidigare:** `FOLLOW_UP_OPEN_DELAY_MS` standard 700 → 350, och tystnaden innan fasen blir idle är 0,6 s för Gemini i stället för 1,5 s (`idle_debounce_for`; OpenAI Live behåller 1,5 s, den stannar mitt i svaret). Ett uttryckligt `PHASE_IDLE_DEBOUNCE_MS` / `FOLLOW_UP_OPEN_DELAY_MS` i miljön gäller fortfarande, så **Rolle måste ta bort de två raderna ur /etc/raawr-rostagent.env** (1500 / 700) för att standardvärdena ska slå igenom.

## 0.28.5 (fork)

- **OpenAI Live: svaret hackade i köket, nu hålls första ljudet tills 0,7 s finns** (kökets journal 2026-10-10, Henrik: "den är snabb, men det hackar rejält, svaren kommer tillbaka i omgångar").
  Mätt med ankomsttider vid enheten (attrappen, sidoinstans på core): Live skickar 0,2–0,4 s ljud, tystnar 0,36–0,8 s och strömmar sedan i uppspelningstakt, så enheten gick torr
  direkt efter första ordet (min_lead −0,27…−0,81 s, ett stopp per svar; Gemini 0 stopp). Att behålla tystnaden mitt i svaret hjälpte inte (provat och förkastat: stoppet är inget ljud, det är
  ingen data alls). Nu hålls det första ljudet till 0,7 s finns eller 1,2 s gått (`OPENAI_LIVE_PREBUF_S`, `OPENAI_LIVE_PREBUF_MAX_S`; 0 = som förut). Efter åtgärden: 0 stopp över 0,3 s,
  min_lead −0,008…0,0 s på fyra svar. Priset: första ljud ca 1 s senare (kökets 1,7 s blir ca 2,7 s, Gemini ligger på 2,9 s).

## 0.28.4 (fork)

- **Björns text hämtas ur Core genom comms i stället för ur en miljöfil (raawr US-056, D-20).** När en session byggs frågar agenten
  `<rummets comms-adress>/bjorn` (kontorets nyckel, `BJORN_KARNA_URL` åsidosätter) om själen plus högtalartillägget. Reservkedja: hämtad text
  (minne en minut), senast hämtade (`minne`), `INSTRUCTIONS` ur miljön (`miljö`), sist en inbyggd minimal själ (`inbyggd`); loggen säger
  `📜 Björn-kärnan: källa=...`. Hämtningen håller aldrig ett sessionsbygge längre än 2,5 s och en död comms frågas inte om igen på 10 s.
  Språklåset (`SPRAKLAS`) sitter sist som förut. `INSTRUCTIONS` kan tas bort ur miljöfilen när Comms-dörren och Core-tillägget är utlagda.
  Kräver raawr-ändringarna (Comms-dörr `/kanal/rost/kontoret/bjorn`, Core-tillägget `rost`); innan dess faller agenten tyst tillbaka på miljön.

Rättat efter korsgranskning (A): Comms reservtext (`halsa: "reserv"`, Core nere) räknas som misslyckad hämtning så miljön/minnet behålls; en text under 200 tecken räknas inte som själ; `BJORN_KARNA=av` hoppar över hämtningen; `TIMEOUT_S` 1,5 s.

## 0.28.3 (fork)

- **Turen kan sluta vid föravslutet när snabbspåret redan hört ett helt kommando** (raawr US-047, `TIDIGT_SLUT=1`, av som standard, bara Gemini).
  Efter 500 ms tystnad (`preend`) körs den tidiga tal-till-texten redan. Är texten en vanlig lampordning eller en klockfråga med minst två ord
  som inte slutar på ett ord som ber om mer ("och", "i", "på" ...), avslutas turen direkt i stället för att vänta ut de sista 300 ms.
  Talar han igen (föravslutsdetektorn hör tal) klipps inget. Detektorns eget slut efteråt sväljs, så turen avgörs en gång.
  Tidsraden räknar talets slut från föravslutet. OpenAI Live (köket) rörs inte.

## 0.28.1 (fork)

- **Tidsraden `⏱ tider` skrivs också för OpenAI Live.** Jämförelsedygnet (köket på openai_live, kontoret på Gemini, Henrik 2026-10-10) läser tiderna
  ur den raden, men Live-motorn skrev ingen: den startar nu raden vid lokalt turslut och märker `modell` när första riktiga ljudet kommer
  (`enhet` märks av websocket-handlern som förut). Samma form som Gemini: `⏱ tider koket turslut=.. [stt=.. comms=.. verktyg=..] modell=.. enhet=..`.

## 0.28.0 (fork)

- **0.28.0, verktyg provade 2026-10-10 (nyckeln på All).** Live-delegation med `gpt-5.4-mini` och `gpt-6-luna` som backend fungerar hela vägen:
  Live delegerar, vi kör verktyget (provat: `homeassistant__GetLiveContext` 236 ms ok), Live talar svaret. Två gränser hos OpenAI funna och
  hanterade: indata får inte komma snabbare än 1,2 x realtid eller i skurar över 5 s (`input_audio_rate_limit_exceeded`), så det hållna turljudet släpps
  nu i 4 s-skur + 1,15 x realtid (`OPENAI_LIVE_BURST_S`, `OPENAI_LIVE_MAX_X`); och projektets gpt-live-1-gräns är 600 tokens/min där EN
  sessionsstart begär allt (`rate_limit_exceeded`, "try again in 900ms"), så väckningen väntar den tiden en gång och försöker igen.
  Sätt `OPENAI_LIVE_DELEGATION_MODEL` (t.ex. gpt-5.4-mini). Mätt via hela add-onet på core: första ljud 0,9 s efter lokalt turslut, svenska hela vägen.

- **0.28.0, prov mot riktiga OpenAI Live 2026-10-10 (nyckeln i rostagentens SOPS).** Session-formatet rättat efter riktiga endpointen:
  EN `audio.format` (24 kHz) för båda håll, `audio.input` nekas; tystnad är exakta nollor i en jämn ström och räknas inte som svar;
  väckning/sömn går nu genom `SovlageMixin`-krokarna och tillståndsmaskinen (inga egna `vakna`/`sova`); `report_failure` väntas (async).
  Verktyg: `delegation.type=responses` kräver nyckelns behörighet `api.responses.write` (saknas: `missing_scope`, ingen session);
  `OPENAI_LIVE_DELEGATION=client` startar utan den men har ingen funktionsanrop. NY: `DEVICE_PROVIDERS="koket=openai_live,kontoret=gemini"`
  låser en enhet vid en motor (jämförelsedygn); en motor som fallerar lämnar enheten åt routerns motor i `DEVICE_PROVIDER_PAUS_MIN` (10) min.
  Mätt mot riktiga Live (syntetisk svensk röst, direkt mot endpointen): rätt hörd svenska, första ljud 0,9-1,0 s efter talets slut.

- **OpenAI Live som röstmotor, `openai_live`** (raawr US-025). Henriks
  OpenAI-nyckel tillåter bara Live (gpt-live-1); Realtime-motorn `openai`
  lämnas orörd. Eget protokoll (`app/providers/openai_live.py`):
  `session.start` -> `session.started`, ljud in som
  `session.input_audio.append` (16 kHz), ut som `session.output_audio.delta`
  (24 kHz), verktyg via Responses-delegering (`response.event` ->
  `function_call_output` + `response.create`), `session.close`. Inget
  "ljud klart"-event: `OPENAI_LIVE_REPLY_GAP_MS` (1500) utan ljud avslutar
  svaret.
- Som de andra: sover till väckningen, snabbvägen först (turens ljud hålls
  tills bana 0 avgjort, Live hör aldrig en träff), lokala turer, maxtid och
  dagstak. Henriks två regler för motorn: **en OpenAI-session åt gången** i
  hela processen, och **eget tak `OPENAI_MAX_MINUTER_PER_DAG` (6)** i
  `/data/moln_minuter_openai.json`. En nekad väckning flyttar högtalaren till
  nästa motor i kedjan (han väcker igen).
- `VOICE_PROVIDERS=gemini,openai_live,xai`. Inställningar: `OPENAI_LIVE_MODEL`
  (gpt-live-1), `OPENAI_LIVE_VOICE` (marin), `OPENAI_LIVE_DELEGATION_MODEL`,
  `OPENAI_LIVE_TAIL_MS`. Protokollfält som inte är provade live står på ett
  ställe: `openai_live.session_config` (ponytail).
## 0.27.42 (fork)

- **Comms egna MCP-dörr som andra verktygskälla, hämtad dynamiskt (raawr US-021, spår A:s önskemål).** Agenten har ingen lista över
  dörrens verktyg: `tools/list` frågas av `/kanal/rost/kontoret/mcp` (bredvid `HA_API_URL`, inte under; `/api/mcp` går till HA) varje gång en
  session byggs eller vid väckningens omhämtning, så ett verktyg som läggs till eller ändras i comms syns vid nästa väckning utan att agenten
  ändras. Verktygen läggs till HA:s i samma schema och varje verktyg anropas på den klient det kom från. Svarar dörren inte försvinner bara
  dess verktyg den gången (HA:s påverkas inte); samma namn i båda ger HA:s. Av som standard: sätt `COMMS_MCP_DORR=1` (adress ur `HA_API_URL`,
  eller `COMMS_MCP_URL`). Mot riktiga dörren (bara läsning): 8 verktyg listas (lage_drift, fraga_huset, lista_svar, hamta_svar, folj_upp,
  mac_byggen, svara_bygget, kasta_in). `MCP_TOOL_ALLOWLIST`, om satt, gäller även dessa.

## 0.27.41 (fork)

- **Sena svar från `ask_openclaw` ropas ut i rummet där frågan ställdes.** Verktyget skickade processens `INSTANCE_NAME` ("kontor" på core) som
  `room`, så ett sent svar på en fråga i köket kunde ropas ut i kontoret. Det skickar nu enhetens eget id (`koket`/`kontoret`,
  `device_registry.rum_ur_enhet`, samma hjälpare som `play_media`); `INSTANCE_NAME` är reserv när id:t är en IP eller `unknown`.
  Adaptern (raawr `Backend/Services/voice-realtime/adapter.mjs`) måste ta emot rummet och skicka `device_id` till `/announce`: egen PR i raawr.

## 0.27.40 (fork)

- **Brus som lät som tal startar ingen musik (kontoret 2026-10-09 21:17).** Musikbrus hördes som "Du kan välja med tips?" (lokal STT) och
  "Play Game of Tips" (Gemini), och en podcast startade. Tre skydd: (1) bana 0 släpper kända Whisper-påhitt ("Patreon", "tack för att ni
  tittade", "amara.org", "thanks for watching") och texter under två bokstäver (`bana0.brus`): ingen fråga till Comms, tur går till
  modellen med ljudet som vid tom text. (2) `play_media` spelar inget om Geminis egen avskrift av turen mest är ett annat språk än svenska
  och börjar på det (`sprakkoll.mest_annat`, `GeminiLive.tur_text`), eller om lokala STT inte hörde något av frågans längre ord. (3) Då
  säger verktyget åt modellen att fråga en gång vad användaren vill höra; säger hen samma sak igen i en ny tur inom 90 s spelas det (modellen kan inte ringa om i samma tur). Ej byggt:
  uttryckligt villkor för följdfönstret (spärren gäller alla turer).

## 0.27.39 (fork)

- **play_media utan nämnt rum spelar i rummet där någon frågade (köksprovet 2026-10-09 21:17).** Köket bad om musik och det spelade i
  kontoret: standardspelaren var `INSTANCE_NAME`, ett värde för hela processen ("kontor" på core), fast en process nu bär flera enheter.
  Handlern får nu enhetens eget id (`koket`/`kontoret`) vid registrering; `INSTANCE_NAME` är bara reserv utan enhet eller när id:t är en IP/`unknown` (ingen `?device_id=`). Kvar, ej lagat här: `ask_openclaw` (`openclaw_tool.py`) skickar också processens `INSTANCE_NAME` som rum, så ett sent svar från köket annonseras i kontoret. Test per rum i
  `tests/test_play_media.py`.
## 0.27.38 (fork)

- **Loggar när Geminis egen transkription börjar på ett annat språk än svenska (köksprovet 2026-10-09).** När svaret började på
  italienska fanns inget spår av vad Gemini hört. Nu loggas, en gång per tur och som varning, de första orden (högst fyra) om något
  av de två första orden hör till italienska, franska, engelska, tyska eller spanska vanliga ord och inte är svenskt
  (`app/sprakkoll.py`, `_handle_msg_input_transcription`). Ingen ljudfil sparas och inget annat än de orden loggas; turen påverkas inte.

## 0.27.37 (fork)

- **Svaret är alltid på svenska, oavsett vad ljudet före talet låter som (köksprovet 2026-10-09).** Musik och rumsbrus före väckordet
  hördes som italienska ("Ronzio a terra") och svaret började på italienska (tidigare också "Bonjour du"). Instruktionen får ett fast
  språklås sist före minnet och tidsblocket (`SPRAKLAS` i `main.py`): svara på svenska från första ordet, brus och andra språk ändrar
  aldrig svarsspråket. Mätt mot riktiga Live med italienskt brus före svenskt tal: 4 av 4 svenska med och utan låset, så felet gick inte att
  återskapa syntetiskt; låset är försiktighetsåtgärd, inte bevisad bot. Förrullen (0,8 s) är orörd: ingen åtgärd utan en reproduktion.

## 0.27.36 (fork)

- **Mätraden för personligt svar efter en bana 0-träff (raawr US-047, BANA0_PING).** `tools/matvarden.py` skriver per rum
  `personligt svar P50/P95 … (n=…)` (tider-raden har `modell=`) och `kort svar` (bara `enhet=`, det inbyggda "Klart.") för turer
  där en `bana0: hit`-rad kom efter rummets förra tider-rad; `--json` har `traff_personligt` och `traff_kort`. Inga ord ur
  samtalet läses. Byggt av G (Grok), granskat och körts av C: på kärnans journal efter provet ger raden 2863/2889 ms (n=2),
  samma som provets egna värden. Ingen ändring av agenten.

## 0.27.35 (fork)

- **Parallellt spår: snabbspåret och Live lyssnar båda, bakom BANA0_PING (av) (raawr US-047, Henriks idé).** Vid en träff i
  bana 0 (lampor) utför Home Assistant det direkt som förut, men Gemini-modellen får nu också ljudet, efter en rad
  "[huset] Snabbspåret hörde "…" och gjorde det redan … Gör inte om det med verktygen. Hör du något annat, rätta det."
  (`bana0.ping_text`, `ResilientGeminiLiveService.ping_and_answer`). Björn svarar personligt i stället för det inbyggda
  "Klart.". Mätt mot riktiga Live (`tools/live_ping_prov.py`, syntetisk röst): utan rad anropar modellen verktyget igen;
  med raden gör den det inte (första ljud ca 1,6 s efter sista indata) och när snabbspåret hörde fel rättar den själv
  från ljudet (ca 2,4 s). Spärr i verktygslagret (`Atgardslogg`, `skydda_verktyg`): samma verb och plats inom 10 s som en
  träff = "redan gjort", annat verb går igenom. Bara Gemini; OpenAI/xAI oförändrade.

## 0.27.34 (fork)

- **En omkoppling dödar inte längre en tur som modellen inte börjat svara på (köksprovet 2026-10-09).** När enhetens
  socket föll mitt i en tur (väckning under THINKING) revs pipelinen och turen försvann. Nu sparar tjänsten ljudet
  för den tur modellen senast fick (`answer_turn`) tills första ljud, ett verktygsanrop eller turslut; om länken
  tappas hamnar det i handlerns `_raddade`, och enhetens nästa anslutning svarar på det (`_lamna_raddad_tur`):
  inom 20 s, bara om ingen börjat tala igen, och aldrig en tur som modellen agerat på (annars skulle ett verktyg
  köras två gånger). Gäller ljudläget (inte LOCAL_TEXT_TO_MODEL, som är av).

## 0.27.33 (fork)

- **En tur till en sovande motor väcker den först (köksprovet 2026-10-09).** 16:51:16 kom en följdfråga efter att molnet
  sövts; `answer_turn_text` (och `answer_turn`) skickade till ingen session, `_send_activity` returnerar tyst, och enheten
  väntade 15 s på ett svar som inte kunde komma. Nu väcks en sovande motor först (`vakna`); vill den inte vakna loggas det
  som varning i stället för att turen försvinner tyst.

## 0.27.32 (fork)

- **En andra väckning kapar inte längre ett svar som pågår (köksprovet 2026-10-09).** Timern "wake without
  speech for 5s" räknade en väckning under följdfönstret som en väckning ingen svarat på, fast frågan före den
  redan tänktes på eller talades: 16:50:59 stängde den molnet 0,6 s in i svaret. `_vakning_utan_tal` gäller nu
  inte i faserna thinking och replying (en svarsfas som verkligen fastnar är tänkvaktens och tystnadsregelns jobb).
  Gäller ljudläget lika mycket som textläget.

## 0.27.31 (fork)

- **Core-svar läses av Live själv, inte av en annan röst (raawr US-047 AC-4). Flaggan CORE_STREAM_TALA är fortfarande av.**
  Henrik 2026-10-09: TTS-rösten för Core-svar är nej ("låter som en annan röst"). `fraga_core`
  matar nu varje mening som text in i den anslutna Live-sessionen (`LiveMatare`, `mata_text`),
  inramad som ett citat att läsa upp, aldrig som en order (Core-text kan citera webben). Nästa
  mening går in `CORE_STROM_FORSKOTT_S` (1,5 s, en gissning) före slutet på den förra, räknat på 12
  tecken/s; ger en mening inget ljud inom 8 s går resten som ett block. Loggraden `core-ström`
  har nu `live_s` (sekunder tal per svar, för dygnstaket), `matade`, `turer_klara`, `mojligen_klippt`.
  Försprånget är kumulativt (B på #38): varje mening går in 1,5 s före den förras verkliga slut, och
  bara första meningen väntar på ljud. `"""` i Cores text byts ut så citatet inte kan stängas.
  Borta: TTS-vägen (`_say_mening`, 6,5 s-bromsen, `levande`/`cache` i `_ack_clip`/`gemini_tts`/`xai_tts`,
  `READ_ALOUD_LEVANDE`) och #37:s nivåkod (`niva_db`, `normalisera`), samt `tools/rostjamforelse.py`.
  Nytt: `tools/live_strom_prov.py` (mätkörning mot Live: seriell/tidsatt/kö/hel/försprång).

## 0.27.30 (fork)

- **Nivån på strömmade Core-meningar jämnas per mening, inte med fast dämpning (raawr US-047 AC-4).**
  0.27.29 sänkte alla 2,3 dB efter fem meningar; tre till visade att en renderad mening varierar
  ±2 dB från nästa, så det blev 2,3 dB för tyst (-23,3 mot Live -21,0). Nu mäts varje mening
  (`early_ack.niva_db`) och sätts på Live-rösten medelnivå (`STROM_MAL_DB` -21,1 dB, högst ±6 dB;
  vid full skala klipps toppen hårt, den viks inte). Ett klipp kortare än en ram lämnas orört. xAI
  normaliseras inte än (flaggan är av). `daempa` är borta.

## 0.27.29 (fork)

- **Nivån på strömmade Core-meningar (raawr US-047 AC-4).** TTS mättes 2,3 dB högre än Live-rösten
  (`tools/rostjamforelse.py`); meningar som `fraga_core` talar på Gemini dämpas nu 2,3 dB
  (`early_ack.daempa`, `STROM_NIVA_DB`), så rösten inte hoppar i volym mellan Live-svar och mening.

## 0.27.28 (fork)

- **Core-svar som talas medan de skrivs, bakom flagga (raawr US-047 AC-4, US-036).** Med
  `CORE_STREAM_TALA=1` (AV) och `CORE_STROM_URL` får modellen verktyget `fraga_core`: det svarar
  direkt 'kollar' och i bakgrunden läser röstagenten Cores SSE-ström (`start`, `token`, `deferred`,
  `done`, `error`), delar tokens i meningar och talar varje mening så fort den är hel, i samtalets
  röst (Gemini: Charon, xAI: xAI-rösten), via samma väg som övriga utsändningar. `deferred` talar Cores
  egen rad; fel talar en fast rad, aldrig felets text. Meningarna läses med en livligare ram (`READ_ALOUD_LEVANDE`) än klippen: mätt mot Live-rösten
  (`tools/rostjamforelse.py`) ligger tonhöjd, ljushet och längd då närmare. Meningarna sparas aldrig på disk (nytt
  `cache=False` i TTS-vägen): ett Core-svar kan vara privat. Vägen är Comms `POST <rum>/fraga` (US-050, spår A: rummets egen nyckel,
  `Accept: text/event-stream`, kropp `{"text": …}` högst 2000 tecken); adressen härleds ur `HA_API_URL`,
  `CORE_STROM_URL` åsidosätter. Köket har ingen egen nyckel än och är utanför. Ett svar talas högst 8 meningar och säger då att det finns mer, en i taget, Geminis TTS-spärr (6,5 s mellan
  renderingar) hålls, och ett svar stoppas när han börjar tala eller länken försvinner. Cores `done`
  bär hela texten: den talas bara om inget annat sagts. Modellen hör inte
  det som talas (det går vid sidan av dess tur).

## 0.27.27 (fork)

- **Tidsraden per tur (raawr US-047 AC-1).** En rad i journalen per tur, utan ord:
  `⏱ tider kontoret turslut=812 stt=1002 comms=1150 verktyg=1900 modell=2310 enhet=2360`, i
  millisekunder efter talets slut (turslutet minus den tystnad den lokala detektorn väntar på,
  en uppskattning på några tiotals ms). Stegen som inte hände en tur utelämnas; raden skrivs när
  första ljudet når enheten (`enhet`), en tur utan ljud skrivs vid nästa tur med `utan_ljud`.
  `tools/matvarden.py` ger P50/P95 efter talets slut per rum, turer med verktyg för sig.
  `enhet` märks också när bana 0:s egna klipp och kvittona går direkt till enheten
  (`send_bytes_to`), så bana 0-turer får sin rad och inte `utan_ljud` (B:s granskning av #34).
  Ingen ändring av vad agenten gör.

## 0.27.25 (fork)

- **Experiment: lokalt hörd text till modellen (raawr US-032, Hemma ja 2026-10-09).** Live
  köket 2026-10-09: Gemini hörde 'Kan du ta den på toalettet kanske?' där lokala STT, på samma
  ljud, fick 'Kan du tända kontoret kanske?'. Med `LOCAL_TEXT_TO_MODEL=1` (AV som standard) får
  Gemini vid en miss de lokalt hörda orden i stället för det hållna ljudet (`answer_turn_text`:
  bara texten, utan activityStart/End; med dem stänger Google socketen, 1007). Svaret är fortfarande tal
  (attrappen på core 2026-10-09: 197 760 B ljud, första ljud 2,3 s efter frågan). Kvar att mäta: att enheten
  faktiskt talar (en extra not i samma tur gjorde den tyst tidigare), och att texten inte tappar
  vad rösten bar. Attrappen först, sedan Henrik i köket.
  **Så sätts flaggan:** på core är det raden `LOCAL_TEXT_TO_MODEL="1"` i `/etc/raawr-rostagent.env`
  (systemd-miljön för `raawr-rostagent`), sedan `systemctl restart raawr-rostagent`; ta bort raden för
  att stänga av. Det är en miljövariabel, inget tilläggsalternativ. **Öppet:** mät att lokala STT inte
  hör fel åt andra hållet (en text modellen annars hade hört rätt) innan fler rum slås på.
## 0.27.24 (fork)

- **En fråga över Björns eget svar tappar inte början (raawr US-032, live 2026-10-09 12:09).**
  Bana 0:s ljud samlades inte medan fasen sade `replying`, så en väckning eller ett tal över svaret
  gav en tur där de första 0,8 s saknades (varningen 'a question may have been cut', 5,63 mot
  6,12 s). Nu samlas turen från väckningen eller från att lokala VAD säger tal, och förrullen
  hämtas ur en löpande 0,8 s-buffert. Följden: bana 0 och motorn hör samma början. Björns eget
  svar samlas fortfarande inte i turen innan tal börjar.

## 0.27.23 (fork)

- **Förinspelade klocksvar är av (raawr US-032, PO-beslut Henrik 2026-10-09).** Klippen gav snabbt
  men statiskt svar och lät inte som Björn: hellre ett personligt svar efter ~1 s än ett statiskt
  efter 0,5 s. En klockfråga går nu till modellen som alla andra, och inga klockklipp renderas
  (ingen kvot går åt). Koden finns kvar bakom `KLOCKA_KLIPP=1` (av som standard).

## 0.27.22 (fork)

- **Klockmatchningen släpper inte igenom order (A:s granskning av #29).** Den garblade klassen
  ('Böda klockan') tas bara som exakt två ord eller efter ett frågeord (vad, var, va, hur, vet, har,
  säg, kan, e, är). Allt annat kort som slutar på 'klockan' går till modellen, och fler orderord
  stoppar matchen (larma, vakna, ring, påminna, starta, stoppa, pausa, spela, kör, öppna, stäng,
  byt, ändra, alarm, väckarklocka, schemalägg, mig, oss). 26 av 42 order A prövade fångades förut;
  de ligger nu som test.

## 0.27.21 (fork)

- **Bredare klockmatchning och längre tal-till-text-tid (raawr US-032).** Live 2026-10-09 hörde
  tal-till-text Henrik som 'Böda klockan', 'Vad är det klockan?', 'Hur mycket klockan.' och
  matcharen tog ingen av dem. Nu är en kort fras (2-5 ord) som SLUTAR på 'klockan' en klockfråga,
  om den inte börjar som en order (ställ, väck, sätt, timer, tänd ...) eller har en tid efter sig.
  `BANA0_STT_TIMEOUT_MS` går från 600 till 1500 ms: en tal-till-text som tog längre än 0,6 s gav
  TimeoutError och turen gick till modellen. Normalt tar den 0,13 s, så ingen väntar längre.

## 0.27.20 (fork)

- **Mätkommandot (raawr US-032 AC-10).** `journalctl -u raawr-rostagent --since today | python tools/matvarden.py`
  ger per rum: sessioner, uppkopplade minuter, tid till första ljud och reflex som P50/P95,
  avbrott (SPEAKING -> LISTENING) och stängningsorsaker. Läser bara tider, lägen och kodens egna
  orsaker; det som sades (raderna `heard`/`hit`) kopieras aldrig. Reflexen är tiden från
  THINKING till bana 0:s träff, och en träffrad har inget rum, så den läggs på rummet som
  senast blev THINKING.

## 0.27.19 (fork)

- **Bana 0:s ljudlängd mot motorns, och fasen vid turstart (raawr US-032, A:s granskning av #27).**
  Bana 0:s ljud hoppar över fasen replying men motorn får det ändå, så en fråga kan stympas.
  Vid turslut loggas bana 0:s ljudlängd och motorns varje tur; varning bara när motorn håller
  mer än 0,3 s mer än bana 0 (normalt har bana 0 ~0,3 s mer, förrullen 0,8 mot 0,5 s). Emitterns
  fas (den som styr hoppet i serialiseraren) loggas vid turstart. Kontrolluppgiftens referens hålls nu (kan inte städas
  bort mitt i).

## 0.27.18 (fork)

- **Kontroll av den tidiga tal-till-texten (raawr US-032).** Live 2026-10-08 missade två
  klockfrågor efter att den tidiga texten använts ('Så kan.'). Syntetisk röst går igenom, så
  felet är inte bevisat. Nu läses hela turen en gång till i bakgrunden (inte på den kritiska
  vägen) och loggen säger `early speech-to-text 'X' differs from the whole turn 'Y'` om de skiljer.
  `SPEC_STT_KONTROLL=0` stänger av. Ingen ändring av vad bana 0 svarar.

## 0.27.17 (fork)

- **Egna repliker i flera varianter (raawr US-032 AC-8).** Bekräftelsen efter en order
  ("Klart.") har fem varianter och "når inte nätet" tre. De dras ur en blandad påse: fem i rad
  ger minst tre olika, aldrig samma två gånger efter varandra. En variant vars klipp saknas ger
  nästa, aldrig tystnad. Uppvärmningen renderar alla varianter (`LOKALA_REPLIKER`).
  "Kan inte nu" finns inte som replik i koden och är inte med.

## 0.27.16 (fork)

- **OpenAI är ingen klippkälla (raawr US-032).** OpenAI-nyckeln är avsiktligt Live-only och dess
  TTS svarar 403. Uppvärmningen renderar bara med Gemini och xAI, och en OpenAI-session får
  Geminis klipp (xAI:s om Gemini-nyckel saknas).

## 0.27.15 (fork)

- **Bana 0 loggar vad tal-till-text hörde (raawr US-032).** En rad `bana0: heard '...'` per tur,
  så att en klockfråga som missar går att förklara (live 2026-10-08 16:52 gick den till modellen
  utan att loggen visade texten).

## 0.27.14 (fork)

- **Klockklippens kvotdygn följer Google (raawr US-032).** Googles TTS-kvot nollas vid
  midnatt i Pacific (07:00 UTC på sommaren, 08:00 på vintern), inte 00:00 UTC. Uppvärmningen
  väntar nu till fem minuter efter det, och dagens renderingar räknas från den tidpunkten.

## 0.27.13 (fork)

- **Bana 0:s tal-till-text startar tidigt (raawr US-032).** Turdetektorn får en andra,
  kortare tystnadsgräns (`LOCAL_PRE_END_MS`, 500 ms, 0 = av) och säger då `preend`,
  300 ms innan turen är slut. Bana 0 hämtar texten på det som sagts hittills medan
  de sista 300 ms väntas ut, och använder den vid det riktiga slutet om det bara blivit
  tystare (högst 0,6 s ljud till). Fortsätter talet kastas texten. Hit-eller-miss-beslutet
  och comms-anropet sker som förut efter slutet, så **modellen hör fortfarande aldrig en
  träff**. Mätt på core: den lokala turen efter tystnaden går från 0,3 s till 0,02 s, och
  svaret kommer efter 3,3 s (ren fråga), 3,6 s (hemverktyg) och 4,1 s (Core), mot 3,9, 4,4
  och 4,5 s med bara 800 ms utan tidig text. Gäller Gemini med bana 0.

## 0.27.12 (fork)

- **"Ett ögonblick" är borta (Henrik 2026-10-07: det blir konstigt).** Ledtråden till
  modellen i de långsamma verktygens beskrivningar, timerklippen och deras
  uppvärmning är av. `EARLY_ACK=1` slår på dem igen. Bana 0:s egna repliker
  ("Klart.", "Jag når inte nätet") är kvar. Mätt på core 2026-10-08: tidigare var
  första ljudet nästan alltid utfyllnaden, 3,5-3,8 s efter frågans slut, medan
  svaret kom efter 4,2-5,2 s.
- **Turslutet väntar 800 ms på tystnad i stället för 1200 ms** (Gemini och xAI).
  `tools/paustest.py` visar att en paus på upp till 0,8 s mitt i en mening går igenom, att
  0,7 s kapar vid 0,8 s paus, och att en paus på 1,0 s kapar redan vid 900 ms. Genom hela
  kedjan på core: pauserna 0,6 s och 0,8 s blir en tur, 1,0 s blir två. Vägen tillbaka är
  `GEMINI_TURN_SILENCE_MS=1200` och `XAI_TURN_SILENCE_MS=1200`. På core står
  1200 i `/etc/raawr-rostagent.env`, och måste ändras där för att 800 ska gälla.
- **`tools/forstaljud.py` delar upp väntan** från frågans slut till första ljud i
  tystnad, lokal tur, motor och verktyg, och uppspelning.

## 0.27.11 (fork)

- **Attrappens molnminuter har en egen bok och ett eget tak (Henrik 2026-10-08).**
  Provens falska högtalare (`device_id` som börjar på `attrapp`) räknas i
  `moln_minuter_prov.json` mot `MOLN_MAX_MINUTER_PROV_PER_DAG` (30 min), och inte
  mot Henriks dygnstak på 60 min. Förra kvällen åt mina prov upp 58 av hans 60
  minuter. Öppna provsessioner räknas inte heller in i hans tak. En riktig högtalare
  kan inte kalla sig `attrapp`: comms stämplar `device_id` från rummet, och agenten
  lyssnar bara på loopback. `tools/minutkoll.py --typ prov` jämför provboken med
  journalens `[prov ...]`-rader.

## 0.27.10 (fork)

- **En tillståndsmaskin per session (raawr US-032 AC-9).** `app/session_state.py`
  har läget IDLE, WAKE, LISTENING, THINKING, SPEAKING och CLOSING och en tabell med
  utfall för varje par av läge och händelse. Den är den enda kod som skickar ett
  fasmeddelande till enheten och den enda som väcker eller söver molnmotorn.
  Varje byte blir en journalrad med orsak, `🧭 kontoret: SPEAKING -> CLOSING
  (close: quiet for 30s)`. En händelse som läget inte tillåter ändrar ingenting
  och skrivs som `rejected`. Väckningen vinner alltid över en nedstängning.
- **En tappad länk stänger kedjan.** Provet med låtsashögtalaren på core visade
  att kedjan stannade i THINKING när länken bröts. Nu stänger nedrivningen den
  (`close: link lost or displaced`) och kedjan slutar i IDLE.
- **En källtextsvakt** i `tests/test_session_state.py` faller om en annan modul
  anropar motorns `sova`/`vakna` eller enhetens `send_phase`. På 0.27.9 fångar den
  sju anrop i `websocket_handler.py` och `xai_realtime.py`.
- **`tools/kedjekoll.py`** läser journalen och kontrollerar att en enhets kedja är
  obruten, börjar och slutar i IDLE och saknar avvisade händelser.

## 0.27.9 (fork)

- **Klockan säger tiden som en svensk, i Björns ton (Henrik 2026-10-07).** Med
  tolvtimmars urtavla och närmaste fem minuter blir det "Hon är tjugo över fem."
  eller "Hon är kvart i sex.". Varannan gång följer en torr rad som passar tiden
  på dygnet, till exempel "Gå och lägg dig, för fan." på natten eller "Snart
  dags att käka." på kvällen. Varje klipp är en hel fras, eftersom Gemini TTS inte
  ger ljud för ett ensamt tal.
- **149 klipp, fördelade över dagarna.** Gemini TTS ger 100 anrop om dagen per
  projekt, och de levande svaren använder samma kvot. Uppvärmningen renderar
  därför högst 60 klipp om dagen och fortsätter nästa UTC-dygn. Den tar de
  närmaste timmarna först. Tre fel i rad betyder att kvoten eller motorn är
  slut, och då väntar den till i morgon i stället för att ge upp. Saknas ett
  klipp svarar modellen som förut. Dagens antal räknas från disken, så en
  omstart samma dag börjar inte om på 60. Varje motor värms parallellt med sin
  egen kvot. Hel timme heter "Hon är sex.", inte "prick", eftersom 17:58 avrundas.

## 0.27.8 (fork)

- **Dygnstaket tappar högst en minut, också vid kill -9 (raawr US-032 AC-4).**
  En öppen session bokfördes först när den slutade, så en agent som dödades
  tappade hela samtalet ur minutfilen. Sov-loopen bokför nu den öppna tiden var
  20:e sekund: två högtalare x (20 s + loopens 5 s) = högst 50 s förlorat vid
  kill -9. Taket räknar bara det som inte redan är bokfört, och maxtiden per
  samtal räknas fortfarande från uppkopplingen.
- **En trasig, tom eller borttagen minutfil ger aldrig ett lägre dagsvärde.**
  Filen skrivs två gånger, atomiskt, till `moln_minuter.json` och `.kopia`, och
  läses som det högsta av de två.
- **`tools/minutkoll.py` jämför minutfilen med journalen.** Varje uppkoppling och
  stängning loggas med `[moln <tagg>]`. Skriptet parar dem, låter en session som
  processen dog med sluta vid systemd:s rad och ger exit 1 vid mer än 1 minuts
  skillnad.

## 0.27.7 (fork)

- **Klockan svarar utan moln (raawr US-032 AC-7).** "Vad är klockan", "hur
  mycket är klockan", "hur dags är det" och några till känns igen i den lokala
  STT:ns text, före comms och modellen. Svaret är två förrenderade klipp i
  motorns röst ("Klockan är fjorton" + "och tjugotvå minuter"), 83 klipp i
  allt, på disk.
  Modellen hör aldrig frågan (Gemini: turen släpps). Fungerar utan internet.
  Väckningen kopplar fortfarande upp motorn som förut.
- **Klippen renderas i Googles takt.** Gemini TTS tillåter 10 anrop i minuten
  per projekt (429 efter 12 klipp 2026-10-07): ett nytt klipp väntar 8 s, ett
  klipp på disk väntar inte. Ett klipp som inte blir av hoppas över; tre i rad
  betyder att motorn är nere. Saknas ett klipp går klockfrågan till modellen.
  Minuten heter "och två minuter": för ett ensamt tal ("tjugo") eller "noll
  två" gav Gemini inget ljud.

## 0.27.6 (fork)

- **Ett avvisat Gemini-handtag ger ett nytt samtal i samma väckning.** Live
  2026-10-07 svarade Google på återupptagningen med 1011 på 0,9 s. Väckningen
  gick förlorad, högtalaren lyste rött och kopplade om sig. Nu startar agenten
  ett nytt samtal direkt, inom väckningens 5 s. Svarar Google inte alls (nätet)
  görs inget andra försök.
- **Ett följdfönster som stänger räknas inte som en väckning utan tal.**
  Fönstrets flush släckte flaggan för tal, så motorn somnade ~3 s senare
  ("wake without speech") i stället för efter 30 s tystnad. Tystnadsregeln
  gäller igen efter ett samtal.
- **En väckning som ger upp river sin halvfärdiga uppkoppling.** Förut kunde en
  handskakning som inte hann klart komma upp efteråt som en session utanför
  tio-minuterstaket och dygnstaket (G:s granskning av 0.27.6, fynd 1). Gäller
  också en långsam första uppkoppling, som redan fanns i 0.27.5.
- `tools/satellit_attrapp.py` och `tools/sessionsgranser.py`: en högtalare
  utan människa, och en domare som läser journalen per fall (raawr US-032 AC-3).

## 0.27.5 (fork)

- **Hämtningen av HA-verktyg vid väckning har ett eget tak på 1 s**
  (`MCP_TOOLS_WAKE_TIMEOUT_SECONDS`). En hängande HA höll förut molnuppkopplingen
  i upp till 5 s.
- **En lista som HA svarar med är sanningen, även när den är kortare.** En
  borttagen integration hämtas inte längre om vid varje väckning. Bara en
  misslyckad hämtning prövas igen.

## 0.27.4 (fork)

- **Home Assistant tillbaka: verktygen hämtas vid nästa väckning.** Förut
  stängdes högtalarens anslutning när HA kom tillbaka, och den nya anslutningen
  väckte molnet. Nu hämtas en misslyckad eller kort verktygslista om vid nästa
  väckning, innan molnmotorn kopplas upp. Högtalaren stannar uppkopplad.
- **Provet av reservmotorn fryser inte längre agenten.** Det gjorde ett
  blockerande anrop i upp till 2 s. Nu väntar det utan att stoppa allt annat.

## 0.27.3 (fork)

- **Dagssumman bär sitt datum.** När filen med dagens molnminuter saknades eller
  var trasig återanvändes senast kända summa, även om den lästes i går. Över
  midnatt kunde gårdagens minuter då räknas mot dagens tak. Nu gäller det
  sparade värdet bara samma dag som det lästes.

## 0.27.2 (fork)

- **En väckning utan tal kopplar ner molnmotorn.** Sovloopen kopplade bara ner
  i fasen idle, så en väckning där ingen talade kunde lämna motorn vaken till
  tiominuterstaket om fasen fastnade i lyssning eller svar. Nu sover den när
  väckningen är äldre än `VAKNA_TIMEOUT_S` och ingen riktig tur har börjat,
  oavsett fas. Öppen mikrofon räknas inte som tal, och ett samtal där någon
  talat bryts inte av taket.

## 0.27.1 (fork)

- **Dygnstaket bokförs när sessionen rivs.** Minuterna skrevs bara i `sova()`.
  HA-återvinning, en högtalare som ansluter igen och nedstängning gick via
  `_teardown` och räknades inte. Samma öppna tid bokförs en gång där, och en
  gång till från `sova()` lägger inte till något. Ledgern skrivs atomiskt.
  En trasig eller saknad fil efter ett känt värde nollar inte dagen. Taket
  räknar alla vakna motorer i processen, inte bara den egna.

## 0.27.0 (fork) - tidigare 0.25.8 + 0.25.7, ovanpå sovläget

- **Snabbvägen bekräftas av modellen, inte av HA:s torra röst.** Ägaren
  2026-10-03: "hellre tyst än den torra". Efter en träff talas HA:s svar inte
  längre; modellen får ett systemmeddelande om vad som gjorts och ombeds
  bekräfta med en mening, utan verktyg (`tool_choice: none`), så den inte kan
  göra ordern igen. Live 0.25.6 på xAI svarade modellen ändå efter träffen,
  ovanpå HA:s röst - två bekräftelser. Säger modellen inget på 2,5 s (utan
  internet) spelas "Klart." i motorns egen röst ur diskcachen; på Gemini, som
  aldrig hör ordern, direkt. Även "nätet är nere" spelas i motorns röst.


- **Utan internet (raawr US-018).** Snabbvägen tände lampan men högtalaren
  kunde tiga: HA:s svar talas genom molnets TTS. Går det inte säger den nu
  "Klart.", förrenderat vid start och cachat på disk. En fråga som inte når
  modellen får "Jag når inte nätet just nu. Lampor och sånt fungerar ändå."
  när INGEN motors API svarar inom 1 s (`bana0.natet_nere`, alla motorer
  parallellt - bara xAI nere är en failover, inte "inget nät"), i samma
  enda tystnadsplats som "Ett ögonblick." - aldrig båda; proben räknas från
  samma stund och hinner före kvittot. Ett misslyckat modellanrop efter en
  miss kraschar inte längre turen. Konduktörens TTS ger upp anslutningen
  efter 3 s (var 30), så "Klart." inte väntar på ett dött nät.
## 0.26.3 (fork)

- **Hård maxtid per molnsamtal:** `VOICE_SESSION_MAX_SECONDS` (600). Henrik
  2026-10-04, utöver sovläget (30 s tyst) och dagstaket (60 min): en session
  stängs efter tio minuter även om ljud fortsätter komma in (en öppen mikrofon,
  en tv, en enhet som hängt sig), för alla tre motorerna. Ingen
  återuppkoppling förrän nästa väckningsord. Provet faller utan spärren.

## 0.26.2 (fork)

- **Tre motorer i ordning:** `VOICE_PROVIDERS=gemini,xai,openai` (Henrik
  2026-10-04: 1 Gemini, 2 xAI, 3 OpenAI). En motor som fallerar lämnar över
  till den första efter sig som är känd frisk (sista turen, eller sonden mot
  `/models`); finns ingen går den tillbaka till den första, som inte kräver
  bevis. Efter nedkylningen (`PROVIDER_COOLDOWN_MINUTES`) provas den första
  igen. Utan listan gäller `VOICE_PROVIDER` och `VOICE_PROVIDER_BACKUP` som
  förut. Sovläget och dagstaket gäller alla tre (de sitter i varje motor).

## Drift (ingen ny version)

- **`scripts/deploy-core.sh`** - så lägger Rolle ut main på core efter en
  merge (Henrik 2026-10-04: allt som mergas går ut via Rolle). Backup som
  följer symlänken (`cp -aL`, tre senaste sparas), rsync utan `--delete`
  (agenten skriver `recordings/`), `chown raawr`, omstart, hälsoprov (aktiv,
  lyssnar på 127.0.0.1:8080, inget "Fatal error") och automatisk
  tillbakarullning om hälsoprovet faller. `--rollback` för hand.
  `/etc/raawr-rostagent.env` rörs aldrig. Provat mot core 2026-10-04 14:49Z:
  utläggning och tillbakarullning friska efter 8 s.

## 0.26.1 (fork)

- **Molnbudget per dag.** Ägaren 2026-10-04 efter xAI-läckan: "allt vi gör
  framåt behöver försiktighet". Uppkopplade minuter räknas per dag för alla
  motorer och högtalare tillsammans (`MOLN_LEDGER`, standard
  `/data/moln_minuter.json`, överlever omstart). Över
  `MOLN_MAX_MINUTER_PER_DAG` (60) kopplar en väckning inte upp, och en öppen
  session söver direkt. Ett fel någon annanstans kostar då högst så många
  minuter om dagen, oavsett hur leverantören tar betalt.

## 0.26.0 (fork)

- **Sovläge: molnmotorn är uppkopplad bara under ett samtal** (raawr INKAST
  2026-10-04, brådskande). Agenten höll en realtidssession per högtalare
  öppen dygnet runt; xAI tar betalt per uppkopplad minut och stänger en tyst
  session efter 900 s, och agenten kopplade upp igen på 0,5 s - 212 gånger,
  ~45 dollar på ett dygn i ett tyst hus. Nu (`app/providers/sovlage.py`):
  varje motor (OpenAI, xAI, Gemini) startar sovande, väckningen kopplar upp
  (ljudet efter väckningen väntar under tiden), och 30 s tystnad efter
  samtalet (`SOV_EFTER_S`) kopplar ner. En sovande motor återansluts aldrig:
  varken av återställningen, av xAI:s 900 s-stängning (som nu söver) eller av
  Geminis egen återanslutning. `MOLN_SOVLAGE=0` = som förut.

## 0.25.6 (fork)

- **"Jag kollar" säger vad agenten gör.** Ägaren 2026-10-02 23:12: de fasta
  klippen låter mekaniska. Två lager:
  1. Modellen säger det själv, med egna ord. De LÅNGSAMMA verktygens
     beskrivningar (web_search, search_home, play_media, delegera_till_raawr,
     kalender*, ask_openclaw) får en rad om det, på ett ställe:
     `early_ack.with_ack_hint` i `providers.build_service`, alla motorer.
     Verktygslagret, inte systemprompten (0.23.1:s promptrad gav "Jag
     kollar." före varje svar). Snabba verktyg (lampor, GetLiveContext,
     GetDateTime) får inget. På xai är webbsökningen serversidig och har
     ingen beskrivning vi styr.
  2. Skyddsnätet: klippet väljs per verktyg (`ack_phrase`): vädret, nätet,
     kalendern, "Jag letar fram det.", "Jag ber Raawr ta det.", annars
     "Ett ögonblick." (även tystnadskvittot). Förrenderas vid start som
     förut. Spelas bara om modellen inte sagt något ALLS den här turen
     (`TurnLiveness.spoke_this_turn`), inte bara de senaste 2 s.
- **Loopvakten räknar identiska anrop** (granskning av PR #3): samma verktyg
  med samma normaliserade argument, högst 3 per tur; totaltaket 8 -> 12.
  "Tänd kontoret, köket, hallen och sovrummet" är fyra HassTurnOn och alla
  fyra körs nu.
- Testet för xai:s `_create_response` bygger tjänsten på riktigt (via
  `__init__`), så det fäller 0.25.1-felet.

## 0.25.3 (fork)

- **xai: turslutet avgörs lokalt (Silero), inte av xAI:s server_vad.**
  Live 2026-10-02 20:29:42: "Vad är det för väder i helgen?" nådde
  "thinking" först 13 s efter väckningen, senare turer 5-9 s; musik och
  rumsljud höll server_vad öppen. Sessionen får nu `turn_detection: null`;
  samma lokala Silero som Gemini (flyttad till `app/providers/local_turns.py`,
  delad av båda) avgör slutet, och då skickas `input_audio_buffer.commit`
  och `response.create` -- eller, med bana 0 på, får bana 0 avgöra först
  (som på OpenAI). Ljud före talet hålls som pre-roll (0,5 s); följdfönster
  som stängs mitt i en mening besvaras i stället för att tappas. Ny env:
  `XAI_TURN_SILENCE_MS` (förval 1200), `XAI_TURN_DETECTION=server` ger
  tillbaka det gamla.
- **"Jag kollar" kan nu höras på xai.** Tystnadskvittot startar vid
  turslutet, som kom 13 s sent. Och pipecat gör användarens transkript
  (ca 1 s efter turslutet, 20:29:56.2 -> 57.0) till ett emulerat "user
  started speaking", som `TurnLiveness` tog för en ny yttring och som
  avblåste kvittot före sina 1,5 s, varje tur, även på OpenAI. Ett emulerat
  start räknas nu bara när inget riktigt start öppnat turen.
- **xAI:s tomgångsstängning efter 900 s** ("Conversation timed out ... due to
  inactivity", server_error/timeout) är ingen hicka längre: socketen stängs,
  läsaren slutar och ConnectionRecovery återansluter utan att räkna det mot
  motorn (förut "xai hiccup (1/2)" efter en tyst kvart).

## 0.25.1 (fork)

- **Spärr mot verktygsloopar, alla motorer.** Samma verktyg körs högst 3
  gånger per användartur, alla verktyg tillsammans högst 8. Därefter anropas
  inte HA; modellen får svaret "Stopp: du har redan anropat X N gånger i den
  här turen. Svara nu med det du vet, eller säg ärligt att du inte hittar
  det." och loggen får `⏱ tool-loop stopp <namn> <antal>`. Räknas i
  `TurnLiveness`, nollas vid varje ny yttring (`user_started`), kontrolleras
  i `ToolRegistrationMixin` under varje motor. Skäl: Grok anropade
  GetLiveContext upp till 48 gånger i en tur när svaret saknades (0.25.0-proben).

## 0.25.0 (fork)

- **xAI Grok Voice som tredje motor: `VOICE_PROVIDER=xai`.** Ny
  `app/providers/xai_realtime.py`, en underklass till OpenAI-motorn
  (samma protokoll, `wss://api.x.ai/v1/realtime`). Ny env: `XAI_API_KEY`,
  `XAI_MODEL` (förval `grok-voice-latest`), `XAI_VOICE` (förval `rex`;
  `helios` är den mörkaste, median-F0 92 Hz mot rex 108). Går även som
  backup (`VOICE_PROVIDER_BACKUP=xai`).
- server_vad (xAI har ingen semantic_vad); med bana 0 på skickas
  `create_response: false` och agenten ber om svaret själv, som på OpenAI.
  Transkriptionen får `language_hint: sv`.
- xAI:s egen webbsökning (`{"type":"web_search"}`) ersätter vår
  `web_search`-funktion. Sökningen rapporteras som ett `web_search`-anrop
  EFTER det talade svaret; det anropet besvaras inte (då pratar modellen
  igen).
- Socketen översätts innan pipecat läser: `ping` och okända typer släpps,
  `usage: {}`, `role: "tool"`, `content_part.done` utan `part`,
  `arguments.delta` utan `output_index` och xAI:s egen sessionsform i
  `session.updated` fylls i. Utan det dör pipecats läsare (döv enhet) eller
  tappas `response.done`/`session.updated`. Råa händelser från live-nyckeln
  ligger i `tests/fixtures/xai_events.jsonl`.
- "Jag kollar"-klippet på xai renderas med xAI:s TTS i sessionens röst
  (`XAI_ACK_PREFIX`, t.ex. `"[breath] "`, sätts framför frasen).
- Prob 2026-10-02 (core, svenska frågor från Gemini-TTS, live-instruktioner):
  svar på svenska, transkription ordagrann. Första ljud från talets slut:
  väder ~1,2-1,9 s, webb ~1,8-2,2 s (svaret ingår i första repliken),
  "tänd lampan" ~2,1-2,5 s (HassTurnOn), huset 1,3-3,5 s med ett
  GetLiveContext-anrop. Risk: när GetLiveContext inte har svaret anropar
  Grok det igen och igen med påhittade argument (upp till 48 gånger).

## 0.24.1 (fork)

- **"Jag kollar" bara på första frågan efter väckordet.** Ägaren
  2026-10-02 21:16: i en uppföljning (mikrofonen öppen efter svaret, inget
  nytt väckord) ska den inte låta, utom vid en riktigt lång väntan. En tur
  räknas som väckt om enhetens `{"type":"wake"}` kom efter senaste idle;
  annars gäller `EARLY_ACK_FOLLOWUP_MS` (3000, 0 = aldrig) för både
  tystnads- och verktygsutlösaren. Väckta turer behåller `EARLY_ACK_MS` och
  `EARLY_ACK_SILENCE_MS`. Mikrofonens flush räknas inte som väckning.

## 0.24.0 (fork)

- **Redo för `gemini-3.8-live`.** Efter verktygssvaret skickar 3.8 en
  TOM `turn_complete` (usage, inget ljud, 0,01 s efter svaret) och talar
  svaret i en NY tur; två anrop ger två tomma (uppmätt mot live-nyckeln
  2026-10-02). Den tomma togs för svarets slut: förlorad-tur-klockan
  nollades, uppföljningsbegäran gick före svaret och efter en talad
  inledning gick enheten idle innan svaret kom. Nu sväljs en
  `turn_complete` som kommer efter ett toolCall men före nytt ljud, även
  för pipecat; svarets tur fortsätter samma replik och avslutar den en gång.
- **Googles sökning: förval PÅ för 3.x, AV för 2.5.** `GEMINI_GOOGLE_SEARCH`
  osatt följer modellen; `true`/`false` styr som förut. På, tas vår
  `web_search` bort ur Geminis lista. 2.5 native audio + google_search +
  funktioner + thinking_budget 0 ger 1011 före verktygsanropet (Googles
  fel); 3.8 gav 0 av 21 ljudsessioner med sökningen på.
- Prob mot 3.8: `thinking_level` vägras (1007), `thinking_budget` 0/512
  accepteras (vi skickar inget), språk `sv` och `sv-SE` ok, Charon ok,
  proactivity accepteras, affective dialog vägras (1007, vi ber inte om
  den). Mätt i ljudläge, median första ljud från activityEnd, 5 körningar
  vardera, 2.5 → 3.8: husfråga med verktyg 4,9 → 5,2 s (svaret 6,4 → 5,2),
  väder ur Idag-blocket 4,0 → 2,2 s, webbfråga 12,7 (vår web_search) →
  3,4 s (grundad). Inga 1011/1008 på någon av dem.

## 0.23.3 (fork)

- **"Jag kollar" i svarets röst.** Ägaren 2026-10-02 18:12: kvittensen kom
  i OpenAI:s röst, svaret i Geminis Charon -- två personer i rummet. Nu
  renderas replikerna i den aktiva motorns röst: på Gemini med Geminis TTS
  (`GEMINI_TTS_MODEL`, förval `gemini-2.5-flash-preview-tts`) och sessionens
  `GEMINI_VOICE`, på OpenAI med `gpt-4o-mini-tts` och `OPENAI_VOICE`.
  Förrenderas vid start för båda motorerna, cachas på disk, 24 kHz PCM16
  som enheten spelar (omsamplas om Google svarar med annan takt). Misslyckas
  en rendering används den gamla klippet, och loggen säger det. Geminis TTS
  vägrar ibland den nakna frasen (tom kandidat, "Två sek, jag tittar." varje
  gång); inramad som "Läs upp på svenska ...: <fras>" sägs den, och ramen
  hörs inte (kontrollerat med STT). TTS-kvoten är 10 anrop/min.
- **Raden "Idag:" finns nu i systemprompten.** Kalenderskripten har länge
  bett modellen räkna datum "ur raden Idag: i systemprompten" -- en rad som
  inte fanns. Blocket (`app/idag.py`, ~125 tokens): veckodag, datum och
  klocka (Europe/Stockholm), SMHI-prognosen för idag, i morgon och i
  övermorgon (`script__vaderprognos`) och de tre nästa kalenderhändelserna
  (`script__kalender_sok`), hämtade genom comms MCP-dörr som modellens egna
  anrop. Hämtas var `IDAG_REFRESH_SECONDS` (600) och före första sessionen;
  en misslyckad hämtning behåller sista goda delen. Dagnamn räknas när
  blocket skrivs, inte när det hämtades. Gemini: instruktionen renderas om
  vid varje (åter)anslutning, och en session vars block är äldre än
  intervallet återansluts (med resumption-handtaget, samtalet kvar) först
  när enheten varit tyst -- aldrig mitt i en tur. Uppmätt mot live-nyckeln:
  en återupptagen session följer en NY systeminstruktion. Ingen
  session.update (D-70). OpenAI får blocket när sessionen byggs.
- **Googles sökning på Gemini: byggd men AV** (`GEMINI_GOOGLE_SEARCH=true`
  slår på, och tar då bort vår `web_search` ur Geminis lista). Uppmätt
  2026-10-02: grundade webbsvar fungerar och vanliga turer blir inte
  långsammare (median första ljud 1,63 s med, 1,82 s utan), men med
  sökningen i sessionen dog "Vilken temperatur är det i kontoret?"
  (GetLiveContext) med 1011 Internal error 5 av 5 gånger, aldrig utan.

## 0.23.2 (fork)

- **"Jag kollar" även när modellen själv är långsam.** Live 2026-10-02
  15:19:44, Gemini, "vad blir det för väder i helgen": activityEnd 44.05,
  funktionsanrop 47.97, verktyget klart 48.18 (0,2 s), första ljud 49.99.
  Sex sekunders tystnad, 5,5 av dem Gemini, och 0.23.1:s kvittens tände
  aldrig eftersom verktyget var snabbt. Nu: har modellen inte gett något
  ljud `EARLY_ACK_SILENCE_MS` (1500, 0 = av) efter att den fått turen
  (Geminis activityEnd; OpenAI:s speech_stopped, eller bana 0:s miss) sägs
  samma korta replik. Samma regler som 0.23.1: en gång per tur (delad med
  verktygskvittensen), aldrig över modellens ljud eller ett nytt yttrande,
  aldrig i historiken. En bana 0-träff frågar aldrig modellen och kvitteras
  aldrig; en hängande VAD-stopp på OpenAI inte heller.
- **Smalare verktygslista, båda motorerna: 57 → 40.** Gemini läser alla
  deklarationer före sitt första anrop. Gömda (ingen anropad 1-2 okt):
  `HassBroadcast`, `HassClimateSetTemperature`, `HassSetPosition`,
  `HassStopMoving`, `RaawrHubVisa`, `RaawrHubAterstall`, och elva gamla
  numrerade skript (Homekit start, Skicka hem Hugo, nio Städa-rum; de nås
  fortfarande via `HassTurnOn` med skriptets namn). Deklarationerna
  24 885 → 21 175 byte. Listan bor på ett ställe, `app/tool_selection.py`;
  `TOOL_DENY` (kommaseparerad) ersätter den, `-` gömmer inget.
  `MCP_TOOL_ALLOWLIST` gäller som förut, nu genom samma funktion.
  Effekten på Geminis tid till första anrop är inte uppmätt.

## 0.23.1 (fork)

- **Ett tidigt "jag kollar" när ett verktyg dröjer.** Ägaren 2026-10-02:
  när agenten måste kolla i backend blir det tyst. Ett verktyg som inte
  är klart efter `EARLY_ACK_MS` (700) ger en kort replik i Björns röst
  ("Vänta, jag kollar.", varierad, aldrig frågetecken) genom samma
  skyddade TTS-fil som bana 0, utanför modellens historik, högst en gång
  per tur och aldrig om modellens eget ljud redan har börjat. Klippet
  skickas i ett svep så ett svar som börjar under det köas efter.
  Personan ber också modellen säga det själv före en långsam uppslagning.
  `EARLY_ACK_MS=0` stänger av.
- **Varje verktygsanrop loggar `⏱ tool <namn> <ms> ok|fel`.** Journalen
  1-2 okt: HA-verktygen 0,07-0,9 s; `web_search` 4,7-4,9 s, `play_media`
  2,7-4,8 s, `ask_openclaw` 8,9 s.
- **web_search med låg resonemangsinsats.** `WEB_SEARCH_REASONING_EFFORT`
  (förval `low`, tom = modellens eget). Mätt från core: gpt-5.5 10,6/7,2 s
  på förval, 5,5/6,3 s på low; gpt-5.4-mini på low 4,5/4,6 s.

## 0.22.5 (fork)

- **Tillägget bestämmer själv var en tur börjar och slutar på Gemini.**
  0.22.4 (START HIGH) provades live 13:42-13:45: "Var är klockan?" sades
  vid -26 dBFS, samma nivå som OpenAI-turer som fungerat (röstprob
  134234), och Gemini gav ingenting på en minut. Ljudet är rätt — Gemini
  transkriberade det korrekt en gång samma morgon — men Googles
  automatiska aktivitetsdetektering öppnar inga turer för den här
  enheten. Nu stängs den av (`automatic_activity_detection.disabled`),
  och en lokal Silero-VAD (modellen pipecat redan levererar, körd av
  sherpa-onnx som redan finns för röstavtryck — inget nytt beroende)
  skickar `activityStart` med 0,5 s förrulle när någon börjar tala och
  `activityEnd` efter `gemini_vad_silence_duration_ms` tystnad. Enheten
  signalerar bara väckningen, aldrig slut på tal; OpenAI avgör det också
  på serversidan. Stoppord och följdfönstrets avklipp överger en öppen
  aktivitet utan `activityEnd`, så den besvaras aldrig; `audioStreamEnd`
  skickas inte i manuellt läge. Silero nollställs efter 5 s tystnad
  (utan det döv efter ~20 s — uppmätt på 13:42-inspelningen). Går VAD:n
  inte att ladda faller sessionen tillbaka på Googles detektering.

## 0.22.4 (fork)

- **Gemini hör den som just väckte den** (gemini-snabb). Kontoret
  2026-10-02 12:52-12:54, Gemini efter failover, START_SENSITIVITY_LOW:
  sju saker sades till enheten, Gemini öppnade en enda tur. "Vad händer,
  frågar jag" besvarades 22 s efter att det sades, ur cachat ljud; "Hallo!"
  och "Vad är klockan?", högt och tydligt, gav inte ens en transkription,
  och följdfönstrets och stoppknappens audioStreamEnd kastade sedan det
  cachade ljudet. Ljudet nådde Google helt (provat mot en lokal falsk
  Live-server genom 1008-återanslutningar och audioStreamEnd), så det var
  startdetektorn. Standard för `gemini_vad_start_sensitivity` är nu
  `high`, även vid felstavat värde. Mikrofonen strömmar bara efter
  väckning eller i följdfönstret och är stängd medan assistenten talar,
  så LOW:s skydd mot rummet kostade mer än det gav. **Driftsteg:**
  `/etc/raawr-rostagent.env` sätter uttryckligen `low` och måste ändras
  till `high`, annars ändrar uppdateringen ingenting i kontoret.

## 0.22.3 (fork)

- **Omkopplingen efter en HA-omstart klipper inte längre utrop eller
  följdfönster, och studsar inte enheten om HA fladdrar** (D-72). När
  HA:s verktyg kommer tillbaka stänger agenten enhetens anslutning först
  när enheten är ledig. "Ledig" räknade bara fas och senaste väckning, så
  ett utrop (som saknar fas) eller följdfönstret efter en lång tur kunde
  klippas. Nu räknas ett pågående utrop som upptaget, och tystnaden mäts
  också från turens och utropets slut, förlängd med följdfönstret
  (`follow_up_listen_seconds`). Högst en omkoppling per enhet var tionde
  minut (`MCP_RECYCLE_MIN_INTERVAL_SECONDS`); däremellan fortsätter
  hämtningen.

## 0.22.2 (fork)

- **En enhet som återansluter mitt i en nedstängning blir inte längre döv**
  (D-80). Ljudinspelningens två processorer delades av alla pipelines, så
  när kontoret återanslöt medan den gamla sessionen avbröts gick den gamla
  pipelinens CancelFrame in i den nya och satte inspelaren i ett
  avbrytläge som pipecat aldrig återställer. Därefter släpptes allt ljud
  från enheten innan det nådde OpenAI, tills tillägget startades om. Nu
  får varje pipeline egna inspelare.

## 0.22.1 (fork)

- **Bana 0 hör bara användarens yttrande** (US-016, granskningsfynd F2).
  En följdtur har ingen väckning, så turens ljud nollställdes aldrig och
  STT:n fick allt sedan förra turen: tystnad och ekot av modellens eget
  svar, upp till 30 s. Det kunde spräcka tidsgränsen på 600 ms och, värre,
  få Whisper att skriva ut modellens egna ord som en order som HA sedan
  utför. Nu börjar turens ljud om när användaren börjar tala
  (`input_audio_buffer.speech_started`), med 0,8 s förrulle så att första
  stavelsen följer med, och inget ljud sparas medan modellen svarar.

## 0.22.0 (fork)

- **Bana 0: enkla hemkommandon går till HA:s egen agent först** (raawr
  US-016). Nytt tillval `bana0_stt` (`host:port` till en lokal Wyoming-STT,
  tomt = av, som i dag). När det är satt skickas turens ljud vid turens slut
  (`input_audio_buffer.speech_stopped`) till STT:n, texten till HA:s
  konversationsagent genom raawr-comms, och HA:s bekräftelse talas upp i
  rummet. Modellen får då bara veta vad som sades och ombeds aldrig svara.
  Hanterade HA inte ordern (204, fel, tidsgräns) får modellen svara som
  vanligt. Med bana 0 på skapar servern inte längre svar själv
  (`create_response` av); agenten skickar `response.create` vid en miss.
  Tidsgränser: `BANA0_STT_TIMEOUT_MS` (förval 600) och
  `BANA0_COMMS_TIMEOUT_MS` (förval 4000). Bara OpenAI och `semantic_vad`.

## 0.21.2 (fork)

- **0.21.1:s återhämtning gjorde sessionen döv — nu återansluts enheten i
  stället** (raawr D-70). 0.21.1 lade in de återhämtade HA-verktygen i den
  levande OpenAI-sessionen med `session.update`. Live 2026-10-01 blev
  sessionen efter det döv: två väckningar i rad fick "no server VAD activity
  12s after wake", och inte ens en återanslutning mot OpenAI hjälpte, först
  när högtalaren själv anslöt på nytt fungerade det. Nu rör agenten inte den
  levande sessionen alls. När HA svarar igen väntar den tills enheten är
  ledig (ingen tur pågår och ingen väckning på `MCP_RECYCLE_QUIET_SECONDS`,
  förval 30) och stänger sedan enhetens anslutning normalt. Firmwaren
  ansluter igen och får en ny session med alla verktyg den vanliga vägen:
  `✅ HA back — recycling connection for <enhet> to load N tools`.

## 0.21.1 (fork)

- **HA-verktygen kommer tillbaka av sig själva** (raawr D-70). Var HA nere
  (omstart) när en högtalare anslöt byggdes sessionen utan HA-verktyg (14
  i stället för 59), och de kom inte tillbaka förrän högtalaren anslöt på
  nytt — vilket kan dröja timmar. Nu försöker agenten igen i bakgrunden var
  `MCP_TOOLS_RETRY_SECONDS` (förval 15) och lägger in verktygen i den
  levande sessionen (`session.update` för OpenAI; Gemini får dem vid nästa
  återanslutning) när HA svarar: `✅ HA tools recovered: N`. Försöket
  stoppas när enheten kopplar ner.

## 0.21.0 (fork)

- **En Home Assistant som startar om tystar inte längre högtalaren** (raawr
  US-014). 2026-09-30 startades HA om; agenten tog emot ljud men öppnade
  ingen modellsession förrän den själv startades om 35 minuter senare.
  Hämtningen av HA:s MCP-verktyg sker under pipeline-låset, och pipecats
  MCP-klient låter en läsning hänga i upp till 300 s per försök — varje ny
  anslutning ställde sig i kö bakom den. Nu ger hämtningen upp efter
  `MCP_TOOLS_TIMEOUT_SECONDS` (förval 5) och sessionen byggs utan HA-verktyg,
  precis som vid andra fel. Nästa anslutning försöker igen.
- **Announce-endpointen kan binda till loopback.** `ANNOUNCE_HOST` (förval
  `0.0.0.0`, så tillägget är oförändrat) — `127.0.0.1` när agenten kör som
  systemd-tjänst bakom en omvänd proxy.

## 0.20.0 (fork)

- **Tillägget håller ingen Home Assistant-nyckel längre** (raawr US-011,
  beslut 56). `homeassistant_api` är av, `longlived_token` och `ha_mcp_url`
  är borta. Varje HA-anrop — MCP-verktygen, `search_home`, `play_media`,
  sensorerna, timerns ringbrytare och inspelningens väckljud — går till
  `ha_api_url` (raawr-comms) med `comms_nyckel`, i `app/ha_api.py`. Comms
  håller HA:s token och släpper bara de former tillägget faktiskt använder.
  Återvägen: `homeassistant_api: true` och 0.19.5.

## 0.19.5 (fork)

- **Timern ringer, och gör inget annat.** En 30-sekunderstimer var en röst vid
  30 s och en klocka vid 50 s: ett talat utrop först, sedan 20 sekunders
  respit, sedan bjällran om ingen väckt enheten. Rösten kom dessutom från en
  annan motor än björnen. Efterfrågat och borttaget 2026-09-09: "det räcker
  med chime på rätt tid". En timer är det enda i huset som måste vara exakt.
  Utropet, respiten, ägaren och väck-kvittensen är borta — inte avstängda,
  borta, med sin inkoppling i main.py. TTS-banan finns kvar för
  inspelningscoachen och announce-endpointen.

## 0.19.4 (fork)

- **En främling i huset läste upp timern.** Utropet vid utgången går genom
  TTS-banan, inte genom modellens röst — och den banan var skriven för
  engelska inspelningsuppmaningar: rösten `fable` med instruktionen "Calm,
  composed British butler." Hört live 2026-09-09 23:19, mellan två repliker
  från en djup svensk björn. Nu `onyx` som förval, och instruktionen beskriver
  björnen i stället för en betjänt. Detta gör inte banan till hans röst —
  det är en annan motor — men den låter inte längre som en annan person.
  Riktig lagning, senare: låt modellen själv säga meningen.

## 0.19.3 (fork)

- **Timern talade engelska i ett svenskt hus.** Hört live 2026-09-09 22:58:
  "Henrik, your timer is done." Utropet vid utgången är den enda mening
  add-onet säger utan att modellen skrivit den, och den var kvar på engelska
  sedan uppströms. Nu: "Henrik, din timer är klar." — och med etikett "Henrik,
  din timer för pasta är klar." Utan namn får meningen den stora bokstaven
  namnet annars bär. Ingen inställning: prompten, transkriberingsspråket och
  huset är svenska, en språkknapp här vore bara ett andra ställe att glömma.

## 0.19.2 (fork)

- **Tools died 81 ms after they started, and the assistant said they had
  worked.** Measured live 2026-09-09 22:23: `play_media` was called, the
  handler logged `🎵 play_media: 'chill' type=playlist player=kontoret`, and
  81 ms later pipecat cancelled it — while the reply said "jajemän, fixar
  det" and nothing played. Same for `GetLiveContext` and `vaderprognos`, on
  every turn all evening. The cause is Gemini's late input transcript: it
  arrives AFTER the model has already called the tool, and pipecat's user
  aggregator turns that late transcript into an emulated "user started
  speaking" — so the very sentence that asked for the tool interrupts it, and
  the interruption cancels it. The rule that prevents this (`register_function`
  with `cancel_on_interruption=False`) was written for the OpenAI service and
  stayed there when the house moved to Gemini. It now lives in one place,
  `ToolRegistrationMixin`, that both engines mix in, so it cannot protect one
  engine and forget the other again. The speaker gate and the liveness
  tracking moved with it and now cover Gemini too — they never did before.

## 0.19.1 (fork)

- **Fixes a regression 0.18.3 shipped straight into the house.** Waiting for
  the engine's end-of-turn assumed `LLMFullResponseEndFrame` reaches
  PhaseEmitter. It does not: `LLMAssistantAggregator` sits between the engine
  and the phase machine and consumes it. So the wait was spent in full on
  EVERY Gemini turn — the log said `no end-of-turn from the engine after 8.1s`
  each time, the device stayed shut for eight seconds after each answer, and
  the user had to repeat himself. Two changes: the Gemini service now calls
  the phase machine directly when Google reports `turn_complete` (the path
  that actually works), and the wait is only ever entered on a connection that
  has genuinely seen an end-of-turn — so an engine that never signals behaves
  exactly as it did before 0.18.3, rather than paying the cap every turn.

## 0.19.0 (fork)

- **The microphone stays open because the model asked, not because a timer
  said so.** New tool `request_follow_up`. The device has accepted
  `{"type":"request_follow_up"}` all along — `va_client.cpp`'s own comment
  names the tool that was supposed to send it, and it never existed on this
  side. So the window was opened on `follow_up_ms` instead, sent once at
  connect and applied by the device after EVERY reply: say "that was all",
  get "Bra. Hörs." back, and the mic still opened for another eight seconds,
  with a chime, listening to an empty room. The system prompt has always
  promised the opposite — "the house keeps the microphone open exactly as long
  as your reply ends with a question mark" — and nothing implemented it.
  Now the model asks in the same turn as a real question, and says nothing
  after a finished answer.
  - Reading the reply for a question mark would have been the obvious fix and
    is the wrong one: on Gemini the assistant transcript does not reliably
    reach this pipeline (measured 2026-09-09 — every user line logged, not one
    assistant line), so that rule would have silently never fired.
  - The request is recorded at tool-call time but sent at the engine's
    end-of-turn. The device opens the mic as soon as its speaker drains, which
    at tool-call time it usually has — sending immediately would open the mic
    before the question was spoken.
  - **Set `follow_up_listen_seconds` to 0** to get the new behaviour; anything
    higher keeps the old unconditional window on top of it.
- 7 new tests (194 total).

## 0.18.3 (fork)

- **No more start chime in the middle of an answer.** The phase machine ended
  a reply on a timer: 1.5 s of silence after the last audio and the device was
  told the turn was over. That holds for OpenAI, whose reply arrives in
  sentence-sized pieces. It does not hold for Gemini — measured live
  2026-09-09, one answer came in three bursts with 6.7 s and 4.7 s of silence
  between them, so the phase went replying → idle → replying twice inside a
  single answer and the device chimed on every way back in. The engine says
  when it is finished (`LLMFullResponseEndFrame`, from Gemini's own
  `turn_complete`), which a timer can only guess at, so the debounce now waits
  for that before releasing the device. Capped by `PHASE_MID_TURN_GRACE_MS`
  (8 s) so an engine that never sends one — or a reply that dies half-way —
  costs a slow idle rather than a device stuck in "replying".

## 0.18.2 (fork)

- **The native-audio model can actually be used in a Swedish house.** It
  refuses an explicit language code this house needs — probed live:
  `sv` and `sv-SE` both come back `1007 Unsupported language code`, while
  `en-US`, `de-DE` and *no code at all* open fine. So on these models the code
  is dropped and the (entirely Swedish) system instruction steers the
  language. There is no clean way to ask pipecat for that: `language=None`
  becomes the string `"en-US"` before it reaches the wire, which would have
  pinned the house to English *silently*, since en-US is a code the model
  accepts. The setting is cleared on the built service instead, and logged.
- **The wedge warning stopped crying wolf on Gemini.** `force_reconnect` has
  stood back from a self-healing engine since 0.17.3, but the alarming
  "presuming a half-open OpenAI socket" warning was logged before the guard
  was reached, so the log kept reporting a repair that never happened.

## 0.18.1 (fork)

- **Proactive audio actually reaches the session now.** 0.18.0 followed
  Google's guide, which says these features need API version `v1beta`. They do
  not, and the mistake is invisible: google-genai already defaults to v1beta,
  so setting it changes nothing, and the session is refused with
  `1007 ... Unknown name "proactivity" at 'setup': Cannot find field`. Probed
  all six combinations against the live account: v1beta takes affective dialog
  but not proactivity; **v1alpha takes both**. pipecat's own docstring said
  v1alpha all along.

## 0.18.0 (fork)

Towards a Gemini session you can hold an ordinary conversation with.

- **The engine is finally told when the microphone stops.** Gemini Live has
  `audioStreamEnd` — Google's guide calls it the way to "flush any cached
  audio" when an audio stream pauses, after which the client "can resume
  sending audio data at any time without reconnecting". This add-on never sent
  it. Two consequences: half an utterance, left behind when the follow-up
  window closed mid-sentence, stayed cached on Google's side and could be
  completed into a stale answer on the next wake (the OpenAI path has cleared
  exactly this since 2026-06-12); and a pause was indistinguishable from a
  dead client, which is what the ~152 s idle hang-ups were. It is now sent on
  the device's stop button and on the follow-up cut-off, through one shared
  `drop_pending_input_audio()` that speaks each engine's own dialect —
  `input_audio_buffer.clear` for OpenAI, `audioStreamEnd` for Gemini.
- **`gemini_affective_dialog`**: the model matches the expression and tone it
  hears instead of reading every answer flat. Shares proactive audio's gate —
  a native-audio model on `v1beta` — and the same refusal to be switched on
  with a model that cannot carry it.
- 6 new tests (181 total).

**Not changed, and deliberately**: handsfree barge-in stays off. It was tried
on this hardware and measured: the ~10x speaker→mic leak defeated the XMOS
AEC, the VAD flapped listening↔thinking, and it once built into an acoustic
feedback squeal. See `barge_in: false` in the firmware for the full note.

## 0.17.5 (fork)

- **A quiet house no longer kills the Gemini engine.** The Voice PE is
  push-to-talk: between conversations the add-on sends Google nothing, and
  Google hangs up on a session it hears nothing from — measured live at a very
  regular ~152 s of silence. pipecat reconnects in about half a second, so the
  hang-up itself is invisible. But pipecat only forgives a failure from inside
  its receive loop, when a message ARRIVES; a silent connection delivers none,
  so the stable-connection rule never ran and the count never cleared. Three
  idle hang-ups in a row — about seven and a half quiet minutes — were pushed
  as a fatal error. Live 2026-09-09 17:05:41: the engine died and the house
  had no voice until the add-on was restarted 45 minutes later. The service is
  now a subclass that runs pipecat's own rule at the moment of failure, when
  the connection's lifetime is known. Three failures inside the threshold
  still go fatal — that is the case the counter is for.

## 0.17.4 (fork)

- **A false wake can be reported again.** Both ways of flagging one -- saying
  so ("that was a false alarm") and the button-cancel shortly after a wake --
  listed `/share/voice-probes` directly. That directory is only written while
  `ENABLE_RECORDING` is on, so on a normal install it does not exist and every
  report ended in `FileNotFoundError`: the assistant apologised, and the
  counter behind `sensor.voicepe_<instance>_false_wakes_today` never moved.
  The count needs no audio, so it is now published either way, and a missing
  recording is reported as "nothing kept" rather than as an error. One shared
  helper replaces the path literal that had been copied into three files.

## 0.17.3 (fork)

First run in a real house, 2026-09-09, found the Gemini engine had been given
a session but not a room. Three fixes, all Gemini-only — OpenAI is untouched.

- **Turn detection is now configured, not left to Google.** The session sent
  no `realtime_input_config` at all, so the API ran its automatic activity
  detection at its own `START_SENSITIVITY_HIGH`. On a speaker that hears its
  own voice that means answering room noise, the tail of its own reply, and
  half-words nobody said — the log has it answering `Och?`, `Ja.`, `Né?` and
  one whole sentence in Portuguese. Four new settings, defaulting to the
  equivalent of the OpenAI side's `vad_eagerness: low`:
  `gemini_vad_start_sensitivity` (low), `gemini_vad_end_sensitivity` (low),
  `gemini_vad_prefix_padding_ms` (300), `gemini_vad_silence_duration_ms`
  (800 — Google's own recommended range is 500–800 ms).
- **The wedge repair no longer fights an engine that heals itself.** Twelve
  seconds after any quiet wake, `force_reconnect` ran the full OpenAI repair
  on Gemini: it forced an idle phase at the device first — which the user
  hears as the end-of-turn chime and sees as the LED, mid-conversation — and
  only then discovered `service has no reset_conversation()` and gave up,
  leaving a session pipecat was already reconnecting on its own. Observed
  three times in ten minutes. `handle_error` had stood back from a
  self-healing engine since the provider work; `force_reconnect` and the
  proactive 60-minute-cap refresh now read the same table.
- **Optional: `gemini_proactive_audio`.** Google's own "was that meant for
  me?" judgement — the model hears the room but stays silent when it was not
  addressed, and silence is not billed. This is what the Gemini app does. It
  needs a native-audio model (`models/gemini-2.5-flash-native-audio-latest`)
  on API version `v1beta`; Gemini 3.1 Flash Live does not support it, so with
  that model the setting is ignored and warned about rather than allowed to
  get the session refused.
- 12 new tests (170 total). All six of the new behaviour tests were confirmed
  to fail against 0.17.2 before the fixes landed.

## 0.17.0 (fork)

- **Second voice engine: Google Gemini Live**, as an alternative to OpenAI
  Realtime. `voice_provider` picks the primary engine (`openai` default);
  `voice_provider_backup` names the *other* engine, which takes over
  automatically when the primary runs out of money, has its key rejected, or
  its socket dies and a retry doesn't help. A backup equal to the primary
  means no failover, the same as `none`. New options: `gemini_api_key`,
  `gemini_model`, `gemini_voice`, `provider_cooldown_minutes` (default 30 —
  how long the backup runs before the primary is tried again).
- **What changes if you only update, without touching any setting**: the
  engine and the audio path are the same (OpenAI, no failover, same voice and
  latency), but the assistant now remembers across reconnects where it
  previously forgot. The cached conversation is finally delivered to the
  engine on reconnect — it provably never was before, on either engine — and
  the session is re-seeded every hour instead of starting blank, so each turn
  is billed with that history as its prefix (bounded by
  `max_context_messages`, default 12). A new entity,
  `sensor.voicepe_<instance>_motor`, also appears.
- Failure is classified before any switch is made: quota/billing and
  auth/model errors switch immediately; a transient error (timeout, dead
  socket, 5xx) gets one retry on the same engine first; a tool error never
  triggers a switch.
- New `sensor.voicepe_<instance>_motor`: which engine is running, with
  `reason`, `switched_at`, and `retry_primary_in_s` attributes, so a switch is
  visible instead of only appearing in the log.
- Both engines get the same tools, system prompt, memory, and duck/interrupt
  behaviour. What's genuinely different between them (voice names, no
  `openai_speed` or noise reduction on Gemini, its own VAD tuning, cost
  observability only implemented for OpenAI so far) is documented in
  `Docs/superpowers/specs/2026-09-08-gemini-live-provider-design.md` in the
  main Raawr repo, not hidden behind a claim of parity.
- 158 unit tests cover the router, failure classification, and both provider
  modules (no API keys required to run them). **Not yet exercised against a
  live house** — that verification is a separate, manual step.

## 0.16.11 (fork)

- Fixed announcements immediately after a single Voice PE reconnect. The sole
  connected device is now addressable before its first wake/audio activity;
  multi-device instances still require activity or an explicit target when
  more than one idle device is connected.

## 0.16.10 (fork)

- Added an opt-in relay-side output lead buffer for the measured Voice PE
  resampler cold-start defect. It holds the first part of a reply and releases
  it as a burst, giving the device a playout lead before normal streaming.
- The buffer is safe across interruption, connection recovery, short replies,
  and mid-reply pauses, with a bounded watchdog for a stalled source. It is
  disabled by default; our two-device deployment enables 400 ms while the
  existing device playback prebuffer remains 250 ms.

## 0.16.9 (fork)

- Added selectable OpenAI transcription models, including `gpt-live-transcribe`
  and `gpt-transcribe`. Both now receive their required `languages` array when
  a transcription language is configured.

## 0.16.8 (fork)

- **Multiple Voice PE devices on one add-on instance**: every connected device
  now has its own OpenAI session, conversation history, audio pipeline, phase
  updates, speaker-recognition state, and enrollment flow. Devices can talk at
  the same time without interrupting or receiving audio from one another.
- Reconnecting a device replaces only its own stale connection; other active
  devices keep their conversations intact.
- Timer announcements and acknowledgements stay with the device that created
  the timer. Targeted announce requests now return an error when that device
  is offline rather than reporting a false success.

## 0.16.7 (fork)

- **Wedge watchdog**: a half-open OpenAI socket (dies silently during an idle
  gap — no close frame, no error) used to swallow the next request entirely:
  audio streamed out, nothing came back, no reply. Now every wake arms a 12 s
  liveness check; if the server VAD shows no activity, the session reconnects
  in place (~3 s). A silent wake triggers a harmless idle-time reconnect.

## 0.16.6 (fork)

- **Fixed: direct `ask_openclaw` silently rebinding to the HA MCP path.**
  pipecat registers a handler for every MCP tool during session creation,
  which overwrote the native direct-path handler — resurrecting the 60-second
  MCP cap ("it failed" while the task actually succeeded). Native registration
  now happens after MCP registration and wins.
- **Announce endpoint repeat guard**: near-duplicate messages within 10
  minutes are accepted but not spoken (`duplicate_suppressed`), so an agent
  monitoring for a result can't re-announce the same news every poll cycle.

## 0.16.5 (fork)

- **Voice prints now build automatically** when enrollment completes — the
  coach confirms out loud, warns when the enrolled name isn't in
  `speaker_male_name`/`speaker_female_name` (recognition stays inactive until
  it is), and asks for a retry when there wasn't enough clear speech.
  Previously this required a manual `python3 -m app.build_voiceprint` step
  that was easy to miss, leaving enrollments silently ineffective.
- New `sensor.voicepe_<instance>_voice_prints`: enrolled prints, with an
  `active` attribute showing which are enrolled *and* configured.

## 0.16.4 (fork)

- **Cost observability**: every response's exact token usage (from the API's
  `response.done`) is logged with an estimated cost, and a
  `sensor.voicepe_<instance>_openai_cost_today` sensor tracks daily spend in
  Home Assistant. Rates auto-switch for mini models.
- Recommended default applied to our install: `max_output_tokens: 1200` —
  output audio is the dominant per-turn meter ($64/1M tokens, measured); a cap
  bounds runaway monologues without touching normal replies.

## 0.16.3 (fork)

- Documentation overhaul: marketing README, `docs/` (getting started,
  configuration reference, features, agent integration, FAQ); repository
  renamed to `voicepe-realtime` (old URLs redirect). `repository.json` now
  carries this project's identity (was still the upstream fork's).
- `enrollment_phrase` default is now "hey leonard" (matches the shipped
  default wake word); HA UI help text added for all fork options.

## 0.16.2 (fork)

- **Guaranteed report-back on long delegations**: ask_openclaw now sends the
  instance name as `room`; the bridge answers "still working" at 120s instead
  of killing the turn, and delivers the agent's eventual answer to that room's
  announce endpoint itself. Previously a >145s research task was reported as
  a failure by voice while the agent kept working with nowhere to deliver.

## 0.16.1 (fork)

- **`recall_memory` tool** (with `openclaw_url`): instant deterministic search
  of the agent's memory files via the bridge (`{"recall": query}` →
  `{"matches": [...]}`). Registered as the FIRST stop for personal/household
  recall; `ask_openclaw` becomes the deep fallback. Fixes recall being a
  40-80s agent turn that found or missed facts depending on phrasing.

## 0.16.0 (fork)

- **Announce endpoint** (`announce_port` + `announce_token` options): a LAN
  route back to the device for the household's external agent. POST
  `/announce {"message": "..."}` (bearer-authed) speaks the message through
  the device's guarded TTS lane — the same path timers use — so a delegated
  task ("research X") can report back by voice minutes later. Disabled unless
  both options are set; 503 when no device is connected.

## 0.15.1 (fork)

- **Direct OpenClaw escalation** (`openclaw_url` option): `ask_openclaw` now
  calls the bridge endpoint directly instead of going through HA's MCP server,
  whose hardcoded 60-second request timeout killed longer agent turns (deep
  memory recall, contact lookups). Direct calls get ~2.5 minutes. Unset, the
  MCP-script path is used unchanged. The speaker gate applies either way.
- (0.10–0.15.0 entries — speaker voice-prints, timers, enrollment v2, HA
  sensors, false-wake flagging, voice-instructed memory — are in git history.)

## 0.9.0 (fork)

- **Firmware-backed voice enrollment** (pairs with firmware commit 5095ed0+):
  the device enters a true enrollment mode — mic pinned open, wake/stop models
  disarmed, cyan breathing LED, 10-minute hard cap, center button as physical
  escape — while an automated audio coach (gpt-4o-mini-tts prompts, cached,
  pushed down the speaker lane on a fixed schedule) guides 25 varied wake-phrase
  repetitions plus 90 s of natural speech. Mic audio flows ONLY to the recorder
  during enrollment: OpenAI hears nothing, so no VAD commits, no forced
  responses, no cost, no conversation mechanics to fight. New options:
  `enrollment_phrase`, `enrollment_tts_voice`.

## 0.8.0 (fork)

- **Voice enrollment**: say "I want to teach you my voice" — the assistant runs
  a guided recording session (varied wake-phrase repetitions + natural speech)
  via the new `voice_enrollment` tool, capturing the raw device mic stream to
  `/share/voice-enrollment/<person>_<timestamp>.wav` (16 kHz mono, 15-minute
  safety cap, persists across rebuilds). One session yields wake-word training
  positives AND voice-print enrollment audio. Recordings are personal data and
  are not managed by the add-on beyond writing the file.

## 0.7.1 (fork)

- Speaker probe tuned for real device audio (live test found 3-7 voiced frames
  in actual speech vs 100+ on synthetic bench audio): YIN threshold 0.15 → 0.20
  with a moderate-periodicity argmin fallback, energy gate 0.15 → 0.08 of peak
  RMS, minimum voiced frames 12 → 8, capture window 2.5 s → 3.0 s. Synthetic
  bench unchanged (0% wrong on typical voices).
- Debug: when `enable_recording` is on, each probe capture is saved to
  `recordings/probe_*.wav` for offline threshold calibration.

## 0.7.0 (fork)

- **Speaker context v1**: optional voice-type (male/female) detection for a
  two-person household. On every wake the first ~2.5 s of command audio is
  classified by median pitch (pure numpy YIN, in-process, off the event loop;
  benched at 98.6% right / 0% wrong across 11 typical synthetic voices) and the
  verdict is injected into the Realtime session as a system item, so the
  assistant can address the speaker by name ("sir"/"ma'am") and hedge when
  uncertain. New options: `speaker_male_name`, `speaker_female_name` (both
  empty = feature off).
- **Speaker-gated tools**: `male_only_tools` (comma-separated tool names) are
  enforced below the model — gated tools return a polite refusal unless the
  last voice verdict is the male speaker. Fails closed on uncertain/stale
  verdicts. Convenience gating, not biometric auth.

## 0.6.0

> ⚠️ **This update has two parts — please update both:**
> 1. **This add-on** (the update you're installing now).
> 2. **The Voice PE firmware** — open **ESPHome Device Builder** and click **Update** (or **Install**) on your device.
>
> The device and the add-on use one shared protocol; updating only one half can cause odd behaviour.

A reliability and voice-control polish release.

**Stop word**

- **Saying "stop" now usually works on the first try.** The spoken "stop" could
  previously be answered by the assistant a moment later, so you sometimes had to
  repeat it; that follow-on reply is now cancelled, so a single "stop" is
  typically enough.
- **Saying "stop" during a web search returns the device to rest promptly** — the
  light ring no longer keeps showing the "replying" animation for several seconds.
- **Fewer accidental stops** on the assistant's own speech.
- The light ring briefly flashes **red** to confirm your "stop" was registered. *(firmware)*

**Reliability**

- **No more unresponsive sessions.** A silently dropped connection to OpenAI is
  now detected and repaired within seconds, instead of leaving the assistant deaf
  until a restart.
- **The roughly hourly reconnect now happens proactively during a quiet moment**,
  so it practically never interrupts a conversation.
- **Smart-home commands are no longer cancelled** if you keep talking while they run.
- The light can no longer get **stuck on "thinking"**, and long web searches get
  all the time they need.

**No more "answers out of nowhere"**

- The assistant no longer occasionally replies — or repeats its previous answer —
  right after the wake word when you said nothing.
- A sentence that got cut off is no longer answered minutes later on your next wake.

**Settings**

- New **"Wake mic delay"** setting: a short pause after the wake chime before the
  mic opens, so the chime can't be mistaken for speech (default 700 ms).
- The **"Follow-up mic delay"** default is now **700 ms**. Existing installs keep
  their saved value — raise yours if the assistant ever answers right after its
  own reply.

## 0.5.0

A big stable release: everything built and tested on the dev channel over the
past days. **Also update the Voice PE firmware** (v1.1.0 — one click in ESPHome
Builder) to get the full effect of the "stop" improvements; the two halves
work best together.

- **"Stop" now works through the whole reply AND the after-reply listening
  window.** The device detects the word more reliably, and the bridge treats
  it as authoritative: in-flight audio is discarded and an answer OpenAI had
  already started for the stop word itself is cancelled on arrival — no more
  "Okay, I'll be quiet" replies to your "stop".
- **Fixed: an answer could cut off mid-sentence, after which the assistant
  went deaf** until the next reconnect. Harmless protocol races (e.g. your
  sentence being split into two turns by a pause) no longer kill the session.
- **Fixed an audio race that could inject noise/hiss into replies** (firmware,
  paired with this release).
- **Mute behaves properly now** (firmware): the ring goes dark with red
  markers by the microphones, and muting also ends an open listening window
  immediately — both from Home Assistant and with the physical side switch.
- **The LED Ring switch in Home Assistant works again** (firmware): entity off
  = device dark at rest; entity on = the gentle "ready" pulse.
- **Completely reworked Configuration tab**: options grouped logically
  (Basics → Model & voice → Conversation → Web search → Audio →
  Home Assistant → Advanced), every description rewritten in plain practical
  language, and a full Dutch translation included (shown automatically when
  your HA is set to Dutch). Confusing or broken switches were removed; rarely
  needed expert fields stay hidden until you need them.
- **The add-on now has its own icon.**
- Friendlier defaults for new installs: follow-up mic delay 200 ms and
  playback buffer 150 ms. **Existing installs keep their saved values** — if
  yours still say 0, consider setting 200/150 manually (Conversation / Audio
  groups) for fewer ghost triggers and less crackle.

### Heads-up: the firmware stub template was improved

The per-device stub in ESPHome Builder used to reference the firmware in a
form that lets ESPHome **cache the downloaded YAML for a day** — clicking
Update shortly after a release could then silently rebuild yesterday's code.
The stub templates in the firmware repo are fixed; existing users can apply
the same fix once by replacing **only the `packages:` block** in their
device's YAML in ESPHome Builder (everything else — your name, secrets,
`dashboard_import` — stays exactly the same):

```yaml
packages:
  realtime:
    url: https://github.com/TristanBrotherton/voicepe-realtime-firmware
    ref: main
    files: [home-assistant-voice.realtime.yaml]
    refresh: 0s
```

Current templates for reference:
[esphome-builder.dhcp.yaml](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.dhcp.yaml) ·
[esphome-builder.static-ip.yaml](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.static-ip.yaml)

## 0.4.26

- **Web search is now ON by default**, using **gpt-5.5** (the best-quality search
  model), so the assistant can look things up online — weather, news, facts — out
  of the box. **Existing installs keep their saved setting**: if you had it off,
  switch `enable_web_search` on (and set `web_search_model` to `gpt-5.5`) in the
  add-on Configuration. The cheaper mini/nano models stay available.

## 0.4.25

- **Fix:** the first thing you said in the few seconds right after an automatic
  reconnect (e.g. after the 60-minute session cap) could be ignored
  (`conversation_already_has_active_response`). The reconnected session no longer
  creates a duplicate response, so that turn answers normally.

## 0.4.24

- **Renamed** to **OpenAI Realtime 2 Voice Agent**.
- Rewrote the store/info description and added a full **Documentation** tab
  (install steps, OpenAI key, Home Assistant MCP setup, recommended settings, web
  search, credits). Removed stale text from the original upstream client.
- Default system prompt is now an English, voice-tuned prompt (silent tool calls,
  varied confirmations, language pinning). Your own saved prompt is not changed.
- Default `follow_up_open_delay_ms` and `playback_prebuffer_ms` set to `0` (raise
  them if the device hears its own tail or you hear crackle).

## 0.4.23

- **Fix:** the 60-minute session cap sometimes left the session dead until a
  restart. It now reconnects automatically in all cases (both the keepalive-drop
  and the `session_expired` forms).

## 0.4.22

- **New options:** voice **speed** (0.25–1.5), **max reply length**
  (`max_output_tokens`), and **input noise reduction** (off / near-field /
  far-field). All default to current behaviour.

## 0.4.21

- Model, voice, web-search-model and transcription-model options are now
  **dropdowns** with the known-good values, each with a **custom** entry if you
  need a value not in the list.

## 0.4.20

- **New:** optional **web search**. Turn on `enable_web_search` to let the
  assistant look things up online (weather, news, facts). Uses your OpenAI key;
  off by default. Model configurable via `web_search_model` (default gpt-5.4-mini).

## 0.4.19

- Clarified the MCP option help text for both the built-in HA MCP Server and the
  unofficial ha-mcp add-on.

## 0.4.18

- **Fix:** removed a meaningless filler reply ("I'm ready to continue…") that could
  appear on the first turn of a session.

## 0.4.17

- **Fix:** cap restored conversation history (`max_context_messages`, default 12) to
  bound per-turn token cost and avoid hitting OpenAI's rate limit.

## 0.4.16

- **Fix:** the device no longer gets stuck blinking "thinking" after a turn-ending
  error (e.g. a rate limit) — it returns to idle so you can retry.

## 0.4.14

- **New:** `playback_prebuffer_ms` jitter buffer to reduce occasional crackle at the
  start of replies.

## 0.4.12 – 0.4.13

- **Fix:** "say stop, then immediately ask again → silence". Disabled the broken
  server-side audio truncation that wedged the next turn.

## 0.4.9 – 0.4.11

- **New:** auto-reconnect the OpenAI Realtime session when its connection drops
  (keepalive timeout / 60-minute cap), instead of going dead until a restart.
  Refined so a normal device disconnect doesn't trigger an unnecessary reconnect.

## 0.4.6 – 0.4.8

- **New:** configurable post-reply **follow-up listening window** (answer back
  without re-saying the wake word) + its open-delay, and per-option help text in the
  UI.
- **New:** the assistant's and user's transcripts are logged to the add-on log
  (`🤖 assistant:` / `🗣️ user:`).

## 0.4.0 – 0.4.4

- **Fix:** resample the device's 16 kHz mic to the 24 kHz OpenAI requires (garbled
  speech), and drop empty audio chunks.
- **New:** device **"stop"** interrupt now actually cancels the reply and clears
  buffered audio.

## 0.3.x

- Switched the target to **gpt-realtime-2**, pinned pipecat-ai 0.0.97, and tuned
  turn detection (semantic VAD), phase delivery to the device, and the startup
  sequence to stop double-responses. Made the disconnect tool and transcription
  model configurable.

## Earlier

- Initial pipecat + WebSocket implementation (forked from
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)).
