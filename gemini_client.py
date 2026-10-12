"""
gemini_client.py - ALL the Gemini text/code API plumbing lives here.

* Models come in TIERS (heavy / medium / light). The lists live in config.py
  (VideoConfig.code_models = heavy, medium_models, light_models; env GEMINI_MODELS_CODE /
  _MEDIUM / _LIGHT) and are all free-tier models.
* TASKS maps every pipeline task to a tier and the Rotator builds a fallback chain:
      heavy task  -> heavy models, then medium, then light
      medium task -> medium models, then light, then heavy
      light task  -> light models, then medium, then heavy
  so on 429 / 503 / bad output the next model (and the next key) is used automatically.
* Every (key, model) pair has its own cooldown; one failing pair never blocks the others.
  A model your project cannot use (404 / limit 0) is dropped automatically.
* TTS uses the same Rotator (see tts.py).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time

from google import genai
from google.genai import types

from config import (DEFAULT_CODE_MODELS, DEFAULT_LIGHT_MODELS, DEFAULT_MEDIUM_MODELS, csv,
                    env_list, secs_to_pacific_midnight, strip_fences)

log = logging.getLogger("edu")


# --------------------------------------------------------------------------- #
# Task -> tier routing  (edit freely)
# --------------------------------------------------------------------------- #
TASKS = {
    "plan": "heavy",      # stage 1a: chapters, running example, terms (1 call, sets the quality)
    "outline": "medium",  # stage 1b: scene list per chapter (structure only, low thinking is enough)
    "write": "heavy",     # stage 1c: the narration itself
    "script": "heavy",    # (old name, kept for compatibility)
    "code": "heavy",      # stage 5: Manim code
    "repair": "heavy",    # stage 5: fixing broken Manim code
    "spec": "medium",     # stage 4: animation plan pinned to sentences
    "analyze": "light",   # stage 3: concepts / key points
}

# fallback order between tiers
TIER_ORDER = {
    "heavy": ("heavy", "medium", "light"),
    "medium": ("medium", "light", "heavy"),
    "light": ("light", "medium", "heavy"),
}


# Quality-critical tasks never use Flash-Lite models (note: the default heavy list itself ends with
# lite models, so this filter is what actually keeps them out). If every other model is out of
# quota the run stops with QuotaExhausted and resumes later (the script is saved after every
# batch) instead of quietly writing the narration with the weakest models.
# Remove a task from this set to get the normal fallback back.
NO_LITE = {"plan", "write"}


def _dedupe(*lists):
    seen, out = set(), []
    for lst in lists:
        for m in lst or []:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
    return out


def tier_of(task):
    return task if task in TIER_ORDER else TASKS.get(task, "medium")


def rpm_for(model, base):
    """Free-tier Flash-Lite / Lite-TTS models allow far more requests per minute."""
    return base * 3 if "lite" in model else base


# --------------------------------------------------------------------------- #
# Key / model rotation engine
# --------------------------------------------------------------------------- #
class BadOutput(Exception):
    """Model answered, but with unusable content (empty / invalid JSON / no audio)."""


class RetrySame(BadOutput):
    """Retry the same model immediately (e.g. after dropping an unsupported option)."""


class QuotaExhausted(RuntimeError):
    pass


class Rotator:
    """
    Calls fn(key, variant) with the best (variant, key) pair that is not cooling down.
    `variant` = model name (Gemini) or voice name (Cloud TTS), in priority order.
    classify(exc) -> (kind, delay_seconds):
        rate        quota/429: cool this (key, variant) for `delay`
        overloaded  503 / overloaded: model is busy for everybody -> cool it on ALL keys
        bad_output  empty / invalid answer: cool this model on all keys, try the next model
        transient   5xx / timeout: short cooldown of this (key, variant), limited attempts
        bad_key     key invalid/disabled: never use it again this run
        bad_variant model/voice unavailable: never use it again this run
        fatal       re-raise
    Pacing (min request gap per key+variant) is implemented as a cooldown too, so while one
    model is "resting" the rotator immediately uses another model/key instead of sleeping.
    """

    def __init__(self, name, keys, classify, min_interval=0.0, max_wait=240.0, max_attempts=20,
                 interval_for=None):
        self.name, self.keys, self.classify = name, list(keys), classify
        self.min_interval, self.max_wait, self.max_attempts = min_interval, max_wait, max_attempts
        self.interval_for = interval_for      # optional fn(variant) -> seconds between calls
        self.cool = {}            # (key_index, variant) -> epoch when usable again
        self.dead_keys = set()
        self.dead_variants = set()
        self.rr = 0               # round-robin pointer so load spreads over keys

    def _interval(self, v):
        return self.interval_for(v) if self.interval_for else self.min_interval

    def _cool(self, ki, v, secs):
        self.cool[(ki, v)] = max(self.cool.get((ki, v), 0.0), time.time() + secs)

    def _cool_all_keys(self, v, secs):
        for k in range(len(self.keys)):
            self._cool(k, v, secs)

    def run(self, fn, variants, label=""):
        attempts = 0
        n = len(self.keys)
        name = f"{self.name}:{label}" if label else self.name
        while True:
            live = [(v, ki) for v in variants if v not in self.dead_variants
                    for ki in range(n) if ki not in self.dead_keys]
            if not live:
                raise RuntimeError(f"[{name}] no usable keys/models left "
                                   f"(dead keys: {sorted(k + 1 for k in self.dead_keys)}, "
                                   f"dead models: {sorted(self.dead_variants)})")
            now = time.time()
            ready = [(v, ki) for v, ki in live if self.cool.get((ki, v), 0) <= now]
            if not ready:
                wake = min(self.cool[(ki, v)] for v, ki in live)
                if wake - now > self.max_wait:
                    raise QuotaExhausted(
                        f"[{name}] every key/model is rate-limited for another "
                        f"{(wake - now) / 60:.0f} min (daily quota?). Re-run later; "
                        f"finished stages are cached.")
                (log.info if wake - now > 5 else log.debug)(
                    "[%s] all keys/models cooling down, sleeping %.0fs", name, wake - now)
                time.sleep(max(wake - now, 0) + 0.5)
                continue
            ready.sort(key=lambda t: (variants.index(t[0]), (t[1] - self.rr) % n))
            v, ki = ready[0]
            tag = f"key#{ki + 1}/{v}"
            try:
                out = fn(self.keys[ki], v)
                self._cool(ki, v, self._interval(v))
                self.rr = (ki + 1) % n
                log.info("[%s] ok  %s", name, tag)
                return out
            except Exception as e:  # noqa: BLE001 - classified below
                self._cool(ki, v, self._interval(v))
                kind, delay = self.classify(e)
                nxt = ""
                if kind == "rate":
                    self._cool(ki, v, delay)
                    log.warning("[%s] %s rate-limited, cooling %.0fs, rotating", name, tag, delay)
                elif kind in ("overloaded", "bad_output"):
                    attempts += 1
                    if attempts >= self.max_attempts:
                        raise
                    self._cool_all_keys(v, delay)
                    log.warning("[%s] %s %s (%s), switching model for %.0fs", name, tag, kind,
                                str(e)[:100], delay)
                elif kind == "transient":
                    attempts += 1
                    if attempts >= self.max_attempts:
                        raise
                    self._cool(ki, v, delay)
                    log.warning("[%s] %s transient (%s), retrying", name, tag, str(e)[:120])
                elif kind == "bad_key":
                    self.dead_keys.add(ki)
                    log.error("[%s] key#%d rejected (%s); dropping it", name, ki + 1, str(e)[:120])
                elif kind == "bad_variant":
                    self.dead_variants.add(v)
                    log.error("[%s] %s unavailable (%s); dropping it", name, v, str(e)[:100])
                else:
                    raise


def http_options():
    """The SDK retries 429s internally (and sleeps for minutes/hours), which hides them from
    our key/model rotation. Turn that off for 429; keep a couple of quick retries for 5xx."""
    return types.HttpOptions(
        timeout=180_000,
        retry_options=types.HttpRetryOptions(attempts=2, http_status_codes=[500, 502, 504]))


# ---- Gemini ----------------------------------------------------------------
def classify_gemini(e):
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code is None:
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
    low = str(e).lower()
    if isinstance(e, RetrySame):
        return "transient", 0
    if isinstance(e, BadOutput):
        return "bad_output", 20
    if code == 429 or "resource_exhausted" in low or "too many requests" in low:
        if "limit: 0" in low:                       # this model is not in the free tier here
            return "rate", 12 * 3600
        if re.search(r"perday|per day|daily", low):
            return "rate", secs_to_pacific_midnight()
        m = (re.search(r"retry in\s*([\d.]+)\s*s", low)
             or re.search(r"retrydelay['\"]?\s*:\s*['\"]?([\d.]+)\s*s", low))
        return "rate", (float(m.group(1)) + 2 if m else 65.0)
    key_markers = ("api key not valid", "api_key_invalid", "api key expired", "api key not found",
                   "reported as leaked", "consumer_suspended", "has been suspended")
    if any(m in low for m in key_markers):
        return "bad_key", 0
    if code == 400 and "language" in low and ("support" in low):
        return "bad_variant", 0
    if code in (401, 403):
        return ("bad_variant", 0) if "model" in low else ("bad_key", 0)   # never kill a key for a model
    if code == 404:
        return "bad_variant", 0
    if code == 400 and "api key" in low:
        return "bad_key", 0
    if code == 503 or "overloaded" in low:          # "service unavailable" -> use another model
        return "overloaded", 30
    if code in (500, 502, 504):
        return "transient", 15
    if code is None and any(s in type(e).__name__ for s in ("Timeout", "Connect", "Remote", "Read")):
        return "transient", 10
    return "fatal", 0


def _thinking(model, tier):
    """Thinking budget by tier. heavy = model default (full thinking)."""
    if tier == "heavy" or model.startswith("gemma"):
        return None
    g25 = model.startswith("gemini-2.5")
    if tier == "light":
        return (types.ThinkingConfig(thinking_budget=0) if g25
                else types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL))
    return (types.ThinkingConfig(thinking_budget=1024) if g25
            else types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW))


class Gemini:
    def __init__(self, keys, light_models=None, code_models=None, rpm=5, max_wait=240.0,
                 medium_models=None):
        self.tiers = {"light": list(light_models or csv(DEFAULT_LIGHT_MODELS)),
                      "medium": list(medium_models or csv(DEFAULT_MEDIUM_MODELS)),
                      "heavy": list(code_models or csv(DEFAULT_CODE_MODELS))}
        self.rpm = rpm
        self.rot = Rotator("gemini", keys, classify_gemini, min_interval=60.0 / max(rpm, 0.5),
                           max_wait=max_wait, interval_for=self._interval)
        self._clients = {}
        self.no_think = set()    # models that rejected thinking_config

    def _interval(self, model):
        return 60.0 / max(rpm_for(model, self.rpm), 0.5)

    def _client(self, key):
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key, http_options=http_options())
        return self._clients[key]

    def chain(self, task):
        models = _dedupe(*[self.tiers[t] for t in TIER_ORDER[tier_of(task)]])
        if task in NO_LITE:
            models = [m for m in models if "lite" not in m] or models
        return models

    def _call(self, key, model, prompt, as_json, tier, temperature):
        cfg = {}
        if model.startswith(("gemini-2.5", "gemma")):
            cfg["temperature"] = temperature            # Gemini 3.x: leave temperature at default
        if as_json and not model.startswith("gemma"):   # Gemma has no JSON mode; prompt + strip_fences
            cfg["response_mime_type"] = "application/json"
        think = None if model in self.no_think else _thinking(model, tier)
        if think is not None:
            cfg["thinking_config"] = think
        try:
            r = self._client(key).models.generate_content(
                model=model, contents=prompt, config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True), **cfg))
        except Exception as e:  # noqa: BLE001
            if "thinking_config" in cfg and "thinking" in str(e).lower() and getattr(e, "code", None) == 400:
                self.no_think.add(model)
                raise RetrySame(f"{model} rejects thinking config, retrying without") from e
            raise
        try:
            text = r.text
        except Exception:  # noqa: BLE001
            text = None
        if not text:
            raise BadOutput("empty response")
        if not as_json:
            return text
        try:
            return json.loads(strip_fences(text))
        except json.JSONDecodeError as je:
            raise BadOutput(f"invalid JSON: {je}") from je

    def run(self, task, prompt, as_json, temperature=0.4):
        tier = tier_of(task)
        return self.rot.run(lambda k, m: self._call(k, m, prompt, as_json, tier, temperature),
                            self.chain(task), label=f"{task}/{tier}")

    def json(self, prompt, temperature=0.4, task="analyze"):
        return self.run(task, prompt, True, temperature)

    def code(self, prompt, temperature=0.3, task="code"):
        return self.run(task, prompt, False, temperature)


def list_models():
    """Print the Flash / Pro / Gemma / TTS models the first Gemini key can see (--list-models)."""
    keys = env_list("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not keys:
        sys.exit("No Gemini keys set.")
    for m in genai.Client(api_key=keys[0]).models.list():
        n = m.name.replace("models/", "")
        if any(s in n.lower() for s in ("flash", "pro", "gemma", "tts")):
            print(n)
