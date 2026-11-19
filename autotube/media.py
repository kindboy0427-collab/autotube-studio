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
    try:
        run(["-stream_loop", "-1", "-i", src, "-t", f"{seconds:.3f}", "-vf", vf, *VENC, "-an", out])
    except MediaError:
        # RGB/ProRes-4444 style sources with no colour-space tag: declare bt709 and convert explicitly
        vf2 = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709,format=yuv444p," + vf
        run(["-stream_loop", "-1", "-i", src, "-t", f"{seconds:.3f}", "-vf", vf2, *VENC, "-an", out])
    return out


def color_clip(seconds, out, text="", color="0x101820"):
    w, h = _wh()
    fps = _fps()
    vf = "format=yuv420p"
    if text:
        txt = Path(out).with_suffix(".txt")
        text = _wrap2(text, 18)  # long titles go on two lines and always fit the frame
        longest = max(text.split("\n"), key=len)
        txt.write_text(text, encoding="utf-8")
        vf = drawtext_filter(txt, size=fit_size(longest, w * 0.86, int(h * 0.075)), y="(h-text_h)/2", box=False) + ",format=yuv420p"
    run(["-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}", "-t", f"{seconds:.3f}", "-vf", vf, *VENC, out])
    return out


def _ff_path(p):
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def drawtext_filter(textfile, size=64, y="h*0.08", box=True):
    font = config.font_path()
    parts = [f"drawtext=textfile='{_ff_path(textfile)}'", "expansion=none"]  # '%' in text (99.9%) must stay literal
    if font:
        parts.append(f"fontfile='{_ff_path(font)}'")
    parts += [f"fontsize={size}", "fontcolor=white", "borderw=4", "bordercolor=black@0.8",
              "x=(w-text_w)/2", f"y={y}"]
    if box:
        parts += ["box=1", "boxcolor=black@0.35", "boxborderw=18"]
    return ":".join(parts)


def fit_size(text, max_width, size):
    """Font size so one line never runs off the frame (Korean glyphs ~1.0 em, Latin/digits ~0.6 em)."""
    em = sum(1.0 if ord(c) > 0x2E80 else 0.62 for c in (text or "")) or 1
    return max(18, min(int(size), int(max_width / em)))


def overlay_text(clip, text, out):
    if not text:
        return clip
    w, h = _wh()
    txt = Path(out).with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    run(["-i", clip, "-vf", drawtext_filter(txt, size=fit_size(text, w * 0.9, int(h * 0.06))), *VENC, "-an", out])
    return out


def chapter_intro(clip, label, title, out, seconds=2.8):
    """Full-screen chapter title over the scene's own picture (dimmed): small gold step label + big white title,
    fading in/out. Narration keeps playing, so the scene length does not change."""
    w, h = _wh()
    font = config.font_path()
    ff = f":fontfile='{_ff_path(font)}'" if font else ""
    T = float(seconds)
    alpha = f"if(lt(t,0.45),t/0.45,if(lt(t,{T - 0.45}),1,max(0,({T}-t)/0.45)))"
    tf = Path(out).with_suffix(".ct.txt")
    lf = Path(out).with_suffix(".cl.txt")
    tf.write_text(title, encoding="utf-8")
    vf = (f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.62:t=fill:enable='lt(t,{T})',"
          f"drawbox=x=iw*0.08:y=ih*0.585:w=iw*0.84:h=4:color=0xFFD54F@0.95:t=fill:enable='lt(t,{T})'")
    if label:
        lf.write_text(label, encoding="utf-8")
        vf += (f",drawtext=textfile='{_ff_path(lf)}':expansion=none{ff}:fontsize={fit_size(label, w * 0.8, int(h * 0.085))}:"
               f"fontcolor=0xFFD54F:borderw=3:bordercolor=black@0.8:x=(w-text_w)/2:y=h*0.33:alpha='{alpha}'")
    vf += (f",drawtext=textfile='{_ff_path(tf)}':expansion=none{ff}:fontsize={fit_size(title, w * 0.84, int(h * 0.12))}:"
           f"fontcolor=white:borderw=5:bordercolor=black@0.85:x=(w-text_w)/2:y=h*0.43:alpha='{alpha}'")
    run(["-i", clip, "-vf", vf, *VENC, "-an", out])
    return out


def quote_card(clip, text, by, out):
    """Lower-third quote card: “발언” + — 이름, 연도 (for interview-style citations)."""
    _, h = _wh()
    t1 = Path(out).with_suffix(".q1.txt")
    t2 = Path(out).with_suffix(".q2.txt")
    t1.write_text("“" + text.strip() + "”", encoding="utf-8")
    t2.write_text(by.strip(), encoding="utf-8")
    font = config.font_path()
    ff = f":fontfile='{_ff_path(font)}'" if font else ""
    vf = (f"drawbox=x=0:y=ih*0.70:w=iw:h=ih*0.22:color=black@0.55:t=fill,"
          f"drawtext=textfile='{_ff_path(t1)}':expansion=none{ff}:fontsize={fit_size(text, _wh()[0] * 0.92, int(h * 0.045))}:fontcolor=white:"
          f"x=(w-text_w)/2:y=h*0.735,"
          f"drawtext=textfile='{_ff_path(t2)}':expansion=none{ff}:fontsize={int(h * 0.032)}:fontcolor=0xFFD54F:"
          f"x=(w-text_w)/2:y=h*0.835")
    run(["-i", clip, "-vf", vf, *VENC, "-an", out])
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


def _wrap2(text, per_line=24):
    """Balanced 1-2 line wrap at a space nearest the middle (a sentence is shown whole, never cut)."""
    if len(text) <= per_line:
        return text
    words = text.split(" ")
    if len(words) < 2:
        return text
    best, bd = 1, 10 ** 9
    for k in range(1, len(words)):
        d = abs(len(" ".join(words[:k])) - len(" ".join(words[k:])))
        if d < bd:
            best, bd = k, d
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def build_srt(scene_words, path, max_chars=60, max_seconds=12.0):
    """One cue = one whole sentence (two balanced lines). Only a very long sentence is split, at a comma or the
    middle, into phrases that are each still complete phrases. scene_words: [(offset, [(start,end,word),...]), ...]"""
    cues = []

    def emit(off, ws):
        if not ws:
            return
        text = " ".join(w for _, _, w in ws)
        if len(text) <= max_chars and ws[-1][1] - ws[0][0] <= max_seconds:
            cues.append((off + ws[0][0], off + ws[-1][1], _wrap2(text)))
            return
        # split at the comma/connective nearest the middle, else at the middle word
        cut, bd, tot = None, 10 ** 9, len(text)
        run = 0
        for k, (_, _, w) in enumerate(ws[:-1]):
            run += len(w) + 1
            pref = 0 if w.endswith((",", "、")) else (3 if w.endswith(("고", "며", "만", "는데", "지만", "서")) else 9)
            d = abs(run - tot / 2) + pref * 2
            nxt = ws[k + 1][2]
            if re.search(r"[A-Za-z0-9]", w) and re.search(r"[A-Za-z0-9]", nxt):
                d += 60  # never split inside a name or a number ("Redwood | Research", "3.5 | Sonnet")
            if d < bd:
                cut, bd = k + 1, d
        emit(off, ws[:cut])
        emit(off, ws[cut:])

    for offset, words in scene_words:
        sent = []
        for a, b, w in words:
            sent.append((a, b, w))
            if w.endswith((".", "?", "!", "…", "。")) and len(" ".join(x[2] for x in sent)) >= 4:
                emit(offset, sent)
                sent = []
        emit(offset, sent)
    # each cue stays up until the next one starts (no flicker/gaps inside a scene's flow)
    fixed = []
    for k, (a, b, t) in enumerate(cues):
        nxt = cues[k + 1][0] if k + 1 < len(cues) else None
        end = max(b + 0.25, a + 1.2)
        if nxt is not None:
            end = min(end, nxt - 0.02) if nxt - 0.02 > a + 0.5 else end
        fixed.append((a, end, t))
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(fixed, 1):
            f.write(f"{i}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n")
    return path, len(fixed)


LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"  # YouTube plays back at about -14 LUFS: every video equally loud


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
        style = (f"FontName={config.font_name()},FontSize=15,Bold=1,PrimaryColour=&H00FFFFFF,"
                 f"OutlineColour=&H00000000,BorderStyle=1,Outline=1.6,Shadow=0,MarginV=20")
        vf = f"subtitles={Path(srt).name}:fontsdir=fonts:force_style='{style}'"
        try:
            run([*args, "-vf", vf, *VENC, "-af", LOUDNORM, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                 Path(out).name if Path(out).parent == Path(cwd) else out], cwd=cwd)
            return out, True
        except MediaError:
            pass  # libass missing -> deliver without burned subtitles
    run([*args, "-c:v", "copy", "-af", LOUDNORM, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
    return out, False


def vertical_short(video, audio, start, seconds, out, srt=None, top_text="", size=(1080, 1920), cwd=None):
    """A piece of the finished long-form -> 9:16 Short.
    The 16:9 picture stays whole in the middle (on-screen text, quote cards and chapter titles are not cut off),
    a blurred, darkened copy fills the rest; a caption sits on top and the subtitles under the picture."""
    W, H = size
    fps = _fps()
    cwd = cwd or str(Path(out).parent)
    fade_out = max(0.0, seconds - 0.8)
    vf = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,"
          f"eq=brightness=-0.18[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=0:(H-h)/2,fps={fps},setsar=1")
    if top_text:
        tf = Path(cwd) / (Path(out).stem + "_top.txt")
        tf.write_text(_wrap2(top_text, 12), encoding="utf-8")
        longest = max(tf.read_text(encoding="utf-8").split("\n"), key=len)
        vf += "," + drawtext_filter(tf, size=fit_size(longest, W * 0.88, int(H * 0.05)), y="h*0.11", box=True)
    sub = ""
    if srt:
        font = config.font_path()
        if font:
            fonts_dir = Path(cwd) / "fonts"
            fonts_dir.mkdir(exist_ok=True)
            target = fonts_dir / Path(font).name
            if not target.exists():
                shutil.copy(font, target)
        # 288-high libass canvas: MarginV 62 puts the lines just under the picture, above the Shorts buttons
        style = (f"FontName={config.font_name()},FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,"
                 f"OutlineColour=&H00000000,BorderStyle=1,Outline=1.4,Shadow=0,MarginV=62,MarginL=14,MarginR=14")
        sub = f",subtitles={Path(srt).name}:fontsdir=fonts:force_style='{style}'"
    af = f"afade=t=in:st=0:d=0.15,afade=t=out:st={fade_out:.2f}:d=0.8,{LOUDNORM}"
    base = ["-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(Path(video).resolve()),
            "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(Path(audio).resolve())]
    tail = ["-map", "[v]", "-map", "1:a", *VENC, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
            str(Path(out).resolve())]
    if sub:
        try:
            run([*base, "-filter_complex", vf + sub + "[v]", "-af", af, *tail], cwd=cwd)
            return out, True
        except MediaError:
            pass  # libass missing -> no burned subtitles
    run([*base, "-filter_complex", vf + "[v]", "-af", af, *tail], cwd=cwd)
    return out, False


def thumbnail(background, text, out, size=(1280, 720), variant=0):
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
    # variant 0: yellow bottom-centre / 1: white top-left / 2: red-white bottom-left (A/B candidates)
    color = [(255, 230, 0), (255, 255, 255), (255, 70, 60)][variant % 3]
    y = 60 if variant % 3 == 1 else H - 60 - len(lines) * (fs + 12)
    for k, ln in enumerate(lines):
        w = draw.textbbox((0, 0), ln, font=font, stroke_width=8)[2]
        x = (W - w) // 2 if variant % 3 == 0 else 60
        fill = color if not (variant % 3 == 2 and k) else (255, 255, 255)
        draw.text((x, y), ln, font=font, fill=fill, stroke_width=8, stroke_fill=(0, 0, 0))
        y += fs + 12
    img.save(out, "JPEG", quality=90)
    return out


def plain_video(path):
    """False for motion-graphics/overlay files (ProRes 4444, QuickTime RLE, alpha channel): they are not real footage,
    often have no colour-space tag (conversion errors) and are usually unrelated to the narration."""
    try:
        r = subprocess.run([config.ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return True
    line = next((l for l in r.stderr.splitlines() if "Video:" in l), "")
    low = line.lower()
    if any(c in low for c in ("prores", "qtrle", "png,", "gif,", "rawvideo", "huffyuv")):
        return False
    return not any(f in low for f in ("yuva", "rgba", "argb", "bgra", "gbrap"))

