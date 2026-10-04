# Changelog

All notable changes to this add-on. Newest first.

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
