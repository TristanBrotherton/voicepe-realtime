"""Lightweight voice gender classifier: median F0 via YIN.

Pure numpy, for 2-4 s of 16 kHz mono PCM16 command audio from the Voice PE
(XMOS-processed). Returns (label, median_f0, voiced_frames) where label is
"male" / "female" / "uncertain".

Uses the YIN cumulative-mean-normalized difference function with an absolute
threshold, which prefers the true fundamental over harmonics (plain
autocorrelation octave-doubles on some voices — seen with piper's ryan).

Thresholds: typical adult male F0 85-155 Hz, female 165-255 Hz. The 150-165 Hz
gap maps to "uncertain" so borderline voices hedge instead of misfiring.
"""
import numpy as np

SAMPLE_RATE = 16000
FRAME = 640          # 40 ms — two periods of a 50 Hz fundamental
HOP = 160            # 10 ms
F0_MIN, F0_MAX = 60.0, 350.0
TAU_MIN = int(SAMPLE_RATE / F0_MAX)   # ~45
TAU_MAX = int(SAMPLE_RATE / F0_MIN)   # ~266
YIN_THRESHOLD = 0.15
MALE_MAX_HZ = 150.0
FEMALE_MIN_HZ = 165.0
MIN_VOICED_FRAMES = 12


def _frame_f0_yin(frame: np.ndarray) -> float:
    """YIN F0 for one frame, or 0.0 if unvoiced/aperiodic."""
    frame = frame - frame.mean()
    if np.dot(frame, frame) < 1e-6:
        return 0.0
    n = len(frame)
    # difference function d(tau) for tau in [0, TAU_MAX]
    max_tau = min(TAU_MAX + 1, n)
    # d(tau) = sum (x[j] - x[j+tau])^2 = r(0) + r_tau(0) - 2*corr(tau)
    x = frame
    cumsum2 = np.concatenate(([0.0], np.cumsum(x * x)))
    corr = np.correlate(x, x, mode="full")[n - 1:n - 1 + max_tau]
    d = np.empty(max_tau)
    for tau in range(max_tau):
        # energy of x[0:n-tau] and x[tau:n]
        e1 = cumsum2[n - tau] - cumsum2[0]
        e2 = cumsum2[n] - cumsum2[tau]
        d[tau] = e1 + e2 - 2.0 * corr[tau]
    # cumulative mean normalized difference
    cmnd = np.ones(max_tau)
    running = 0.0
    for tau in range(1, max_tau):
        running += d[tau]
        cmnd[tau] = d[tau] * tau / running if running > 0 else 1.0
    # first tau under threshold (prefers the fundamental over harmonics)
    tau_est = 0
    for tau in range(TAU_MIN, max_tau - 1):
        if cmnd[tau] < YIN_THRESHOLD:
            while tau + 1 < max_tau - 1 and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            tau_est = tau
            break
    if tau_est == 0:
        return 0.0
    # parabolic refinement
    t = tau_est
    if 1 <= t < max_tau - 1:
        a, b, c = cmnd[t - 1], cmnd[t], cmnd[t + 1]
        denom = a - 2 * b + c
        if abs(denom) > 1e-12:
            t = t + 0.5 * (a - c) / denom
    f0 = SAMPLE_RATE / t
    if not (F0_MIN <= f0 <= F0_MAX):
        return 0.0
    return f0


def classify_gender(pcm16, sample_rate: int = SAMPLE_RATE):
    """Classify speaker gender from mono PCM16 (bytes) or float32 audio."""
    if isinstance(pcm16, (bytes, bytearray)):
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        audio = pcm16.astype(np.float32)
    if sample_rate != SAMPLE_RATE:
        raise ValueError("expected 16 kHz audio")
    if audio.size < FRAME:
        return "uncertain", 0.0, 0
    n_frames = 1 + (audio.size - FRAME) // HOP
    rms = np.empty(n_frames)
    for i in range(n_frames):
        seg = audio[i * HOP:i * HOP + FRAME]
        rms[i] = np.sqrt(np.mean(seg * seg))
    gate = max(rms.max() * 0.15, 1e-4)
    f0s = []
    for i in range(n_frames):
        if rms[i] < gate:
            continue
        f0 = _frame_f0_yin(audio[i * HOP:i * HOP + FRAME])
        if f0 > 0:
            f0s.append(f0)
    if len(f0s) < MIN_VOICED_FRAMES:
        return "uncertain", 0.0, len(f0s)
    med = float(np.median(f0s))
    if med <= MALE_MAX_HZ:
        return "male", med, len(f0s)
    if med >= FEMALE_MIN_HZ:
        return "female", med, len(f0s)
    return "uncertain", med, len(f0s)
