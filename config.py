"""
config.py - settings + small helpers (NO Gemini / network calls here).

Contains: model chains, LANGS, VideoConfig (the single control surface) and generic
helpers (env loading, ffprobe duration, slugify, JSON-fence stripping, disk cache...).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("edu")

# Windows consoles / files default to cp1252, which cannot hold Urdu: force UTF-8 everywhere.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

QUALITY_FLAGS = {"low": "-ql", "medium": "-qm", "high": "-qh"}

# --------------------------------------------------------------------------- #
# Free-tier model chains (Google pricing page, Oct 2026: all of these list
# "Free of charge"). Free Flash models have small daily request caps, Flash-Lite has
# a big one -> cheap JSON work goes to Flash-Lite first, code goes to Flash first, and
# the long Flash chain multiplies the daily allowance (limits are per model).
# Models your project cannot use (404/403 "model") are dropped automatically.
# Check your real limits: https://aistudio.google.com/rate-limit
# --------------------------------------------------------------------------- #
DEFAULT_LIGHT_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
)

DEFAULT_MEDIUM_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)

DEFAULT_CODE_MODELS = (
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
)

DEFAULT_TTS_MODELS = (
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
)

DEFAULT_TRANSCRIPTION_MODELS = (
    "gemini-3.5-transcribe",
    "gemini-3.5-transcribe-live",
)

LANGS = {
    "en": dict(name="English", wps=2.5, rtl=False,
               cloud_voices="en-US-Neural2-D,en-US-Standard-D"),
    # Urdu is NOT in Google's official Gemini-TTS language list (checked 2026-10-07):
    # Gemini auto-detects language, so it may still speak it, but test with --tts-test.
    "ur": dict(name="Urdu", wps=2.3, rtl=True,
               cloud_voices="ur-IN-Wavenet-A,ur-IN-Standard-A"),
}
SENT_SPLIT = re.compile(r"(?<=[.!?؟۔…])\s+")


# --------------------------------------------------------------------------- #
# The single control surface
# --------------------------------------------------------------------------- #
def _env(name, default):
    return lambda: os.environ.get(name, default)


@dataclass
class VideoConfig:
    topic: str
    # content
    minutes: float = 3.0
    audience: str = "curious beginners"
    language: str = "en"             # narration + script + TTS: "en" | "ur"
    screen_language: str = ""        # on-screen text; "" = same as `language`
    # voice
    voice: str = "Charon"            # Gemini prebuilt voice (Kore, Puck, Charon, Fenrir, Aoede...)
    tts_style: str = "calm, clear and friendly, like a good teacher"
    tts_engine: str = "auto"         # auto = Gemini then Cloud TTS | gemini | cloud
    cloud_voices: str = ""           # "" = per-language default
    # models
    light_models: str = field(default_factory=_env("GEMINI_MODELS_LIGHT", DEFAULT_LIGHT_MODELS))
    medium_models: str = field(default_factory=_env("GEMINI_MODELS_MEDIUM", DEFAULT_MEDIUM_MODELS))
    code_models: str = field(default_factory=_env("GEMINI_MODELS_CODE", DEFAULT_CODE_MODELS))
    tts_models: str = field(default_factory=_env("GEMINI_MODELS_TTS", DEFAULT_TTS_MODELS))
    rpm: float = field(default_factory=lambda: float(os.environ.get("GEMINI_RPM", 5)))
    tts_rpm: float = field(default_factory=lambda: float(os.environ.get("GEMINI_TTS_RPM", 4)))
    tts_group_scenes: int = 4        # max scenes sent to Gemini TTS in ONE call (1 = no batching)
    tts_group_chars: int = 4000      # max total characters per TTS call
    max_wait: float = 240.0          # max seconds to sleep for a quota window
    # visuals
    scene_sec: float = 15.0          # target seconds per scene (smaller = more scenes)
    chapter_min: float = 2.0         # script is planned in chapters of ~this many minutes (videos > 4 min)
    scene_batch: int = 4             # scenes whose narration is written per Gemini call
    max_scenes: int = 60
    min_steps_per_sentence: int = 2
    # video
    quality: str = "medium"          # low | medium | high
    retries: int = 2                 # Manim auto-repair attempts per scene
    allow_latex: bool = False
    urdu_font: str = "Noto Naskh Arabic"
    workdir: str = "build"
    output_dir: str = "output"

    def __post_init__(self):
        self.language = self.language.lower()
        self.screen_language = (self.screen_language or self.language).lower()
        for lang in (self.language, self.screen_language):
            if lang not in LANGS:
                raise ValueError(f"language must be one of {sorted(LANGS)}, got {lang!r}")
        if self.quality not in QUALITY_FLAGS:
            raise ValueError(f"quality must be one of {sorted(QUALITY_FLAGS)}")
        if self.tts_engine not in ("auto", "gemini", "cloud"):
            raise ValueError("tts_engine must be auto | gemini | cloud")
        if self.chapter_min <= 0 or self.scene_batch < 1 or self.tts_group_scenes < 1:
            raise ValueError("chapter_min must be > 0; scene_batch and tts_group_scenes >= 1")
        if not self.topic or not self.topic.strip():
            raise ValueError("topic is empty")
        if self.screen_language == "ur":
            self.allow_latex = False   # no LaTeX with Urdu text

    @property
    def slug(self):
        # slugify() cuts at 40 chars: two long topics that start the same would share a work
        # folder (and reuse each other's cached script!), so add a short hash when it was cut.
        full = re.sub(r"[^a-z0-9]+", "-", self.topic.lower()).strip("-")
        base = slugify(self.topic)
        if len(full) > 40:
            base = base[:31].rstrip("-") + "-" + hashlib.md5(self.topic.strip().encode()).hexdigest()[:8]
        return f"{base}-{self.language}"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_list(*names):
    out = []
    for n in names:
        for part in os.environ.get(n, "").replace("\n", ",").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def csv(s):
    if isinstance(s, (list, tuple)):
        return [str(x).strip() for x in s if str(x).strip()]
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def run(cmd, **kw):
    # ffmpeg / manim output can contain Urdu: decode as UTF-8, never crash on odd bytes
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    return float(r.stdout.strip())


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or \
        "topic-" + hashlib.md5(s.encode()).hexdigest()[:8]   # Urdu titles have no a-z


def strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json|python)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def as_scenes(raw):
    """Gemini sometimes returns a bare list, or a dict under another key. Always give back
    a list of scene dicts."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("scenes")
        if items is None:
            lists = [v for v in raw.values() if isinstance(v, list)]
            items = lists[0] if lists else []
    else:
        items = []
    return [x for x in items if isinstance(x, dict)]


def load_json(path, default=None):
    """Read a JSON file; a missing, empty or half-written file gives `default` (never a crash)."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("  ignoring unreadable %s (%s); it will be rebuilt", path.name, e)
        return default


def save_json(path, data):
    """Write to a temp file, then swap it in: a crash can never leave a truncated JSON behind."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def cached(path: Path, fn):
    data = load_json(path)
    if data is not None:
        log.info("  (cached) %s", path.name)
        return data
    data = fn()
    save_json(path, data)
    return data


def secs_to_pacific_midnight() -> float:
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return (nxt - now).total_seconds()


def split_sentences(text: str):
    parts = [s.strip() for s in SENT_SPLIT.split(text.strip()) if s.strip()]
    return parts or [text.strip()]
