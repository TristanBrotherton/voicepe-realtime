"""Short silent check: one recorded minute of what the engine was (or would be) given, sent at real time with its own
activity detection on. How many times does it think somebody spoke? usage: rum.py <start HIGH|LOW> fil.pcm [fil.pcm ...]"""
import asyncio, json, os, sys, time
from google import genai
from google.genai import types as T
async def ett(client, fil, start):
    pcm = open(fil, "rb").read()
    cfg = T.LiveConnectConfig(response_modalities=["AUDIO"],
        speech_config=T.SpeechConfig(voice_config=T.VoiceConfig(prebuilt_voice_config=T.PrebuiltVoiceConfig(voice_name="Charon")), language_code="sv-SE"),
        system_instruction="Du är Björn, en svensk röstassistent. Svara ALLTID på svenska och med högst tre ord.",
        input_audio_transcription=T.AudioTranscriptionConfig(), output_audio_transcription=T.AudioTranscriptionConfig(),
        realtime_input_config=T.RealtimeInputConfig(automatic_activity_detection=T.AutomaticActivityDetection(disabled=False,
            start_of_speech_sensitivity=f"START_SENSITIVITY_{start}", end_of_speech_sensitivity="END_SENSITIVITY_HIGH", prefix_padding_ms=300, silence_duration_ms=800)))
    ut = dict(fil=os.path.basename(fil), start=start, turer=[], hort=[], sa="", ut_s=0.0)
    async with client.aio.live.connect(model=os.environ.get("GEMINI_MODEL", "models/gemini-3.8-live"), config=cfg) as s:
        t0 = time.monotonic(); nu = lambda: round(time.monotonic() - t0, 2)
        async def ta():
            ny = True
            while True:
                async for m in s.receive():
                    sc = m.server_content
                    if not sc: continue
                    if sc.model_turn:
                        for p in sc.model_turn.parts or []:
                            if p.inline_data and p.inline_data.data:
                                if ny: ut["turer"].append(nu()); ny = False
                                ut["ut_s"] += len(p.inline_data.data) / 48000
                    if sc.interrupted or sc.turn_complete: ny = True
                    if sc.input_transcription and sc.input_transcription.text: ut["hort"].append([nu(), sc.input_transcription.text])
                    if sc.output_transcription and sc.output_transcription.text: ut["sa"] += sc.output_transcription.text
        r = asyncio.create_task(ta()); nasta = time.monotonic()
        for i in range(0, len(pcm), 640):
            await s.send_realtime_input(audio=T.Blob(data=pcm[i:i + 640].ljust(640, b"\0"), mime_type="audio/pcm;rate=16000"))
            nasta += 0.02; await asyncio.sleep(max(0, nasta - time.monotonic()))
        await asyncio.sleep(2.0); ut["langd"] = nu(); r.cancel()
    return ut
async def main():
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for u in await asyncio.gather(*[ett(c, f, sys.argv[1]) for f in sys.argv[2:]]):
        print(json.dumps(u, ensure_ascii=False))
asyncio.run(main())
