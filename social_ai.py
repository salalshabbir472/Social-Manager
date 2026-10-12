"""
social_ai.py - the Gemini part of the social backend. Uses gemini_client.Gemini, so the key + model rotation
(heavy / medium / light tiers, per-key cooldowns, fallback chain) is exactly the one the video pipeline uses.

Every function degrades gracefully: if there are no keys, or all quota is gone, it returns None / the plain
fallback and the upload carries on with the text the web app already wrote.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from google.genai import types

from social_media import plan_cuts

log = logging.getLogger("social")


def csv_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[\n,]", str(v or "")) if x.strip()]


def make_gemini(frame_cfg):
    """Build Gemini from Application Config/frame_config.json (same keys + models + rpm as the pipeline)."""
    keys = csv_list((frame_cfg.get("keys") or {}).get("gemini"))
    if not keys:
        log.warning("no Gemini keys in frame_config.json: AI tags / reel planning / transcription are off")
        return None
    from gemini_client import Gemini   # lives in the repo root next to this file
    m, lim = frame_cfg.get("models") or {}, frame_cfg.get("limits") or {}
    return Gemini(keys, light_models=csv_list(m.get("light")) or None, code_models=csv_list(m.get("code")) or None,
                  medium_models=csv_list(m.get("medium")) or None, rpm=float(lim.get("rpm") or 5),
                  max_wait=float(lim.get("max_wait") or 240))


def _safe(fn, what):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - AI is a bonus, never a blocker
        log.warning("Gemini %s skipped: %s", what, str(e)[:200])
        return None


def _lang_name(code):
    return {"ur": "Urdu", "en": "English", "ar": "Arabic", "hi": "Hindi"}.get((code or "en")[:2].lower(), code or "English")


# --------------------------------------------------------------------------- tags / hashtags / hook
def enrich(gem, title, language, text, kind, part=None, parts=None):
    """-> {"tags": [...], "hashtags": [...], "hook": "..."} or None. One call per video (or per reel part)
    serves every platform, so several platforms never cost extra quota."""
    if not gem:
        return None
    where = f"reel part {part} of {parts}" if kind == "reel" else "full video"
    prompt = f"""You write search metadata for an educational video channel.
Video title: {title}
This post is the {where}. Language of the video: {_lang_name(language)}.
What is said in it (may be empty):
\"\"\"{(text or '')[:3500]}\"\"\"

Return ONLY JSON: {{"tags": [10 to 14 search keywords people really type, mixed broad and specific, no '#', no commas inside a tag],
"hashtags": [5 to 8 hashtags starting with '#', no spaces, relevant, the first 3 most important],
"hook": "one short line (max 90 chars) that makes someone watch, in {_lang_name(language)}, no emoji, no clickbait lies"}}
Tags and hashtags: include both English and {_lang_name(language)} versions of the main topic when the language is not English."""
    j = _safe(lambda: gem.json(prompt, 0.5, task="light"), "tags")
    if not isinstance(j, dict):
        return None
    tags = [re.sub(r"[<>#,]", "", str(t)).strip() for t in j.get("tags", []) if str(t).strip()]
    hts = ["#" + re.sub(r"[^\w]", "", str(h).lstrip("#")) for h in j.get("hashtags", []) if re.sub(r"[^\w]", "", str(h))]
    return {"tags": tags[:15], "hashtags": hts[:8], "hook": str(j.get("hook") or "").strip()[:120]}


# --------------------------------------------------------------------------- reel planning ("Let the backend choose")
def plan_reels_ai(gem, title, language, dur, count, cues, cands):
    """Gemini picks natural breaks. Result is checked and snapped to real pauses; any doubt -> chapter cuts."""
    fallback = plan_cuts(dur, count, "chapters", cands)
    if not gem or not cues:
        return [(a, b, "") for a, b in fallback]
    lines = "\n".join(f"[{s:.0f}s] {t}" for s, e, t in cues)[:14000]
    prompt = f"""Cut this {dur:.0f}-second educational video into about {count} vertical reels (target {dur / count:.0f}s each,
never shorter than 15s and never longer than 170s). Every reel must be one complete idea: start at the beginning of a
sentence and end at the end of a sentence, never in the middle of one. Title: {title}. Language: {_lang_name(language)}.
Safe cut points (seconds, real pauses in the audio): {cands[:200]}
Timed transcript:
{lines}

Return ONLY JSON: {{"reels": [{{"start": seconds, "end": seconds, "hook": "short line that makes the reel worth watching, in {_lang_name(language)}"}}]}}
Reels must be in order, must not overlap, and must start/end on a safe cut point (or 0 / {dur:.0f})."""
    j = _safe(lambda: gem.json(prompt, 0.3, task="medium"), "reel planning")
    try:
        raw = [(float(r["start"]), float(r["end"]), str(r.get("hook") or "")) for r in j["reels"]]
    except Exception:  # noqa: BLE001
        return [(a, b, "") for a, b in fallback]
    pts = [0.0] + list(cands) + [dur]

    def snap(x):
        n = min(pts, key=lambda p: abs(p - x))
        return n if abs(n - x) <= 4 else x
    out = []
    for a, b, h in sorted(raw):
        a, b = snap(max(0.0, a)), snap(min(dur, b))
        if out and a < out[-1][1]:
            a = out[-1][1]
        if b - a >= 8:
            out.append((a, b, h))
    ok = out and 0.5 * count <= len(out) <= 2 * count and sum(b - a for a, b, _ in out) > 0.6 * dur
    return out if ok else [(a, b, "") for a, b in fallback]


# --------------------------------------------------------------------------- transcription (last resort for subtitles)
def transcribe(gem, audio_path, language):
    """Gemini listens to the narration and returns timed lines -> cues [(start, end, text)]. Narration only, so it is
    clean speech; timings are approximate (+-0.5 s), which is fine for subtitles and for choosing cut points."""
    if not gem:
        return None
    data = Path(audio_path).read_bytes()
    if len(data) > 18_000_000:   # inline limit is ~20 MB
        log.warning("audio too large to transcribe inline (%d MB)", len(data) // 1_000_000)
        return None
    part = types.Part.from_bytes(data=data, mime_type="audio/mp3")
    text = f"""Transcribe this narration in {_lang_name(language)}, exactly as spoken, with correct punctuation.
Split it into subtitle lines of at most 90 characters, each ending at a natural pause.
Return ONLY JSON: {{"cues": [{{"start": seconds, "end": seconds, "text": "..."}}]}} with times in seconds from the start of the audio."""
    j = _safe(lambda: gem.run("medium", [part, text], True, 0.1), "transcription")
    try:
        cues = [(float(c["start"]), float(c["end"]), str(c["text"]).strip()) for c in j["cues"] if str(c.get("text", "")).strip()]
    except Exception:  # noqa: BLE001
        return None
    cues.sort()
    for i in range(len(cues) - 1):   # no overlaps
        if cues[i][1] > cues[i + 1][0]:
            cues[i] = (cues[i][0], max(cues[i + 1][0] - 0.02, cues[i][0] + 0.2), cues[i][2])
    return cues or None


def cues_text(cues, a=0.0, b=1e9):
    return " ".join(t for s, e, t in cues if e > a and s < b)
