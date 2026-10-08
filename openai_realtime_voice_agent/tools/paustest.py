"""Which turn-end silence cuts a sentence with a pause in it? (raawr US-032)

Feeds wavs in /root/paus (a<ms>.wav, b<ms>.wav: a sentence with a pause of <ms> ms in
it, made with `say -v Alva "... [[slnc 800]] ..."`) through the agent's own local turn
detector (Silero) at several silence settings. OK = one turn, KAPAD(n) = cut into n.
Measured 2026-10-08: a pause of up to 0.8 s survives 800 ms, one of 1.0 s needs 1000.
Synthetic pauses are clean silence; a real room (hesitation sounds, noise) can differ.

    python tools/paustest.py        # on core; no cloud, no lock needed
"""
import sys, wave, glob, os
sys.path.insert(0, "/opt/raawr-rostagent/app")
from app.providers.local_turns import LocalTurns
def kor(path, silence_ms):
    w = wave.open(path); pcm = w.readframes(w.getnframes()); rate = w.getframerate()
    t = LocalTurns.create(silence_ms, rate)
    ends = starts = 0
    step = int(rate * 0.032) * 2  # 32 ms frames
    pcm += b"\x00\x00" * int(rate * 2.5)  # 2.5 s of quiet after the sentence: the real end
    for i in range(0, len(pcm), step):
        e = t.feed(pcm[i:i + step], rate)
        if e == "end": ends += 1
        if e == "start": starts += 1
    return starts, ends
print("fil", *[f"{s}ms" for s in (1200, 1000, 900, 800, 700, 600)], sep="\t")
for f in sorted(glob.glob("/root/paus/*.wav"), key=lambda p: (os.path.basename(p)[0], int(os.path.basename(p)[1:-4]))):
    row = []
    for s in (1200, 1000, 900, 800, 700, 600):
        st, en = kor(f, s)
        row.append("OK" if en == 1 else f"KAPAD({en})")
    print(os.path.basename(f)[:-4], *row, sep="\t")
