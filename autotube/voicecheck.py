# -*- coding: utf-8 -*-
"""Narration quality guards.

1 pitch check: estimate each scene's median voice pitch (F0). A scene far from the video's
  typical pitch (e.g. a female-sounding take in a male narration) is a voice switch
  -> the producer regenerates it.
2 loudness: every scene is normalised to the same loudness (EBU R128) so the volume is even.
Pure Python + ffmpeg, no extra packages.
"""
from __future__ import annotations
import array
import statistics
import subprocess

from . import config

RATE = 8000


def _pcm(path, seconds=12.0):
    p = subprocess.run([config.ffmpeg_bin(), "-v", "error", "-i", str(path), "-t", str(seconds), "-ac", "1",
                        "-ar", str(RATE), "-f", "s16le", "-"], capture_output=True)
    a = array.array("h")
    a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    return a


def median_pitch(path):
    """Median F0 in Hz over voiced frames (autocorrelation), or None if mostly silent."""
    x = _pcm(path)
    frame, hop = 320, 400                      # 40 ms frames every 50 ms
    lo, hi = RATE // 400, RATE // 70           # 70..400 Hz
    f0s = []
    for start in range(0, max(0, len(x) - frame - hi), hop):
        seg = x[start:start + frame + hi]
        energy = sum(v * v for v in seg[:frame]) / frame
        if energy < 4e5:                       # silence / breath
            continue
        rs = []
        for lag in range(lo, hi):
            r = 0
            for i in range(0, frame, 2):
                r += seg[i] * seg[i + lag]
            rs.append(r)
        best = max(rs)
        if best <= 0.3 * energy * frame / 2:
            continue
        # first strong local peak = the true period (avoids octave errors at 2x the period)
        for k in range(1, len(rs) - 1):
            if rs[k] >= 0.85 * best and rs[k] >= rs[k - 1] and rs[k] >= rs[k + 1]:
                f0s.append(RATE / (lo + k))
                break
    return statistics.median(f0s) if len(f0s) >= 5 else None


def outliers(pitches, tolerance=0.28):
    """Indexes whose pitch is >tolerance away from the video's median (male<->female ~ +-45 %)."""
    vals = [p for p in pitches if p]
    if len(vals) < 4:
        return []
    med = statistics.median(vals)
    return [i for i, p in enumerate(pitches) if p and abs(p - med) / med > tolerance]


def normalize(src, out, target_lufs=-16):
    """Even loudness for every scene (single-pass loudnorm)."""
    p = subprocess.run([config.ffmpeg_bin(), "-y", "-v", "error", "-i", str(src), "-af",
                        f"loudnorm=I={target_lufs}:TP=-1.5:LRA=7", "-ar", "44100", "-ac", "1", "-b:a", "160k", str(out)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("음량 맞추기 실패: " + p.stderr[-200:])
    return out
