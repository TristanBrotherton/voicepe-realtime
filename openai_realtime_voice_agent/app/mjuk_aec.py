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
SAMMANHANG_S = 0.25           # STADIG_TAKT: a chunk sent this late still belongs to the same unbroken stream
MIC_PAUS_S = 0.5              # no mic packet for this long: the stream was closed, the next one is a new stretch
MIC_DRIFT = 200e-6            # the device's sample clock may run this much slower than ours
SPELSTART_MIN_S = 0.06        # the device waited at least this long before it played a burst: move the reference with it
ERLE_REF_DB = -45.0           # a frame counts for the ERLE only when the speaker was this loud...
ERLE_MIC_DB = -50.0           # ...and the mic heard at least this much of it
ERLE_MIN_RAMAR = 30           # fewer frames than this (0.3 s) is no measurement
NIVA_FONSTER = 20             # person(): the last 0.2 s...
NIVA_HOGA = 12                # ...with at least 0.12 s of it loud
NIVA_OVER_EKO_DB = float(os.environ.get("NIVA_OVER_EKO_DB", "-2"))   # ...over his echo's loudest frames (95th percentile) plus this
NIVA_OVER_REST_DB = float(os.environ.get("NIVA_OVER_REST_DB", "8"))   # ...or, with a device that reports underruns: over what the canceller leaves of them plus this
NIVA_HAL_S = 0.03             # a hole this long in what was sent is a break (shorter ones the canceller rides out)
NIVA_UNDERRUN_S = 3.0          # ...and this long after the device said its playback broke (42 simulated breaks: 2-3 false cuts at 2 s, 0 at 3 s)
NIVA_SVAR_S = 2.0             # ...and not in the first seconds of a reply, or after a break in it
# Echo tracker (AEC_FOLJ, on by default with SOFT_AEC): every FOLJ_S / 2, over the last FOLJ_S, find where his echo sits in the mic against the
# reference, and when it sits BEFORE it (less than FOLJ_MIN_S behind) twice in a row, move the reference so it leads by
# FOLJ_MAL_S. A reference that leads AEC3 finds itself; only an earlier move that now leads by more than FOLJ_MAX_S is
# taken back (the canceller is kept per device across sessions). One estimate alone is not trusted (replayed
# over a whole morning with one canceller, single estimates at +0.7..+0.9 s from similar replies sent it astray). Kontoret
# 2026-10-14 08:07: after the playback stuttered (a dozen underruns in 4 s) the reference sat 0.49 s AFTER the echo for the
# rest of the session; AEC3 cannot work on a reference that lags, took 0-3 dB, and his own voice reached the engine at
# -21..-24 dBFS (16 of 26 cuts with nobody speaking). Replayed: 0.5-2 dB -> 7-19 dB on 11 of 11 such recordings,
# the 15 good ones untouched.
FOLJ_S = 2.0
FOLJ_MIN_S, FOLJ_MAX_S, FOLJ_MAL_S, FOLJ_SAMMA_S = 0.03, 0.6, 0.15, 0.04
FOLJ_SKARPA = 8.0             # the correlation peak over its mean: below this his voice is not clearly in the mic


def soft_aec_paa() -> bool:
    return os.environ.get("SOFT_AEC", "0") == "1"


def aec_spara_paa() -> bool:
    return os.environ.get("AEC_SPARA", "0") == "1"


ALLT_MAX_S = 900.0


def eko_lag(mic: np.ndarray, ref: np.ndarray) -> Optional[float]:
    """Seconds his echo in `mic` sits after `ref` (negative: the echo comes BEFORE the reference), searched from 0.6 s
    before to 1.0 s after; None when the reference is quiet or the peak is not clear (his voice is not in the mic)."""
    if np.sqrt(np.mean(ref * ref)) < 100 or np.sqrt(np.mean(mic * mic)) < 30:
        return None
    n = 1 << int(np.ceil(np.log2(len(mic) + len(ref))))
    c = np.fft.irfft(np.fft.rfft(mic, n) * np.conj(np.fft.rfft(ref, n)), n)      # c[k] = sum mic[i + k] * ref[i]
    w = np.abs(np.concatenate([c[-int(0.6 * RATE):], c[:int(1.0 * RATE)]]))       # lags -0.6 .. +1.0 s
    k = int(np.argmax(w))
    if w[k] / (w.mean() + 1e-9) < FOLJ_SKARPA:
        return None
    return (k - int(0.6 * RATE)) / RATE


class MjukAec:
    def __init__(self, klocka: Callable[[], float] = time.monotonic) -> None:
        self.klocka = klocka
        self.ap = None
        if soft_aec_paa():
            from aec_audio_processing import AudioProcessor

            # SOFT_AEC_NS=0..3: WebRTC's noise suppressor after the canceller. Offline on the replies of 2026-10-13 (one
            # canceller for the evening, as live): what is left of his voice after the first 2 s of a reply goes from
            # median -58 to -79 dBFS at level 3, and Henrik's voice over the reply keeps its level (4 of 4 clips).
            ns = os.environ.get("SOFT_AEC_NS", "")
            self.ap = AudioProcessor(enable_aec=True, enable_ns=ns in ("0", "1", "2", "3"), ns_level=int(ns) if ns in ("0", "1", "2", "3") else 0,
                                     enable_agc=False, enable_vad=False)
            self.ap.set_stream_format(RATE, 1)
            self.ap.set_reverse_stream_format(RATE, 1)
            self.ap.set_stream_delay(0)
        self.tidslinje: List[Tuple[float, np.ndarray, Optional[int]]] = []   # (start time, 16 kHz samples, its mic sample) of what the speaker plays
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
        # While he speaks the AGC stands still: it lifted what was left of his echo by up to 24 dB and the turn detector
        # took it for speech (live 2026-10-13). SOFT_TAL_DB is a fixed gain for that time instead, 0 = none. It is 0
        # until the canceller takes his echo down on live replies: replayed with 12 dB the echo it leaves today got
        # louder and the speech-to-text wrote stop words out of it.
        self.tal_db = float(os.environ.get("SOFT_TAL_DB", "0"))
        self.matt: List[Tuple[float, float, float, float]] = []   # (frame end, mic dBFS, cleaned dBFS, ref dBFS), last 4 s
        self.svar_start = -1e9   # when what is sent to the speaker last began, or broke (a hole, a hold on the device)
        self.underrun_stod = False   # the device's firmware reports underruns (play_stamp carries "ur": 1)
        # SOFT_AEC_REST=<seconds> (0 = off): nothing is passed on while the canceller has not locked on this reply --
        # its first seconds, and after a hole or an underrun (svar_start). The echo's delay differs from reply to reply
        # (102-137 ms measured), so the canceller finds it anew every time, and until it has his own voice gets through:
        # over -40 dBFS in the first 2 s of 9 of 11 replies (up to -16), in 2 of 11 after that (20-30 ms at -37).
        # ponytail: a hard gate, so nobody can cut him off in the first seconds of a reply; let through what is louder
        # than his own echo (as person() does) if that turns out to matter.
        try:
            self.rest_s = max(0.0, float(os.environ.get("SOFT_AEC_REST", "0")))
        except ValueError:
            self.rest_s = 0.0
        # Test only (AEC_SPARA_ALLT=1): everything, not only the replies -- mic, reference and what is passed on, three
        # int16 channels at 16 kHz in one raw file, at most ALLT_MAX_S. To see afterwards what the engine was given.
        self.allt = None
        self.allt_kvar = 0
        if os.environ.get("AEC_SPARA_ALLT", "0") == "1":
            try:
                os.makedirs(self.spar_katalog, exist_ok=True)
                self.allt = open(os.path.join(self.spar_katalog, f"allt_{time.strftime('%Y%m%d_%H%M%S')}_{id(self) % 10000}.pcm"), "wb")
                self.allt_kvar = int(ALLT_MAX_S * RATE / FRAME)
                logger.info(f"🎙️ AEC_SPARA_ALLT: writing mic, reference and output to {self.allt.name} (clock {self.klocka():.3f} = {time.time():.3f})")
            except Exception as e:
                logger.warning(f"⚠️ AEC_SPARA_ALLT: no file ({e!r})")
        self.matt_max = 400
        self.mic_sist: Optional[float] = None     # when the last mic packet came
        self.mic_ankare: Optional[float] = None   # our clock at sample 0 of this stretch of the mic stream
        self.mic_n = 0                            # samples of this stretch so far
        # REF_ENHETSKLOCKA=1 (with STADIG_TAKT=1): an unbroken reply is laid on the device's sample clock, not ours. The
        # speaker and the mic share one clock, so the k-th sample sent plays k samples after the first, counted in mic
        # samples. Ljudprov Kontor 2026-10-15 18:50: laid in our seconds the reference slid 4 ms in 20 s (-200 ppm)
        # against the echo and a long linear canceller took 1-5 dB; with the slide taken out, 16-19 dB, as with the
        # true clip. Only where a reply starts, or after a hole, is our clock used to place it.
        self.ref_klocka = os.environ.get("REF_ENHETSKLOCKA", "0") == "1"
        self.mic_tot = 0                          # mic samples received, all stretches
        self.mic_t: Optional[float] = None        # our clock at mic sample mic_tot
        self.ref_slut_samp: Optional[int] = None  # the mic sample where the reference laid so far ends
        self.ref_frac = 0.0                       # the part of a 16 kHz sample the last resampled chunk did not fill
        self.agc_db = 0.0                 # the gain now
        self.golv_db = -50.0              # noise floor estimate (slow minimum of the frame levels)
        self.folj = os.environ.get("AEC_FOLJ", "1") == "1"
        self.skift_s = 0.0                # the reference is read this much later than its timeline says (the tracker)
        self.folj_mic: List[np.ndarray] = []
        self.folj_ref: List[np.ndarray] = []
        self.folj_forra: Optional[float] = None   # the last estimate that said the reference lags

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
            from app import eko_vakt
            if eko_vakt.egen_tur(enhet):
                a.agc_max_db = 0.0   # the engine runs the turns in this room: it gets the room as it is
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
                if self.ref_klocka:   # counted in samples, so the cut-off fraction is carried, not lost (B on #80)
                    n = int(len(a) * RATE / rate + self.ref_frac)
                    self.ref_frac += len(a) * RATE / rate - n
                a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a.astype(np.float32)).astype(np.int16)
            t = self.klocka() if t is None else t
            # The device plays chunks back to back. With the steady pacer (STADIG_TAKT=1) it also has a cushion, so a
            # chunk that is sent a little late still plays without a hole; without it a late chunk IS a hole.
            sammanhang = SAMMANHANG_S if os.environ.get("STADIG_TAKT", "0") == "1" else 0.0
            start = self.ref_slut if t <= self.ref_slut + sammanhang else t
            if start - self.ref_slut > NIVA_HAL_S:
                self.svar_start = start     # a new reply, or a hole in this one: the echo is not where it was
            samp = None
            if self.ref_klocka:
                samp = self.ref_slut_samp if start == self.ref_slut and self.ref_slut_samp is not None else self._samp(start)
                self.ref_slut_samp = None if samp is None else samp + len(a)
            self.ref_slut = start + len(a) / RATE
            self.tidslinje.append((start, a, samp))
            while self.tidslinje and self.tidslinje[0][0] + len(self.tidslinje[0][1]) / RATE < t - 2.0:
                self.tidslinje.pop(0)
        except Exception as e:
            self._fel(e)

    def spelstart(self, forsening_s: float, t: Optional[float] = None) -> None:
        """The device says it held a burst `forsening_s` before the speaker took it (its jitter buffer at the start of
        a reply, ~0.33 s; firmware play_stamp). The echo comes that much later, for the whole burst: move the reference
        with it. Without this the first reply after a wake is not cancelled at all (measured 2026-10-13: 470 ms from
        reference to echo on the first reply, 120-170 ms on the following)."""
        try:
            if forsening_s < SPELSTART_MIN_S or forsening_s > 2.0:
                return
            t = self.klocka() if t is None else t
            self.svar_start = t
            fran = t - forsening_s - 0.15            # the burst began about this long ago
            d = int(round(forsening_s * RATE))
            for i, (start, a, samp) in enumerate(self.tidslinje):
                if start >= fran:
                    self.tidslinje[i] = (start + forsening_s, a, None if samp is None else samp + d)
            if self.ref_slut >= fran:
                self.ref_slut += forsening_s
                if self.ref_slut_samp is not None:
                    self.ref_slut_samp += d
        except Exception as e:
            self._fel(e)

    def erle(self, t0: float, t1: float, minst: int = ERLE_MIN_RAMAR, sista: Optional[int] = None) -> Optional[float]:
        """How many dB the canceller took off his echo between t0 and t1 (energy in / energy out over the frames where
        the speaker played and the mic heard it). `sista`: only the last so many of those frames (he pauses between
        sentences, and the first second of a reply is before the canceller found its place). None = fewer than `minst`
        frames, too little to tell. Measured BEFORE a candidate starts it says whether a loud sound after the canceller
        can be trusted to be a person."""
        ramar = [(mic_db, ren_db) for t, mic_db, ren_db, ref_db in self.matt
                 if t0 <= t <= t1 and ref_db > ERLE_REF_DB and mic_db > ERLE_MIC_DB]
        if sista:
            ramar = ramar[-sista:]
        if len(ramar) < minst:
            return None
        # The median frame, not the energy sum: a word the person said in the window is loud in and loud out, and as a
        # sum it hid that the canceller worked (live 2026-10-13 12:07: 3.9 dB as a sum, 30.8 dB as the median frame, so
        # Henrik's second "vänta" was taken for echo).
        return float(np.median([m - r for m, r in ramar]))

    def underrun(self, t: Optional[float] = None) -> None:
        """The device says its speaker ran out of audio mid-reply (firmware "underrun"): what it plays from here on
        comes later than the reference says, and the canceller needs a moment to find it again."""
        self.svar_start = (self.klocka() if t is None else t) + NIVA_UNDERRUN_S - NIVA_SVAR_S

    def person(self, niva_db: float, tillit_db: float = 20.0, over_db: Optional[float] = None,
               rest_db: Optional[float] = None) -> bool:
        """A person is talking over him, right now, told by the level alone: at least NIVA_HOGA of the last NIVA_FONSTER
        frames out of the canceller are louder than his own echo gets in the mic (its 95th percentile over the last
        seconds, plus `over_db`), and never under `niva_db`. The canceller must have been working just before (the
        median frame, see erle) and the reply must be NIVA_SVAR_S old with no break in what was sent.

        Why "louder than his own echo": when the device's playback slips mid-reply the canceller loses him for a second
        and what comes out is his echo at up to its full level. Nothing measured tells that from a voice (waveform and
        spectrum against the reference, a fresh filter fit, the echo's delay: all overlap, 2026-10-13), but it can not be
        louder than the echo was. 84 simulated slips on clean recordings: 37 false at a fixed -36 dBFS, 4 at the echo's
        loudest frames -4 dB, 0 at -2 dB (and Henrik's words: 10 of 12 found at -2, 6 at 0).
        No speech-to-text and no turn detector: the detector missed Henrik's "stopp" three times of seven (12:07)."""
        try:
            m = self.matt
            if len(m) < NIVA_FONSTER + ERLE_MIN_RAMAR:
                return False
            nu = m[-1][0]
            if nu - self.svar_start < NIVA_SVAR_S:
                return False
            t1 = m[-NIVA_FONSTER][0] - 0.1
            # His echo alone: the frames the canceller took at least 10 dB off (a word of the person's earlier in the
            # window is loud in AND out, and would lift the floor over the person's next word).
            fore = [mic for t, mic, ren, ref in m
                    if t1 - 4.0 <= t <= t1 and ref > ERLE_REF_DB and mic > ERLE_MIC_DB and mic - ren >= 10.0]
            if len(fore) < 80:
                return False
            e = self.erle(t1 - 4.0, t1, 80, 150)
            if e is None or e < tillit_db:
                return False
            topp = float(np.percentile(fore, 95))
            if self.underrun_stod and over_db is None:
                # The device says when its playback breaks (underrun -> svar_start), so a slip is known by its cause
                # and the floor need not be his whole echo: it is what the canceller leaves of his loudest frames
                # right now, plus a margin. A canceller at 30 dB leaves the fixed floor; one still finding its place
                # after a break (20-25 dB) gets a higher one.
                golv = max(niva_db, topp - e + (NIVA_OVER_REST_DB if rest_db is None else rest_db))
            else:
                golv = max(niva_db, topp + (NIVA_OVER_EKO_DB if over_db is None else over_db))
            return sum(1 for _, _, ren, _ in m[-NIVA_FONSTER:] if ren > golv) >= NIVA_HOGA
        except Exception as e:
            self._fel(e)
            return False

    @staticmethod
    def _db(x: np.ndarray) -> float:
        x = x.astype(np.float32)
        return float(20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1.0) / 32768))

    def _samp(self, t: float) -> Optional[int]:
        """The mic sample that is heard at our time `t` (None before the mic has streamed)."""
        return None if self.mic_t is None else self.mic_tot + int(round((t - self.mic_t) * RATE))

    def _ref_ram(self, t0: float, s0: Optional[int] = None) -> np.ndarray:
        """The reference for the mic frame that begins at our time `t0`, or at mic sample `s0` (REF_ENHETSKLOCKA)."""
        ram = np.zeros(FRAME, dtype=np.int16)
        t1 = t0 + FRAME / RATE
        for start, a, samp in self.tidslinje:
            if s0 is not None and samp is not None:
                i0, i1 = max(s0 - samp, 0), min(s0 + FRAME - samp, len(a))
                if i1 > i0:
                    ram[samp + i0 - s0:samp + i1 - s0] = a[i0:i1]
                continue
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
            # The mic is one unbroken 16 kHz stream, but its packets arrive in clumps (the network). Taking each
            # packet's arrival as its time moved the reference +-20 ms against the mic within one reply, and the
            # canceller took 2 dB where a steady reference took 32 (measured live 2026-10-13 09:35). So the time of a
            # sample is counted from the stream itself: an anchor (the packet that came through fastest) plus the
            # samples since. A pause in the stream starts a new anchor.
            n_in = len(pcm) // 2
            if self.mic_sist is None or t - self.mic_sist > MIC_PAUS_S:
                self.mic_ankare, self.mic_n = None, 0
            self.mic_sist = t
            self.mic_n += n_in
            kand = t - self.mic_n / RATE
            if self.mic_ankare is None or kand < self.mic_ankare:
                self.mic_ankare = kand
            else:
                self.mic_ankare += min(kand - self.mic_ankare, MIC_DRIFT * n_in / RATE)   # a slow device clock, no more
            t = self.mic_ankare + self.mic_n / RATE
            ny_stracka = self.mic_n == n_in
            self.mic_tot += n_in
            self.mic_t = t
            if self.ref_klocka and ny_stracka:
                # the mic was closed: what was laid by the old count is placed again by our clock
                self.tidslinje = [(s, a_, self._samp(s)) for s, a_, _ in self.tidslinje]
                self.ref_slut_samp = self._samp(self.ref_slut)
            a = np.concatenate([self.rest, np.frombuffer(pcm, dtype=np.int16)])
            tot = len(a)
            ut: List[bytes] = []
            i = 0
            while i + FRAME <= tot:
                ram = a[i:i + FRAME]
                refr = None
                if aktiv and (self.aec_pa or self.spara):
                    s0 = self.mic_tot - tot + i + int(round(self.skift_s * RATE)) if self.ref_klocka else None
                    refr = self._ref_ram(t - (tot - i) / RATE + self.skift_s, s0)
                    if self.folj and self.aec_pa:
                        self._folj(ram, refr)
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
                    renad = np.frombuffer(ren, dtype=np.int16)
                    slut = t - (tot - i - FRAME) / RATE
                    self.matt.append((slut, self._db(ram), self._db(renad), self._db(refr)))
                    if len(self.matt) > self.matt_max:
                        del self.matt[:-self.matt_max]
                    if self.rest_s and slut - self.svar_start < self.rest_s:
                        ren = bytes(len(ren))
                    elif self.tal_db:
                        ren = np.clip(renad.astype(np.float32) * 10 ** (self.tal_db / 20), -32768, 32767).astype(np.int16).tobytes()
                else:
                    ren = ram.tobytes()
                    if self.agc_max_db > 0:
                        ren = self._agc(np.frombuffer(ren, dtype=np.int16)).tobytes()
                ut.append(ren)
                if self.allt is not None and self.allt_kvar > 0:
                    self.allt_kvar -= 1
                    self.allt.write(np.stack([ram, refr if refr is not None else np.zeros(FRAME, np.int16),
                                              np.frombuffer(ren, dtype=np.int16)], axis=1).astype(np.int16).tobytes())
                    if self.allt_kvar % 100 == 0:
                        self.allt.flush()
                i += FRAME
            if self.spara:
                self._spara_flush(aktiv)
            self.rest = a[i:]
            return b"".join(ut)
        except Exception as e:
            self._fel(e)
            return pcm

    def _folj(self, ram: np.ndarray, refr: np.ndarray) -> None:
        """The echo tracker: collect FOLJ_S of mic and reference, then move the reference if the echo is not where AEC3
        can find it (it needs the reference to lead)."""
        self.folj_mic.append(ram.copy())
        self.folj_ref.append(refr)
        if len(self.folj_mic) * FRAME < FOLJ_S * RATE:
            return
        mic = np.concatenate(self.folj_mic).astype(np.float64)
        ref = np.concatenate(self.folj_ref).astype(np.float64)
        halv = len(self.folj_mic) // 2           # a sliding window: an estimate every FOLJ_S / 2
        self.folj_mic, self.folj_ref = self.folj_mic[halv:], self.folj_ref[halv:]
        lag = eko_lag(mic, ref)
        if lag is None or FOLJ_MIN_S <= lag <= FOLJ_MAX_S or (lag > FOLJ_MAX_S and self.skift_s <= 0):
            self.folj_forra = None          # where AEC3 finds it; too far ahead only undoes an earlier move
            return
        if self.folj_forra is None or abs(lag - self.folj_forra) > FOLJ_SAMMA_S:
            self.folj_forra = lag           # once is not enough
            return
        self.folj_forra = None
        self.folj_mic, self.folj_ref = [], []     # what was collected was read at the old place
        self.skift_s += FOLJ_MAL_S - lag
        logger.info(f"🎯 echo canceller: his echo sat {lag * 1000:+.0f} ms behind the reference; reference moved "
                        f"{(FOLJ_MAL_S - lag) * 1000:+.0f} ms (total {self.skift_s * 1000:+.0f} ms)")

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
