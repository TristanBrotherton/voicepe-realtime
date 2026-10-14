"""Silent probe: recorded mic audio straight into Gemini Live with ITS OWN turn-taking on. No speaker, no house.
usage: sond.py plan.json klippkatalog ut.jsonl [id-prefix ...]"""
import asyncio, json, os, sys, time
from google import genai
from google.genai import types as T

MODEL = os.environ.get("GEMINI_MODEL", "models/gemini-3.8-live")
AAD = {
    "forval": None,
    "idag": dict(start_of_speech_sensitivity="START_SENSITIVITY_HIGH", end_of_speech_sensitivity="END_SENSITIVITY_HIGH", prefix_padding_ms=300, silence_duration_ms=800),
    "lag": dict(start_of_speech_sensitivity="START_SENSITIVITY_LOW", end_of_speech_sensitivity="END_SENSITIVITY_HIGH", prefix_padding_ms=300, silence_duration_ms=800),
    "talig": dict(start_of_speech_sensitivity="START_SENSITIVITY_HIGH", end_of_speech_sensitivity="END_SENSITIVITY_LOW", prefix_padding_ms=300, silence_duration_ms=1500),
}
BIT = 640  # 20 ms at 16 kHz


async def ett(client, p, katalog):
    pcm = open(f"{katalog}/{p['pcm']}.pcm", "rb").read()
    aad = AAD[p.get("aad", "idag")]
    cfg = T.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=T.SpeechConfig(voice_config=T.VoiceConfig(prebuilt_voice_config=T.PrebuiltVoiceConfig(voice_name="Charon")), language_code="sv-SE"),
        system_instruction="Du är Björn, en röstassistent hemma hos Henrik. Du pratar svenska och svarar kort, utom när någon ber om en saga.",
        input_audio_transcription=T.AudioTranscriptionConfig(), output_audio_transcription=T.AudioTranscriptionConfig(),
        realtime_input_config=T.RealtimeInputConfig(automatic_activity_detection=T.AutomaticActivityDetection(disabled=False, **aad)) if aad else None,
    )
    ut = dict(id=p["id"], hand=[], turer=[], inn="", sa="")
    async with client.aio.live.connect(model=MODEL, config=cfg) as s:
        t0 = time.monotonic(); nu = lambda: round(time.monotonic() - t0, 3)
        forsta = asyncio.Event(); klar = asyncio.Event()

        async def ta_emot():
            tur = None
            while True:
                async for m in s.receive():
                    sc = m.server_content
                    if not sc: continue
                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                if tur is None:
                                    tur = dict(start=nu(), sek=0.0); ut["turer"].append(tur); forsta.set()
                                tur["sek"] = round(tur["sek"] + len(part.inline_data.data) / 48000, 3); tur["sist"] = nu()
                    if sc.interrupted: ut["hand"].append([nu(), "avbruten"]); tur = None
                    if sc.generation_complete: ut["hand"].append([nu(), "genererat"])
                    if sc.turn_complete: ut["hand"].append([nu(), "tur_klar"]); tur = None
                    if sc.input_transcription and sc.input_transcription.text: ut["hand"].append([nu(), "in", sc.input_transcription.text]); ut["inn"] += sc.input_transcription.text
                    if sc.output_transcription and sc.output_transcription.text: ut["sa"] += sc.output_transcription.text

        async def skicka():
            if p["typ"] == "over":
                await s.send_realtime_input(text="Berätta en saga om en björn. Den ska vara minst en minut lång.")
            n = 0; i = None; slut = None; nasta = time.monotonic()
            while True:
                if i is None and (p["typ"] == "fraga" and nu() >= 1.0 or p["typ"] == "over" and forsta.is_set() and nu() >= ut["turer"][0]["start"] + 1.0):
                    i = 0; ut["klipp_start"] = nu()
                if i is not None and i < len(pcm):
                    bit = pcm[i:i + BIT].ljust(BIT, b"\0"); i += BIT
                    if i >= len(pcm): slut = nu()
                else:
                    bit = bytes(BIT)
                await s.send_realtime_input(audio=T.Blob(data=bit, mime_type="audio/pcm;rate=16000"))
                if slut is not None and nu() > slut + (8 if p["typ"] == "over" else 10) or nu() > 45: break
                nasta += 0.02; await asyncio.sleep(max(0, nasta - time.monotonic()))
            klar.set()

        r = asyncio.create_task(ta_emot()); k = asyncio.create_task(skicka())
        done, _ = await asyncio.wait([r, k], return_when=asyncio.FIRST_COMPLETED)
        ut["langd"] = nu(); r.cancel(); k.cancel()
        for d in done:
            if d.exception(): ut["fel"] = repr(d.exception())[:200]
    return ut


async def main():
    plan = json.load(open(sys.argv[1])); vill = sys.argv[4:]
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]); tot = 0.0
    with open(sys.argv[3], "a") as f:
        for p in plan:
            if vill and not any(p["id"].startswith(v) for v in vill): continue
            try:
                u = await asyncio.wait_for(ett(client, p, sys.argv[2]), 70)
            except Exception as e:
                u = dict(id=p["id"], fel=repr(e)[:300])
            tot += u.get("langd", 0); f.write(json.dumps(u, ensure_ascii=False) + "\n"); f.flush()
            print(p["id"], "fel" if "fel" in u else "ok", f"{u.get('langd', 0):.0f} s", flush=True)
    print(f"summa {tot/60:.1f} min mot Gemini", flush=True)

asyncio.run(main())
