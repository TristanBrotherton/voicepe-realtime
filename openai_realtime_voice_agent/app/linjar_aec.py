"""A long linear echo canceller with a double-talk guard and a mild residual suppressor (raawr US-060).
MjukAec runs it instead of WebRTC AEC3 when SOFT_AEC_MOTOR=linjar; numpy only, no library.

Why: offline 2026-10-16 (Henrik's saved turns mixed into the live Kontor pair of 2026-10-15 20:53, REF_ENHETSKLOCKA=1)
AEC3 took 33 dB of the echo and Henrik's words with it: on a clip the local speech-to-text reads as "En riktigt lång
historia om en död som bor i stogen" alone, it wrote "Ligit från studieramerikan som hon är studier" with AEC3, at
levels from 6 dB under his echo to 6 dB over. Its suppressor does that. The long linear filter alone kept more of him
but adapted on his voice. This one, with the suppressor floored at -24 dB: 19 dB of echo, "En riktigt lång historia om ett
äldre som bor i skogen" at -20 dBFS, 0.5-0.65 ms per 10 ms frame on core (ordet/lacka_0500.txt on the Mac). The long
filter without a suppressor let "Tänd i köket. Björn." out of one clip (mild_modul_0430.txt).

  * The filter: partitioned-block frequency-domain NLMS, blocks of 10 ms (one mic frame) and P of them (P=26: 260 ms
    of echo, AEC3 covers ~50 ms past its delay estimate).
  * The guard (Valin 2007, as in Speex): the step is scaled by the residual echo the filter expects over the error it
    gets. The expected residual is a leak factor times the echo estimate, the leak read off how the error's power
    follows the estimate's from block to block; a voice that is not his does not follow it, so the error grows and the
    step falls. Before the filter has once taken 10 dB the estimate means nothing, and it adapts freely. Synthetic, a
    voice 6 dB over his echo for 2 s: 15 dB right after with the guard, 2 dB without. On the real clips, at or under
    his echo, the guard changed little (16-17 dB after his words with it, 12-19 without); the suppressor did the work.
  * The suppressor: per frequency bin, gain max(floor, 1 - lack * |echo estimate|^2 / |error|^2), falling at once and
    rising over two frames. AEC3's goes to silence; this one stops at the floor, so a voice under the echo is dimmed,
    not removed.

Same interface as AEC3's AudioProcessor as MjukAec uses it: process_reverse_stream(10 ms) then process_stream(10 ms).
The output is 10 ms late (the suppressor's overlap).
"""
import numpy as np

N = 160   # one 10 ms frame at 16 kHz


class LinjarAec:
    FORDROJNING = N   # samples the output lags the input (MjukAec pairs its levels with the frame before)

    def __init__(self, P: int = 26, mu: float = 0.5, golv_db: float = -24.0, lack: float = 1.0, vakt: bool = True) -> None:
        self.P, self.mu, self.vakt = P, mu, vakt
        self.W = np.zeros((P, N + 1), complex)
        self.X = np.zeros((P, N + 1), complex)     # the last P reference blocks, newest first
        self.xprev = np.zeros(N)
        self.pw: np.ndarray | None = None
        self.pe = self.pd = 0.0
        self.me = self.my = self.cey = self.cyy = 0.0   # the leak estimate: means and co-variation of the block powers
        self.lart = False                          # the filter has once taken 10 dB: from then on the step is guarded
        self.ref = np.zeros(N)
        self.golv, self.lack = 10 ** (golv_db / 20), lack
        self.w = np.sqrt(np.hanning(2 * N + 1)[:2 * N])
        self.eprev, self.yprev, self.ola = np.zeros(N), np.zeros(N), np.zeros(N)
        self.g: np.ndarray | None = None

    def process_reverse_stream(self, pcm: bytes) -> None:
        self.ref = np.frombuffer(pcm, dtype=np.int16).astype(float)

    def process_stream(self, pcm: bytes) -> bytes:
        d = np.frombuffer(pcm, dtype=np.int16).astype(float)
        e, y = self.block(d, self.ref)
        return np.clip(self.mild(e, y), -32768, 32767).astype(np.int16).tobytes()

    def block(self, d: np.ndarray, x: np.ndarray) -> tuple:
        """One block: mic `d`, reference `x` (float, N each). Returns (mic minus the echo, the echo estimate)."""
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = np.fft.rfft(np.concatenate([self.xprev, x]))
        self.xprev = x
        y = np.fft.irfft((self.W * self.X).sum(axis=0))[N:]
        e = d - y
        p0 = np.abs(self.X[0]) ** 2
        self.pw = p0 if self.pw is None else 0.9 * self.pw + 0.1 * p0
        mu = self.mu
        pe, py = float(np.mean(e ** 2)), float(np.mean(y ** 2))
        a = 0.98
        self.me, self.my = a * self.me + (1 - a) * pe, a * self.my + (1 - a) * py
        self.cey = a * self.cey + (1 - a) * (pe - self.me) * (py - self.my)
        self.cyy = a * self.cyy + (1 - a) * (py - self.my) ** 2
        if self.vakt and self.lart:
            lacka = min(1.0, max(0.005, self.cey / (self.cyy + 1e-9)))
            mu *= min(1.0, lacka * py / (pe + 1.0))
        E = np.fft.rfft(np.concatenate([np.zeros(N), e]))
        G = mu / self.P * np.conj(self.X) * E / (self.pw + 1e-6 * self.pw.mean() + 1.0)
        g = np.fft.irfft(G, axis=1)
        g[:, N:] = 0                                # constrained: the update is a causal filter of N taps per block
        self.W += np.fft.rfft(g, axis=1)
        self.pe = 0.7 * self.pe + 0.3 * float(np.mean(e ** 2))
        self.pd = 0.7 * self.pd + 0.3 * float(np.mean(d ** 2))
        self.lart = self.lart or 10 * self.pe < self.pd
        return e, y

    def mild(self, e: np.ndarray, y: np.ndarray) -> np.ndarray:
        """The residual suppressor on one block, 50 % overlap; returns the PREVIOUS block's samples."""
        E = np.fft.rfft(np.concatenate([self.eprev, e]) * self.w)
        Y = np.fft.rfft(np.concatenate([self.yprev, y]) * self.w)
        self.eprev, self.yprev = e, y
        g = np.clip(1 - self.lack * np.abs(Y) ** 2 / (np.abs(E) ** 2 + 1e-3), self.golv, 1.0)
        self.g = g if self.g is None else np.minimum(g, 0.5 * self.g + 0.5 * g)
        ut = np.fft.irfft(E * self.g) * self.w
        o, self.ola = self.ola + ut[:N], ut[N:]
        return o
