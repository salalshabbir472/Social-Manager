"""
social_thumb.py - makes the 1280x720 YouTube / Facebook thumbnail.

item["thumbnail"] comes from the web app (config.js thumbnailFor):
  { source: "video", url }                         -> the video's own picture, used as it is
  { source: "default_template" | "plain_background", template_file_id, title_text,
    font, size, color, stroke, align, x, y, max_width, shade }   -> title written on the playlist template
x / y / max_width are percent of the picture width / height; size is in pixels on a 1280-wide picture.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, features

log = logging.getLogger("social")
W, H = 1280, 720
FALLBACK = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
_font_cache = {}


def _has_arabic(t):
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F]", t or ""))


def google_font(family, workdir):
    """Download a Google Font once (the CSS API answers with a plain .ttf when no browser is announced)."""
    f = Path(workdir) / f"{re.sub(r'[^A-Za-z0-9]+', '_', family)}.ttf"
    if f.exists():
        return str(f)
    try:
        css = requests.get("https://fonts.googleapis.com/css2", params={"family": family}, timeout=20).text
        m = re.search(r"url\((https://[^)]+\.ttf)\)", css)
        if m:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(requests.get(m.group(1), timeout=30).content)
            return str(f)
    except Exception as e:  # noqa: BLE001
        log.info("font %s not downloadable (%s)", family, e)
    return None


def pick_font(family, size, text, workdir):
    fam = "Noto Naskh Arabic" if _has_arabic(text) else (family or "Fredoka")
    key = (fam, size)
    if key in _font_cache:
        return _font_cache[key]
    path = google_font(fam, workdir) or next((p for p in FALLBACK if Path(p).exists()), None)
    font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    _font_cache[key] = font
    return font


def _shape(text):
    """Arabic-script text needs shaping + right-to-left order; Pillow does it only when built with raqm."""
    if not _has_arabic(text) or features.check("raqm"):
        return text, ("rtl" if _has_arabic(text) and features.check("raqm") else None)
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text)), None
    except ImportError:
        return text, None


def _wrap(draw, text, font, max_px):
    lines, cur = [], ""
    for word in text.split():
        t = (cur + " " + word).strip()
        if draw.textlength(_shape(t)[0], font=font) <= max_px or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines[:5]


def _hex(c, default):
    c = (c or default).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return _hex(default, default) if default != c else (255, 255, 255)


def compose(spec, template_bytes, workdir, brand_primary="#4560e6"):
    if template_bytes:
        img = Image.open(io.BytesIO(template_bytes)).convert("RGB")
        scale = max(W / img.width, H / img.height)
        img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
        img = img.crop(((img.width - W) // 2, (img.height - H) // 2, (img.width - W) // 2 + W, (img.height - H) // 2 + H))
    else:   # plain background: soft vertical gradient in the brand colour
        top, bot = _hex(brand_primary, "#4560e6"), tuple(int(c * 0.45) for c in _hex(brand_primary, "#4560e6"))
        img = Image.new("RGB", (W, H))
        px = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            px.line([(0, y), (W, y)], fill=tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    text = (spec.get("title_text") or "").strip()
    if not text:
        return img
    align = spec.get("align") or "left"
    size = int(spec.get("size") or 110)
    max_px = W * float(spec.get("max_width") or 58) / 100
    xp, yp = float(spec.get("x") if spec.get("x") is not None else 6), float(spec.get("y") if spec.get("y") is not None else 50)
    if spec.get("shade", True):   # dark fade behind the text so it stays readable on any template
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        span = int(max_px + W * xp / 100 + 120)
        for x in range(W):
            a = 0
            edge = span if align != "right" else W - span
            if align == "right":
                a = int(150 * max(0, min(1, (x - edge) / max(span, 1))))
            elif align == "center":
                a = int(110 * (1 - abs(x - W / 2) / (W / 2)))
            else:
                a = int(150 * max(0, 1 - x / max(span, 1)))
            sd.line([(x, 0), (x, H)], fill=(0, 0, 0, a))
        img = Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")
    draw = ImageDraw.Draw(img)
    # shrink the font until the title fits in 4 lines
    while True:
        font = pick_font(spec.get("font"), size, text, workdir)
        lines = _wrap(draw, text, font, max_px)
        lh = int(size * 1.18)
        if (len(lines) <= 4 and len(" ".join(lines)) >= len(text) - 2) or size <= 40:
            break
        size = int(size * 0.9)
    block_h = lh * len(lines)
    y0 = H * yp / 100 - block_h / 2
    y0 = max(20, min(y0, H - block_h - 20))
    fill, stroke = _hex(spec.get("color"), "#ffffff"), _hex(spec.get("stroke"), "#2a2550")
    for i, line in enumerate(lines):
        s, direction = _shape(line)
        tw = draw.textlength(s, font=font)
        if align == "center":
            x = W * xp / 100 + (max_px - tw) / 2 if xp < 50 else (W - tw) / 2
        elif align == "right":
            x = W - W * xp / 100 - tw
        else:
            x = W * xp / 100
        kw = {"direction": direction} if direction else {}
        draw.text((x, y0 + i * lh), s, font=font, fill=fill, stroke_width=max(2, size // 22), stroke_fill=stroke, **kw)
    return img


def build_thumbnail(spec, drive, workdir, brand_primary="#4560e6"):
    """-> path of a .jpg under 2 MB (YouTube limit), or None."""
    if not spec:
        return None
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "thumbnail.jpg"
    try:
        if spec.get("source") == "video" and spec.get("url"):
            m = re.search(r"/d/([\w-]{10,})", spec["url"]) or re.search(r"[?&]id=([\w-]{10,})", spec["url"])
            data = drive._req("GET", f"https://www.googleapis.com/drive/v3/files/{m.group(1)}", params={"alt": "media"}).content if m \
                else requests.get(spec["url"], timeout=30).content
            img = Image.open(io.BytesIO(data)).convert("RGB").resize((W, H), Image.LANCZOS)
        else:
            tpl = None
            if spec.get("template_file_id"):
                tpl = drive._req("GET", f"https://www.googleapis.com/drive/v3/files/{spec['template_file_id']}", params={"alt": "media"}).content
            img = compose(spec, tpl, workdir, brand_primary)
        for q in (92, 85, 75, 60):
            img.save(out, "JPEG", quality=q)
            if out.stat().st_size < 1_900_000:
                break
        return out
    except Exception as e:  # noqa: BLE001 - a missing thumbnail never blocks the upload
        log.warning("thumbnail skipped: %s", e)
        return None
