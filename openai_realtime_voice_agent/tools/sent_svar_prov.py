"""Self-test for US-051: the speaker asks before it reads a late answer (raawr US-051, US-052).

Silent: the device is a stand-in on the agent's websocket (like tools/satellit_attrapp.py), comms is a fake HTTP
server this script runs, and nothing reaches a real speaker. Run on core next to a side instance (never the live
agent, and with /root/PROVPLATS set):

    python tools/sent_svar_prov.py --scenario dag --ja ja.wav --nej nej.wav [--agent http://127.0.0.1:18081]
        [--ws ws://127.0.0.1:18080/] [--enhet attrapp] [--enhet-ljud-s 12]
    python tools/sent_svar_prov.py --scenario natt ...   # side instance started with SENT_SVAR_NU=03:00

What the agent must do (US-051 + the door of US-052): POST {agent}/sent-svar {rum, result_id, agent, amne, amnesord,
created_at} -> 202 {"fragar": true|false}; on true the question is spoken (audio frames to the device); a "ja" hears
as POST {comms}/kanal/rost/{rum}/svar {result_id} -> {"text"} and the text is spoken; a "nej" or silence reads nothing.
Port and path of /sent-svar are C's to confirm when built; this script is the one place to change them.

Scenarios (one verdict line each, JSON, exit 0 only if all PASS):
  dag   AC-1 question spoken, no fetch  | AC-2 ja -> fetch + answer audio | nej -> no fetch | silence -> no fetch
        AC-5 no cloud session opened for the question or the ja/nej (journal of --unit has no 'connected to the cloud')
  morgon AC-6 (side instance with SENT_SVAR_NU=12:00): two notices arrive in a conversation (held), the model
        answers a question, then "Ska jag ge dig det jag sparat?" is asked once; ja reads both, oldest first
  natt  AC-3 clock 03:00: fragar:false, no audio, no fetch (the answer stays at Core)
"""
import argparse
import asyncio
import json
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import websockets

FRAME_BYTES, FRAME_S = 640, 0.02
SVAR = "Hockeyresultatet blev tre två."


class FalskComms:
    """Records every request; answers the answer fetch, and 200 {} to anything else (a miss for bana 0)."""

    def __init__(self, port: int):
        self.anrop: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    kropp = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    kropp = {}
                outer.anrop.append((self.path, kropp))
                ut = {"text": SVAR} if self.path.endswith("/svar") else {}
                b = json.dumps(ut).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def hamtningar(self, result_id: str) -> int:
        return sum(1 for p, k in self.anrop if p.endswith("/svar") and k.get("result_id") == result_id)

    def stang(self):
        self.server.shutdown()


def pcm(path: str) -> bytes:
    with wave.open(path) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), "16 kHz mono 16-bit"
        return w.readframes(w.getnframes())


class Enhet:
    """The device stand-in: counts audio bytes per window, can send speech or silence."""

    def __init__(self, ws):
        self.ws, self.bytes, self.text, self.stycken = ws, 0, [], []  # stycken: (time, bytes) per audio frame
        self.las = asyncio.create_task(self._las())

    async def _las(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    self.bytes += len(m)
                    self.stycken.append((time.monotonic(), len(m)))
                else:
                    self.text.append(m[:200])
        except websockets.ConnectionClosed:
            pass

    async def vanta_ljud(self, max_s: float, tyst_s: float = 2.5) -> int:
        """Bytes of audio that arrived until `tyst_s` of quiet after the first sound (or `max_s` without any)."""
        fran, t0, sist, senast = self.bytes, time.monotonic(), None, self.bytes
        while time.monotonic() - t0 < max_s:
            await asyncio.sleep(0.1)
            if self.bytes != senast:
                senast, sist = self.bytes, time.monotonic()
            elif sist is not None and time.monotonic() - sist >= tyst_s:
                break
        return self.bytes - fran

    async def tala(self, ljud: bytes):
        nasta = time.monotonic()
        for i in range(0, len(ljud), FRAME_BYTES):
            await self.ws.send(ljud[i:i + FRAME_BYTES])
            nasta += FRAME_S
            await asyncio.sleep(max(0.0, nasta - time.monotonic()))

    async def tystnad(self, s: float):
        await self.tala(bytes(int(16000 * 2 * s)))


def sant(verdicts: list, namn: str, ok: bool, detalj: str = ""):
    verdicts.append({"prov": namn, "resultat": "PASS" if ok else "FAIL", "detalj": detalj})
    print(json.dumps(verdicts[-1], ensure_ascii=False), flush=True)


def notis(rid: str, rum: str) -> dict:
    return {"rum": rum, "result_id": rid, "agent": "Sixten", "amne": "hockeyresultat",
            "amnesord": ["hockey", "resultat"], "created_at": "2026-10-11T08:00:00+02:00"}


def moln_oppnad(unit: str, sedan: float) -> bool:
    ut = subprocess.run(["journalctl", "-u", unit, "--no-pager", "--since", f"@{int(sedan)}"],
                        capture_output=True, text=True).stdout
    return "connected to the cloud engine" in ut or "cloud session opened" in ut


async def skicka_notis(agent: str, rid: str, rum: str) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(f"{agent}/sent-svar", json=notis(rid, rum))
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}


async def dag(a, comms: FalskComms, v: list):
    ja, nej = pcm(a.ja), pcm(a.nej)
    t_start = time.time()
    async with websockets.connect(f"{a.ws}?device_id={a.enhet}", max_size=None, ping_interval=None) as ws:
        e = Enhet(ws)
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(9.0)  # the agent builds the device's session a few seconds after it connects
        for namn, svar_ljud, forvantat in (("ja", ja, 1), ("nej", nej, 0), ("tystnad", None, 0)):
            rid = f"prov-{namn}-{int(time.time())}"
            kod, kropp = await skicka_notis(a.agent, rid, a.enhet)
            sant(v, f"AC-1 {namn}: notisen tas emot (202 fragar:true)", kod == 202 and kropp.get("fragar") is True,
                 f"{kod} {kropp}")
            if not (kod == 202 and kropp.get("fragar") is True):
                continue  # no door: nothing is asked, so nothing more to say or measure
            fraga = await e.vanta_ljud(a.enhet_ljud_s)
            sant(v, f"AC-1 {namn}: frågan talas", fraga > 16000, f"{fraga} B")
            sant(v, f"AC-1 {namn}: svaret läses inte och inget hämtas", comms.hamtningar(rid) == 0)
            # The agent sends the whole question at once and keeps the mic shut while it plays (+1.2 s); a real
            # device opens the mic after that, so the stand-in speaks only then.
            await asyncio.sleep(fraga / 48000 + 1.0)  # 24 kHz 16-bit mono
            efter_fraga = e.bytes  # everything that arrives from here on is the answer's (or nothing)
            if svar_ljud is not None:
                await e.tala(svar_ljud)
            await e.tystnad(7.0)  # the follow-up window, then the mic closes
            await e.vanta_ljud(a.enhet_ljud_s)
            svar = e.bytes - efter_fraga
            hamtat = comms.hamtningar(rid)
            if forvantat:
                sant(v, "AC-2 ja: svaret hämtas en gång och läses", hamtat == 1 and svar > 16000,
                     f"hämtningar={hamtat}, ljud={svar} B")
            else:
                sant(v, f"AC-2 {namn}: inget hämtas, inget läses", hamtat == 0 and svar < 3200,
                     f"hämtningar={hamtat}, ljud={svar} B")
        ws.transport.abort()
    sant(v, "AC-5: ingen molnsession för frågan eller ja/nej", not moln_oppnad(a.unit, t_start),
         f"journal för {a.unit}")


async def natt(a, comms: FalskComms, v: list):
    async with websockets.connect(f"{a.ws}?device_id={a.enhet}", max_size=None, ping_interval=None) as ws:
        e = Enhet(ws)
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(9.0)  # the agent builds the device's session a few seconds after it connects
        rid = f"prov-natt-{int(time.time())}"
        kod, kropp = await skicka_notis(a.agent, rid, a.enhet)
        sant(v, "AC-3 natt: 202 fragar:false, orsak natt", kod == 202 and kropp == {"fragar": False, "orsak": "natt"},
             f"{kod} {kropp}")
        ljud = await e.vanta_ljud(5.0)
        sant(v, "AC-3 natt: inget talas, inget hämtas", ljud < 3200 and comms.hamtningar(rid) == 0, f"{ljud} B")
        ws.transport.abort()


def omgangar(stycken: list, gap_s: float = 1.0) -> list[tuple[float, float, int]]:
    """Audio frames grouped into bursts: (start, end, bytes); a gap of `gap_s` starts a new one."""
    ut: list = []
    for t, n in stycken:
        if ut and t - ut[-1][1] < gap_s:
            ut[-1] = (ut[-1][0], t, ut[-1][2] + n)
        else:
            ut.append((t, t, n))
    return ut


async def morgon(a, comms: FalskComms, v: list):
    fraga_wav = pcm(a.fraga)
    ja = pcm(a.ja)
    async with websockets.connect(f"{a.ws}?device_id={a.enhet}", max_size=None, ping_interval=None) as ws:
        e = Enhet(ws)
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(9.0)
        await ws.send(json.dumps({"type": "wake"}))
        tal = asyncio.create_task(e.tala(fraga_wav))
        await asyncio.sleep(1.0)  # the conversation is on: both notices are held
        koder = []
        for i in (0, 1):
            kod, kropp = await skicka_notis(a.agent, f"prov-morgon-{i}-{int(time.time())}", a.enhet)
            koder.append((kod, kropp.get("fragar"), kropp.get("orsak")))
        sant(v, "AC-6 notiserna hålls mitt i samtalet", all(k == (202, False, "samtal") for k in koder), str(koder))
        await tal
        await e.tystnad(2.0)  # the turn ends on silence
        await e.vanta_ljud(a.enhet_ljud_s)
        # Wait for the burst after the model's reply: the morning question.
        t0 = time.monotonic()
        while time.monotonic() - t0 < 25 and len(omgangar(e.stycken)) < 2:
            await asyncio.sleep(0.2)
        bursts = omgangar(e.stycken)
        sant(v, "AC-6 frågan kommer efter första svaret", len(bursts) >= 2, f"{len(bursts)} ljudomgångar")
        if len(bursts) < 2:
            return
        fraga_s = bursts[1][2] / 48000
        await asyncio.sleep(max(0.0, bursts[1][1] + fraga_s + 2.5 - time.monotonic()))
        fore = e.bytes
        await e.tala(ja)
        await e.tystnad(7.0)
        await e.vanta_ljud(a.enhet_ljud_s * 2)
        ids = [p for p in comms.anrop if p[0].endswith("/svar")]
        sant(v, "AC-6 ja: båda hämtas, det äldsta först, och läses", len(ids) == 2 and e.bytes - fore > 32000,
             f"hämtningar={[k.get('result_id') for _, k in ids]}, ljud={e.bytes - fore} B")
        await asyncio.sleep(3.0)
        sant(v, "AC-6 frågan kommer bara en gång", len(omgangar(e.stycken)) <= 4, f"{len(omgangar(e.stycken))} omgångar")
        ws.transport.abort()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", choices=("dag", "natt", "morgon"), required=True)
    p.add_argument("--ja", default="/root/q/ja.wav")
    p.add_argument("--nej", default="/root/q/nej.wav")
    p.add_argument("--agent", default="http://127.0.0.1:18081")
    p.add_argument("--ws", default="ws://127.0.0.1:18080/")
    p.add_argument("--fraga", default="/root/q/q1.wav", help="a plain question the model answers (scenario morgon)")
    p.add_argument("--enhet", default="attrapp")
    p.add_argument("--enhet-ljud-s", dest="enhet_ljud_s", type=float, default=12.0)
    p.add_argument("--unit", default="prov-sent-svar")
    p.add_argument("--comms-port", type=int, default=18090)
    a = p.parse_args()
    comms, v = FalskComms(a.comms_port), []
    try:
        asyncio.run({"dag": dag, "natt": natt, "morgon": morgon}[a.scenario](a, comms, v))
    except Exception as ex:  # a missing endpoint is a FAIL line, not a crash
        sant(v, f"{a.scenario}: provet kunde inte köras", False, repr(ex)[:200])
    finally:
        comms.stang()
    return 0 if v and all(x["resultat"] == "PASS" for x in v) else 1


if __name__ == "__main__":
    raise SystemExit(main())
