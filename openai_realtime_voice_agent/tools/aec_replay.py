"""Replay an AEC_SPARA recording through the software canceller, offline (raawr US-060).

    SOFT_AEC=1 python tools/aec_replay.py /data/aec_spara/aec_20261012_203012.wav [--ut ren.wav]

The wav is stereo 16 kHz: left = the raw mic, right = the reference as the canceller saw it. Prints the level per second
before and after (dBFS), so a live test can be judged without anyone talking in the room again."""
import argparse
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SOFT_AEC", "1")
from app.mjuk_aec import FRAME, RATE, MjukAec  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--ut", help="write the cleaned mic to this wav")
    a = ap.parse_args()
    with wave.open(a.wav) as w:
        assert w.getnchannels() == 2 and w.getframerate() == RATE, "an AEC_SPARA recording (16 kHz stereo)"
        d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)
    mic, ref = d[:, 0], d[:, 1]
    m = MjukAec()
    ut = []
    for i in range(0, len(mic) - FRAME, FRAME):
        m.ap.process_reverse_stream(ref[i:i + FRAME].tobytes())
        ren = m.ap.process_stream(mic[i:i + FRAME].tobytes())
        if m.agc_max_db > 0:
            ren = m._agc(np.frombuffer(ren, dtype=np.int16)).tobytes()
        ut.append(np.frombuffer(ren, dtype=np.int16))
    ut = np.concatenate(ut)
    db = lambda x: 20 * np.log10(max(float(np.sqrt(np.mean(x.astype(np.float64) ** 2))), 1.0) / 32768)
    print(" s   mic   efter   ref")
    for s in range(len(ut) // RATE):
        sl = slice(s * RATE, (s + 1) * RATE)
        print(f"{s:3d} {db(mic[sl]):6.1f} {db(ut[sl]):6.1f} {db(ref[sl]):6.1f}")
    if a.ut:
        with wave.open(a.ut, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
            w.writeframes(ut.tobytes())


if __name__ == "__main__":
    main()
