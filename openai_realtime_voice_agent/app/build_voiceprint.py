"""Build a voice-print centroid from enrollment audio.

Usage (inside the add-on container):
    python3 -m app.build_voiceprint <name> /share/voice-enrollment/<file>.wav [...]

Embeds each file in ~3 s chunks, averages, normalizes, writes
/share/voice-prints/<name>.json.
"""
import json
import os
import sys
import time
import wave

import numpy as np

from . import voiceprint


def build(name: str, files: list) -> dict:
    """Build and write the centroid; returns {"ok", "chunks", "path"|"error"}.
    Called automatically after enrollment, and by the CLI below."""
    name = name.strip().lower()
    vecs = []
    for path in files:
        with wave.open(path) as f:
            assert f.getframerate() == 16000, path
            pcm = f.readframes(f.getnframes())
        step = 16000 * 2 * 3  # 3 s
        for i in range(0, max(1, len(pcm) - step), step):
            chunk = pcm[i:i + step]
            # skip near-silent chunks
            a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            if np.sqrt(np.mean(a * a)) < 0.005:
                continue
            v = voiceprint.embed(chunk)
            if v is not None:
                vecs.append(v)
    if len(vecs) < 5:
        return {"ok": False, "chunks": len(vecs),
                "error": f"only {len(vecs)} usable chunks — need more audio"}
    c = np.mean(vecs, axis=0)
    c = c / np.linalg.norm(c)
    os.makedirs(voiceprint.PRINTS_DIR, exist_ok=True)
    out = os.path.join(voiceprint.PRINTS_DIR, f"{name}.json")
    if os.path.exists(out):
        # A new session replaces the print; keep the old one so a worse
        # recording can be undone by renaming the backup back.
        os.replace(out, f"{out}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    json.dump({"name": name, "embedding": c.tolist(), "chunks": len(vecs)}, open(out, "w"))
    return {"ok": True, "chunks": len(vecs), "path": out}


def main():
    r = build(sys.argv[1], sys.argv[2:])
    if not r["ok"]:
        print("ERROR: " + r["error"])
        sys.exit(1)
    print(f"wrote {r['path']} from {r['chunks']} chunks")


if __name__ == "__main__":
    main()
