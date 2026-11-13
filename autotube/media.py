# -*- coding: utf-8 -*-
"""FFmpeg / Pillow building blocks: clips, motion, audio mix, subtitles, thumbnail."""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config


class MediaError(RuntimeError):
    pass


def run(args, cwd=None, timeout=3600):
    cmd = [config.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"] + [str(a) for a in args]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise MediaError((p.stderr or "ffmpeg failed")[-1500:])
    return p


def duration(path) -> float:
    p = subprocess.run([config.ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or "")
    if not m:
        raise MediaError(f"길이를 읽을 수 없음: {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def _wh():
    w, h = config.load_settings()["production"]["resolution"].split("x")
    return int(w), int(h)


def _fps():
    return int(config.load_settings()["production"]["fps"])


VENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def image_clip(img, seconds, out, motion=0):
    """Still image -> moving clip (slow zoom / pan, 4 variants so scenes don't look identical)."""
    w, h = _wh()
    fps = _fps()
    frames = max(1, int(round(seconds * fps)))
    bw, bh = int(w * 1.4) // 2 * 2, int(h * 1.4) // 2 * 2
    variants = [
        f"zoompan=z='min(zoom+0.0007,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        f"zoompan=z='if(eq(on,0),1.18,max(zoom-0.0007,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        f"zoompan=z=1.15:x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'",
        f"zoompan=z=1.15:x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'",
    ]
    vf = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
          f"{variants[motion % 4]}:d={frames}:s={w}x{h}:fps={fps},setsar=1,format=yuv420p")
    run(["-loop", "1", "-i", img, "-t", f"{seconds:.3f}", "-vf", vf, "-r", fps, *VENC, "-an", out])
    return out


def video_clip(src, seconds, out):
    """Stock/AI video -> exact length (loops short clips), muted, normalised."""
    w, h = _wh()
    fps = _fps()
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps},setsar=1,format=yuv420p"
    run(["-stream_loop", "-1", "-i", src, "-t", f"{seconds:.3f}", "-vf", vf, *VENC, "-an", out])
    return out


def color_clip(seconds, out, text="", color="0x101820"):
    w, h = _wh()
    fps = _fps()
    vf = "format=yuv420p"
    if text:
        txt = Path(out).with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        vf = drawtext_filter(txt, size=int(h * 0.075), y="(h-text_h)/2", box=False) + ",format=yuv420p"
    run(["-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}", "-t", f"{seconds:.3f}", "-vf", vf, *VENC, out])
    return out


def _ff_path(p):
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def drawtext_filter(textfile, size=64, y="h*0.08", box=True):
    font = config.font_path()
    parts = [f"drawtext=textfile='{_ff_path(textfile)}'"]
    if font:
        parts.append(f"fontfile='{_ff_path(font)}'")
    parts += [f"fontsize={size}", "fontcolor=white", "borderw=4", "bordercolor=black@0.8",
              "x=(w-text_w)/2", f"y={y}"]
    if box:
        parts += ["box=1", "boxcolor=black@0.35", "boxborderw=18"]
    return ":".join(parts)


def overlay_text(clip, text, out):
    if not text:
        return clip
    _, h = _wh()
    txt = Path(out).with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    run(["-i", clip, "-vf", drawtext_filter(txt, size=int(h * 0.06)), *VENC, "-an", out])
    return out


def pad_audio(src, seconds, out):
    run(["-i", src, "-af", f"apad=whole_dur={seconds:.3f},atrim=0:{seconds:.3f}", "-ar", "44100", "-ac", "2", out])
    return out


def concat(files, out, list_path, copy=True):
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '%s'\n" % str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''"))
    args = ["-f", "concat", "-safe", "0", "-i", list_path]
    args += ["-c", "copy"] if copy else [*VENC]
    run(args + [out])
    return out


def mix_music(narration, music, out, music_volume=0.14):
    """Music under narration with automatic ducking (sidechain) and fade out."""
    total = duration(narration)
    fade_start = max(0.0, total - 3)
    fc = (f"[0:a]asplit=2[n1][n2];[1:a]volume={music_volume},aformat=sample_rates=44100:channel_layouts=stereo[m];"
          f"[m][n1]sidechaincompress=threshold=0.04:ratio=10:attack=15:release=350[duck];"
          f"[n2][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
          f"afade=t=out:st={fade_start:.2f}:d=3[a]")
    run(["-i", narration, "-stream_loop", "-1", "-i", music, "-filter_complex", fc, "-map", "[a]",
         "-t", f"{total:.3f}", "-c:a", "pcm_s16le", out])
    return out


def _ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def proportional_words(text, seconds):
    words = text.split()
    total = sum(len(w) for w in words) or 1
    t, out = 0.05, []
    span = max(0.1, seconds - 0.1)
    for w in words:
        d = span * len(w) / total
        out.append((t, t + d, w))
        t += d
    return out


def build_srt(scene_words, path, max_chars=18, max_seconds=3.2):
    """scene_words: [(scene_offset_seconds, [(start,end,word),...]), ...]"""
    cues = []
    for offset, words in scene_words:
        cur, cs, ce = [], None, None
        for a, b, w in words:
            if cs is None:
                cs = a
            if cur and (len(" ".join(cur + [w])) > max_chars or b - cs > max_seconds):
                cues.append((offset + cs, offset + ce, " ".join(cur)))
                cur, cs = [], a
            cur.append(w)
            ce = b
            if w.endswith((".", "?", "!", "…")) and len(" ".join(cur)) >= 6:
                cues.append((offset + cs, offset + ce, " ".join(cur)))
                cur, cs = [], None
        if cur:
            cues.append((offset + cs, offset + ce, " ".join(cur)))
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(cues, 1):
            f.write(f"{i}\n{_ts(a)} --> {_ts(max(b, a + 0.4))}\n{t}\n\n")
    return path, len(cues)


def final_mux(video, audio, out, srt=None, burn=True, cwd=None):
    args = ["-i", video, "-i", audio]
    if srt and burn:
        cwd = cwd or str(Path(out).parent)
        font = config.font_path()
        fonts_dir = Path(cwd) / "fonts"
        if font:
            fonts_dir.mkdir(exist_ok=True)
            target = fonts_dir / Path(font).name
            if not target.exists():
                shutil.copy(font, target)
        # libass scales SRT styles to a 288-pixel-high canvas, so sizes are resolution independent.
        style = (f"FontName={config.font_name()},FontSize=18,Bold=1,PrimaryColour=&H00FFFFFF,"
                 f"OutlineColour=&H00000000,BorderStyle=1,Outline=1.6,Shadow=0,MarginV=20")
        vf = f"subtitles={Path(srt).name}:fontsdir=fonts:force_style='{style}'"
        try:
            run([*args, "-vf", vf, *VENC, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                 Path(out).name if Path(out).parent == Path(cwd) else out], cwd=cwd)
            return out, True
        except MediaError:
            pass  # libass missing -> deliver without burned subtitles
    run([*args, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
    return out, False


def thumbnail(background, text, out, size=(1280, 720)):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    W, H = size
    if background and os.path.isfile(background):
        img = Image.open(background).convert("RGB")
        r = max(W / img.width, H / img.height)
        img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1))
        left, top = (img.width - W) // 2, (img.height - H) // 2
        img = img.crop((left, top, left + W, top + H))
    else:
        img = Image.new("RGB", size, (16, 24, 32))
    shade = Image.new("L", size, 0)
    d = ImageDraw.Draw(shade)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=int(170 * max(0, (y - H * 0.35) / (H * 0.65))))
    img = Image.composite(Image.new("RGB", size, (0, 0, 0)), img, shade)
    draw = ImageDraw.Draw(img)
    text = (text or "").strip()
    lines = [text] if len(text) <= 8 else [text[:len(text) // 2 + (len(text) % 2)], text[len(text) // 2 + (len(text) % 2):]]
    font_file = config.font_path()
    fs = 150 if len(lines) == 1 else 120
    while fs > 40:
        font = ImageFont.truetype(font_file, fs) if font_file else ImageFont.load_default()
        widths = [draw.textbbox((0, 0), ln, font=font, stroke_width=8)[2] for ln in lines]
        if max(widths) <= W * 0.9:
            break
        fs -= 8
    y = H - 60 - len(lines) * (fs + 12)
    for ln in lines:
        w = draw.textbbox((0, 0), ln, font=font, stroke_width=8)[2]
        draw.text(((W - w) // 2, y), ln, font=font, fill=(255, 230, 0), stroke_width=8, stroke_fill=(0, 0, 0))
        y += fs + 12
    img.save(out, "JPEG", quality=90)
    return out
