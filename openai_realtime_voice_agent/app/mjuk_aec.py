"""Software echo canceller in the agent (raawr US-060, path B). OFF unless SOFT_AEC=1.

The XMOS canceller on the Voice PE removes ~1.5 dB (measured 2026-10-12), so the agent does it: WebRTC AEC3
(`aec-audio-processing`, `pip install aec-audio-processing`) on the mic stream, with what the agent itself sends to
the speaker as the reference. Measured offline on live captures (XMOS AGC off): ~30 dB, a spoken "vänta" over the echo
loses 3-6 dB and is still heard.

The reference is placed on a play-back-to-back timeline starting when the audio is SENT, with no extra delay: the device
plays it 0.1-0.25 s later, and AEC3 finds that lag itself. Measured: a reference that leads the echo by up to 200 ms
keeps ~30 dB; one that arrives after the echo gives 2-5 dB. So lead, never lag.

After the canceller comes a software AGC (same flag), because the XMOS AGC had to go (it pumps the echo up to speech
level): a slow gain, only ever UP (max SOFT_AGC_DB, default 24, 0 = off), that moves speech toward -20 dBFS and holds still
on anything near the noise floor, so distant speech is not lost under the -36 dBFS floor of the echo net.

Safe by construction: if the library is missing or anything throws, the mic goes on untouched.
"""
import logging
import os
import time
import wave
from typing import Callable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

RATE = 16000
FRAME = RATE // 100           # AEC3 works on 10 ms
IDLE_BYPASS_S = 1.5           # no reference for this long: nothing to cancel, spare the CPU
AGC_MAL_DB = -20.0            # where the AGC wants speech (RMS of a 10 ms frame, dBFS)
AGC_ATTACK_S = 0.05           # gain goes DOWN this fast (a loud word must not clip)
AGC_RELEASE_S = 0.25          # and UP this slowly (a word is short; the hold below keeps it between words)
AGC_GOLV_MARGINAL_DB = 9.0    # a frame this far over the noise floor counts as speech
AGC_MIN_DB = -44.0            # ...but never a frame quieter than this


def soft_aec_paa() -> bool:
    return os.environ.get("SOFT_AEC", "0") == "1"


def aec_spara_paa() -> bool:
    return os.environ.get("AEC_SPARA", "0") == "1"


class MjukAec:
    def __init__(self, klocka: Callable[[], float] = time.monotonic) -> None:
        self.klocka = klocka
        self.ap = None
        if soft_aec_paa():
            from aec_audio_processing import AudioProcessor

            self.ap = AudioProcessor(enable_aec=True, enable_ns=False, enable_agc=False, enable_vad=False)
            self.ap.set_stream_format(RATE, 1)
            self.ap.set_reverse_stream_format(RATE, 1)
            self.ap.set_stream_delay(0)
        self.tidslinje: List[Tuple[float, np.ndarray]] = []   # (start time, 16 kHz samples) of what the speaker plays
        self.ref_slut = 0.0
        self.rest = np.zeros(0, dtype=np.int16)               # mic samples that did not fill a 10 ms frame yet
        self.trasig = False
        self.aec_pa = soft_aec_paa()                  # False = only record (AEC_SPARA), the mic passes untouched
        self.spara = aec_spara_paa()
        self.spar_katalog = os.environ.get("AEC_SPARA_KATALOG", "/data/aec_spara")
        self.spar_max = int(os.environ.get("AEC_SPARA_MAX", "30"))
        self.spar_mic: List[np.ndarray] = []
        self.spar_ref: List[np.ndarray] = []
        self.spar_full = False
        self.agc_max_db = float(os.environ.get("SOFT_AGC_DB", "24"))
        self.agc_db = 0.0                 # the gain now
        self.golv_db = -50.0              # noise floor estimate (slow minimum of the frame levels)

    def _agc(self, ram: np.ndarray) -> np.ndarray:
        """Gain one 10 ms frame (int16). Up only when the frame is speech-like, else hold and drift back to 0 dB."""
        x = ram.astype(np.float32)
        niva = 20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1.0) / 32768)
        # floor: falls fast to a quiet frame, climbs slowly (a minute of speech must not become the "floor")
        self.golv_db = niva if niva < self.golv_db else self.golv_db + 0.0005 * (niva - self.golv_db) + 0.0002
        tal = niva > max(self.golv_db + AGC_GOLV_MARGINAL_DB, AGC_MIN_DB)
        onskad = min(max(AGC_MAL_DB - niva, 0.0), self.agc_max_db) if tal else None
        dt = FRAME / RATE
        if onskad is None:
            self.agc_db -= self.agc_db * dt / 6.0                               # drift back toward 0 dB when nothing is said (~6 s)
        elif onskad < self.agc_db:
            self.agc_db += (onskad - self.agc_db) * min(1.0, dt / AGC_ATTACK_S)
        else:
            self.agc_db += (onskad - self.agc_db) * min(1.0, dt / AGC_RELEASE_S)
        g = 10 ** (self.agc_db / 20)
        return np.clip(x * g, -32768, 32767).astype(np.int16)

    _per_enhet: dict = {}

    @classmethod
    def for_enhet(cls, enhet: str) -> Optional["MjukAec"]:
        """One canceller per device, kept across reconnects: what it has learnt of the room is not thrown away every time
        the link drops (a cold canceller let the first reply through as an interruption in the offline test)."""
        if enhet not in cls._per_enhet or cls._per_enhet[enhet] is None:
            cls._per_enhet[enhet] = cls.skapa()
        a = cls._per_enhet[enhet]
        if a is not None:
            a.rest = np.zeros(0, dtype=np.int16)
        return a

    @classmethod
    def skapa(cls) -> Optional["MjukAec"]:
        """The canceller, or None when the flag is off or the library is not there (never an error)."""
        if not (soft_aec_paa() or aec_spara_paa()):
            return None
        try:
            return cls()
        except Exception as e:
            logger.warning(f"⚠️ SOFT_AEC=1 but the echo canceller could not start ({e!r}); the mic is left as it is")
            return None

    def ref(self, pcm: bytes, rate: int = 24000, t: Optional[float] = None) -> None:
        """Audio the agent sends to the speaker (PCM16 mono at `rate`)."""
        try:
            a = np.frombuffer(pcm, dtype=np.int16)
            if len(a) == 0:
                return
            if rate != RATE:
                n = int(len(a) * RATE / rate)
                a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a.astype(np.float32)).astype(np.int16)
            t = self.klocka() if t is None else t
            start = max(t, self.ref_slut)          # the device plays chunks back to back
            self.ref_slut = start + len(a) / RATE
            self.tidslinje.append((start, a))
            while self.tidslinje and self.tidslinje[0][0] + len(self.tidslinje[0][1]) / RATE < t - 2.0:
                self.tidslinje.pop(0)
        except Exception as e:
            self._fel(e)

    def _ref_ram(self, t0: float) -> np.ndarray:
        ram = np.zeros(FRAME, dtype=np.int16)
        t1 = t0 + FRAME / RATE
        for start, a in self.tidslinje:
            slut = start + len(a) / RATE
            if slut <= t0 or start >= t1:
                continue
            i0 = int(round((max(start, t0) - start) * RATE))
            i1 = int(round((min(slut, t1) - start) * RATE))
            o0 = int(round((max(start, t0) - t0) * RATE))
            seg = a[i0:i1][: FRAME - o0]
            ram[o0:o0 + len(seg)] = seg
        return ram

    def process(self, pcm: bytes, t: Optional[float] = None) -> bytes:
        """Mic audio (PCM16 mono 16 kHz) that ended at time `t`. Returns the cleaned audio; up to 10 ms is held back."""
        if self.trasig:
            return pcm
        try:
            t = self.klocka() if t is None else t
            aktiv = self.ref_slut >= t - IDLE_BYPASS_S     # something is (or just was) playing: cancel it
            a = np.concatenate([self.rest, np.frombuffer(pcm, dtype=np.int16)])
            tot = len(a)
            ut: List[bytes] = []
            i = 0
            while i + FRAME <= tot:
                ram = a[i:i + FRAME]
                refr = None
                if aktiv and (self.aec_pa or self.spara):
                    refr = self._ref_ram(t - (tot - i) / RATE)
                if self.spara and refr is not None:
                    self.spar_mic.append(ram.copy())
                    self.spar_ref.append(refr)
                if not self.aec_pa:
                    ut.append(ram.tobytes())
                    i += FRAME
                    continue
                if aktiv:
                    self.ap.process_reverse_stream(refr.tobytes())
                    ren = self.ap.process_stream(ram.tobytes())
                else:
                    ren = ram.tobytes()
                if self.agc_max_db > 0:
                    ren = self._agc(np.frombuffer(ren, dtype=np.int16)).tobytes()
                ut.append(ren)
                i += FRAME
            if self.spara:
                self._spara_flush(aktiv)
            self.rest = a[i:]
            return b"".join(ut)
        except Exception as e:
            self._fel(e)
            return pcm

    def _spara_flush(self, aktiv: bool) -> None:
        """AEC_SPARA=1: the raw mic (left) and the reference as the canceller saw it (right), 16 kHz stereo wav, one
        file per spell of playback. For replaying a live test offline. The mic is a person's voice: delete after use."""
        n = len(self.spar_mic)
        if n == 0 or (aktiv and n < 9000):      # keep collecting while it plays, up to 90 s
            return
        mic, ref = np.concatenate(self.spar_mic), np.concatenate(self.spar_ref)
        self.spar_mic, self.spar_ref = [], []
        if len(mic) < 2 * RATE or self.spar_full:
            return
        try:
            os.makedirs(self.spar_katalog, exist_ok=True)
            if len([f for f in os.listdir(self.spar_katalog) if f.endswith(".wav")]) >= self.spar_max:
                self.spar_full = True
                logger.info("AEC_SPARA: the folder is full; nothing more is saved")
                return
            namn = os.path.join(self.spar_katalog, time.strftime("aec_%Y%m%d_%H%M%S.wav"))
            with wave.open(namn, "wb") as w:
                w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
                w.writeframes(np.stack([mic, ref], axis=1).astype(np.int16).tobytes())
        except Exception as e:
            logger.warning(f"AEC_SPARA: could not save ({e!r})")

    def _fel(self, e: Exception) -> None:
        if not self.trasig:
            logger.warning(f"⚠️ echo canceller failed ({e!r}); switched off, the mic goes on untouched")
        self.trasig = True
