"""
media_tools.py - everything ffmpeg: probing, silence, subtitles, branding a full video, cutting reels.

Brand placement comes from Brand/brand.json -> "backend" block (written by the web app):
    { "video": {on,file_id,width_fraction,margin_fraction,opacity,x,y,when,seconds}, "reel": {...},
      "extras": {intro,outro,endCard,lowerThird: <drive file id or ''>} }
x / y are ready-made ffmpeg overlay expressions (they use main_w / main_h / overlay_w / overlay_h).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("social")

TS = "15360"   # one video timescale everywhere, so pieces concat with -c copy
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
       "-video_track_timescale", TS, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
       "-movflags", "+faststart"]
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def run(cmd, what="ffmpeg", timeout=3600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{what} failed: {(p.stderr or '')[-600:]}")
    return p


def ff(*args, what="ffmpeg"):
    return run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)], what)


# --------------------------------------------------------------------------- probe
def probe(path):
    j = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", str(path)], "ffprobe").stdout)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    fps = fps if 5 < fps < 120 else 30.0
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": round(fps, 3),
            "dur": float(j["format"].get("duration") or v.get("duration") or 0),
            "audio": any(s["codec_type"] == "audio" for s in j["streams"])}


def even(n):
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


# --------------------------------------------------------------------------- silence / timing
def silences(path, noise="-35dB", min_d=0.30):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                        f"silencedetect=noise={noise}:d={min_d}", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=1800)
    out, start = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    return out


def cut_candidates(sil, dur, min_gap=0.55):
    """Midpoints of the longer pauses: safe places to cut a reel without chopping a sentence."""
    return [round((a + b) / 2, 2) for a, b in sil if (b - a) >= min_gap and 1.0 < (a + b) / 2 < dur - 1.0]


# --------------------------------------------------------------------------- subtitles
def _t(sec):
    sec = max(0.0, sec)
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _sec(t):
    h, m, rest = t.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def write_srt(cues):
    return "\n".join(f"{i}\n{_t(a)} --> {_t(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(cues, 1))


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, l in enumerate(lines):
            if "-->" in l:
                a, b = l.split("-->")
                try:
                    cues.append((_sec(a), _sec(b.split()[0]), " ".join(lines[i + 1:]).strip()))
                except ValueError:
                    pass
                break
    return [c for c in cues if c[2]]


def split_sentences(text):
    text = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]|\([^)]*scene[^)]*\)", " ", text or "", flags=re.I)).strip()
    parts = re.split(r"(?<=[.!?۔؟…])\s+", text)
    out = []
    for p in parts:           # very long sentences are split at commas so a subtitle stays readable
        while len(p) > 110:
            cut = max(p.rfind(", ", 0, 100), p.rfind("، ", 0, 100), p.rfind(" ", 0, 100))
            if cut < 40:
                cut = 100
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return [s for s in out if s]


def align_script(script, dur, sil):
    """Script text + real silences -> timed cues. Narration is spoken in order, so each sentence gets time
    in proportion to its length, and every boundary is pulled to the nearest real pause (+-1.5 s)."""
    sents = split_sentences(script)
    if not sents:
        return []
    lead = sil[0][1] if sil and sil[0][0] < 0.3 else 0.0
    tail = sil[-1][0] if sil and sil[-1][1] >= dur - 0.3 else dur
    if tail - lead < 1:
        lead, tail = 0.0, dur
    weights = [len(s) + 8 for s in sents]
    total, acc, bounds = sum(weights), 0, []
    pauses = [(a + b) / 2 for a, b in sil if lead < (a + b) / 2 < tail]
    for w in weights[:-1]:
        acc += w
        b = lead + (tail - lead) * acc / total
        near = min(pauses, key=lambda p: abs(p - b), default=None)
        bounds.append(near if near is not None and abs(near - b) <= 1.5 else b)
    edges = [lead] + bounds + [tail]
    for i in range(1, len(edges)):                      # keep strictly increasing
        edges[i] = max(edges[i], edges[i - 1] + 0.4)
    edges = [min(e, dur) for e in edges]
    return [(edges[i], max(edges[i + 1] - 0.05, edges[i] + 0.3), s) for i, s in enumerate(sents)]


def cues_in_range(cues, a, b):
    """Cues that overlap [a,b], shifted so the clip starts at 0."""
    return [(max(s, a) - a, min(e, b) - a, t) for s, e, t in cues if e > a and s < b]


def extract_audio(video, out):
    ff("-i", video, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", out, what="audio extract")
    return out


# --------------------------------------------------------------------------- reel cut planning
def plan_cuts(dur, count, mode, cands, ranges=None):
    """-> [(start, end)]. mode: equal | chapters | auto (fallback) | manual."""
    if mode == "manual" and ranges:
        return [(float(a), min(float(b), dur)) for a, b in ranges if a < dur]
    count = max(1, int(count))
    if count == 1:
        return [(0.0, dur)]
    step = dur / count
    edges = [0.0]
    for i in range(1, count):
        ideal = step * i
        if mode == "equal" or not cands:
            edges.append(ideal)
            continue
        win = step * 0.45
        near = [c for c in cands if abs(c - ideal) <= win and c > edges[-1] + 5]
        edges.append(min(near, key=lambda c: abs(c - ideal)) if near else ideal)
    edges.append(dur)
    return [(edges[i], edges[i + 1]) for i in range(count) if edges[i + 1] - edges[i] > 1.0]


REEL_MAX_SEC = float(os.environ.get("REEL_MAX_SEC", 89))   # Facebook Reels: 90 s max, so every shared reel stays under it


def fit_reel(a, z, cands, max_sec=None):
    """-> (start, end, speed). Keeps a reel under max_sec.
    Slightly too long (<= 6 %): play it a bit faster (speech stays natural). Much too long: end it at the last safe pause."""
    max_sec = max_sec or REEL_MAX_SEC
    dur = z - a
    if dur <= max_sec:
        return a, z, 1.0
    if dur <= max_sec * 1.06:
        return a, z, dur / max_sec
    near = [c for c in cands if a + max_sec * 0.7 <= c <= a + max_sec]
    return a, (max(near) if near else a + max_sec), 1.0


# --------------------------------------------------------------------------- watermark
def _wm_filter(wm, label_in, label_wm, label_out, base_w, dur):
    """Overlay chain for one placement dict from brand.json."""
    w = even(base_w * float(wm.get("width_fraction") or 0.12))
    op = max(0.0, min(1.0, float(wm.get("opacity") or 1)))
    when, secs = wm.get("when", "always"), float(wm.get("seconds") or 0)
    en = ""
    if when == "first" and secs > 0:
        en = f":enable='between(t,0,{secs})'"
    elif when == "last" and secs > 0:
        en = f":enable='gte(t,{max(dur - secs, 0):.2f})'"
    return (f"[{label_wm}]format=rgba,scale={w}:-1,colorchannelmixer=aa={op}[wmk];"
            f"[{label_in}][wmk]overlay=x='{wm['x']}':y='{wm['y']}'{en}[{label_out}]")


def _norm_segment(src, out, w, h, fps, is_image=False, secs=4.0):
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
          f"setsar=1,fps={fps},format=yuv420p")
    if is_image:
        ff("-loop", "1", "-t", secs, "-i", src, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-vf", vf, "-shortest", "-r", fps, *ENC, out, what="end card")
        return
    pr = probe(src)
    if pr["audio"]:
        ff("-i", src, "-vf", vf, "-r", fps, *ENC, out, what="normalise clip")
    else:
        ff("-i", src, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", vf, "-shortest",
           "-r", fps, *ENC, out, what="normalise clip")


def brand_video(src, out, pr, backend, assets, tmp):
    """Complete video: watermark + lower third on the main part, then intro / outro / end card around it.
    assets: {slot: local path}. Returns (output path, seconds added before the main part = intro length)."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    wm = backend.get("video") or {}
    logo = assets.get("wm_video")
    lower = assets.get("lowerThird")
    inputs, chains, last, idx = ["-i", str(src)], [], "0:v", 1
    if wm.get("on") and logo:
        inputs += ["-i", str(logo)]
        chains.append(_wm_filter(wm, last, f"{idx}:v", "v1", pr["w"], pr["dur"]))
        last, idx = "v1", idx + 1
    if lower and pr["dur"] > 9:
        inputs += ["-i", str(lower)]
        lw = even(pr["w"] * 0.38)
        chains.append(f"[{idx}:v]format=rgba,scale={lw}:-1[lt];"
                      f"[{last}][lt]overlay=x='main_w*0.04':y='main_h-overlay_h-main_h*0.09':"
                      f"enable='between(t,1.5,6.5)'[v2]")
        last, idx = "v2", idx + 1
    main = tmp / "main_branded.mp4"
    if chains:
        ff(*inputs, "-filter_complex", ";".join(chains), "-map", f"[{last}]", "-map", "0:a?", "-r", pr["fps"], *ENC,
           main, what="watermark")
    else:
        shutil.copy(src, main)
    parts, offset = [], 0.0
    if assets.get("intro"):
        p = tmp / "intro.mp4"
        _norm_segment(assets["intro"], p, pr["w"], pr["h"], pr["fps"])
        parts.append(p)
        offset = probe(p)["dur"]
    if parts or assets.get("outro") or assets.get("endCard"):
        # make the main part match the same encode params before concatenation
        m2 = tmp / "main_norm.mp4"
        _norm_segment(main, m2, pr["w"], pr["h"], pr["fps"])
        parts.append(m2)
        if assets.get("outro"):
            p = tmp / "outro.mp4"
            _norm_segment(assets["outro"], p, pr["w"], pr["h"], pr["fps"])
            parts.append(p)
        if assets.get("endCard"):
            p = tmp / "endcard.mp4"
            _norm_segment(assets["endCard"], p, pr["w"], pr["h"], pr["fps"], is_image=True, secs=4.0)
            parts.append(p)
        lst = tmp / "concat.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out, what="concat")
    else:
        shutil.move(str(main), out)
    return Path(out), offset


# --------------------------------------------------------------------------- reels
def _font():
    return next((f for f in FONTS if Path(f).exists()), None)


def render_reel(src, out, start, end, fit, part, parts, part_label, backend, logo_path, W=1080, H=1920, fps=30, speed=1.0):
    wm = backend.get("reel") or {}
    dur = end - start
    speed = max(1.0, min(float(speed or 1.0), 1.1))
    if fit == "crop":
        base = f"[0:v]scale=-2:{H},crop={W}:{H},setsar=1[b0]"
    elif fit == "pad":
        base = f"[0:v]scale={W}:-2,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[b0]"
    else:   # blur (default): the video sits in the middle on a blurred copy of itself
        base = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"boxblur=28:6[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[b0]")
    chains, last, inputs = [base], "b0", ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    if speed > 1.001:
        chains.append(f"[{last}]setpts=PTS/{speed:.5f}[bs]")
        last = "bs"
        dur = dur / speed
    font = _font()
    if part_label and parts > 1 and font:
        chains.append(f"[{last}]drawtext=fontfile={font}:text='Part {part}/{parts}':fontsize=54:fontcolor=white:"
                      f"borderw=4:bordercolor=black@0.7:x=(w-text_w)/2:y=h*0.075[b1]")
        last = "b1"
    if wm.get("on") and logo_path:
        inputs += ["-i", str(logo_path)]
        chains.append(_wm_filter(wm, last, "1:v", "b2", W, dur))
        last = "b2"
    ff(*inputs, "-filter_complex", ";".join(chains), "-map", f"[{last}]", "-map", "0:a?", "-r", fps, *ENC,
       "-af", (f"atempo={speed:.5f}," if speed > 1.001 else "") + "aresample=48000", out, what=f"reel {part}")
    return Path(out)


def make_thumbnail_from_frame(src, out, at):
    ff("-ss", f"{at:.2f}", "-i", src, "-frames:v", "1", "-q:v", "2", out, what="frame grab")
    return out
