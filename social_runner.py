"""
social_runner.py - uploads what the Frame AI web app planned in  Frame AI/Social/Days/<date>.json

Run:   python social_runner.py                 # normal run (GitHub Actions)
       python social_runner.py --dry-run       # show what is due, change nothing
       python social_runner.py --ignore-time   # upload today's items now, do not wait for their time slot
       python social_runner.py --date 2026-10-15

Env:   GOOGLE_CLIENT_ID  GOOGLE_CLIENT_SECRET  GOOGLE_REFRESH_TOKEN      (the same login the video runner uses)
       GEMINI_API_KEYS (optional; normally the keys come from Application Config/frame_config.json)
       MAX_ITEMS (default 10)  TIME_BUDGET_MIN (default 150)  SOCIAL_WORKDIR (default social_build)

Rules it follows (all written by the web app, see socialPlan.js / brand.js):
  * the web app chooses the DAY and the ORDER; the backend chooses the time of day (item.time == "auto")
  * only items planned for TODAY (in social_config.json -> timezone) go out; older ones show as "Missed" in the app
  * YouTube / Facebook complete videos get: logo, lower third, intro, outro, end card, thumbnail (+ subtitles on YouTube)
  * every platform's reels are cut ONCE per video and cached in Drive (Social/Cache); the next run picks them up
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import social_ai as AI
import social_media as M
from social_drive import FOLDER, Drive, TokenRejected, col_letter
from social_platforms import NeedsUser, Quota, YouTube, buffer_post
from social_thumb import build_thumbnail

log = logging.getLogger("social")
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
YT_COST = {"upload": 1600, "thumb": 50, "caption": 400}
PUBLIC_HOURS = 3          # a Drive file stays public this long after a Buffer post (Buffer must fetch it)

DEFAULTS = {
    "timezone": "Asia/Karachi",
    "accounts": {"youtube": {"refresh_token": ""}, "buffer": {"api_key": "", "organization_id": ""}},
    "platforms": {},
    "ai": {"tags": True, "hashtags": True, "hook": False, "reel_planning": True, "transcribe": True},
    "limits": {"youtube_units": 10000, "attempts": 3, "window_start": "10:00", "window_end": "22:00"},
}


def merge(a, b):
    if not isinstance(b, dict):
        return a if b is None else b
    o = dict(a) if isinstance(a, dict) else {}
    for k, v in b.items():
        o[k] = merge(o.get(k), v) if isinstance(v, dict) and isinstance(o.get(k), dict) else v
    return o


def lang_code(s):
    s = (s or "en").strip().lower()
    return {"urdu": "ur", "english": "en", "arabic": "ar", "hindi": "hi"}.get(s, s[:5] if re.match(r"^[a-z]{2}(-[a-z]{2})?$", s) else "en")


class Fail(Exception):
    """Permanent problem for this item (no retry)."""


class Runner:
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.drive = Drive()
        self.work = Path(os.environ.get("SOCIAL_WORKDIR", "social_build"))
        self.work.mkdir(parents=True, exist_ok=True)
        self.done, self.touched, self.blocked, self.needs_add, self.needs_clear = [], set(), set(), {}, set()
        self.local = {}           # drive file id -> local path (this run only)
        self.sheet_ids = {}
        self.gem = None

    # ------------------------------------------------------------------ workspace
    def locate(self):
        d = self.drive
        root = d.find("Frame AI", None, FOLDER)
        if not root:
            sys.exit("Folder 'Frame AI' not found in this Drive. Is GOOGLE_REFRESH_TOKEN from the right account?")
        self.root = root["id"]
        sub = lambda n: (d.find(n, self.root, FOLDER) or {}).get("id")   # noqa: E731
        self.social, self.brand_dir, self.app = sub("Social"), sub("Brand"), sub("Application Config")
        if not self.social:
            sys.exit("Frame AI/Social does not exist yet. Open the web app > Social once.")
        self.days_dir = d.find("Days", self.social, FOLDER)["id"]
        self.cache_dir = d.ensure_folder("Cache", self.social)
        self.cfg_file, cfg = d.read_json(self.social, "social_config.json")
        self.cfg = merge(DEFAULTS, cfg or {})
        self.tz = ZoneInfo(self.cfg.get("timezone") or "Asia/Karachi")
        _, brand = d.read_json(self.brand_dir, "brand.json") if self.brand_dir else (None, None)
        self.brand = brand or {}
        self.backend = (self.brand.get("backend") or {})
        self.bhash = hashlib.md5(json.dumps(self.backend, sort_keys=True).encode()).hexdigest()[:8]
        self.idx_file, idx = d.read_json(self.cache_dir, "media_index.json")
        self.index = idx or {}
        self.index.setdefault("videos", {})
        self.index.setdefault("public", {})
        _, fcfg = d.read_json(self.app, "frame_config.json") if self.app else (None, None)
        fcfg = fcfg or {}
        if not AI.csv_list((fcfg.get("keys") or {}).get("gemini")) and os.environ.get("GEMINI_API_KEYS"):
            fcfg.setdefault("keys", {})["gemini"] = os.environ["GEMINI_API_KEYS"]
        try:
            self.gem = AI.make_gemini(fcfg)
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini unavailable: %s", e)
        yt = self.cfg["accounts"].get("youtube") or {}
        self.yt = YouTube(self.drive.cid, self.drive.secret, yt["refresh_token"]) if yt.get("refresh_token") else None

    def save_index(self):
        self.idx_file = self.drive.write_json(self.cache_dir, "media_index.json", self.index, self.idx_file)

    def bucket(self, fid):
        return self.index["videos"].setdefault(fid, {"reels": {}, "plans": {}, "meta": {}})

    # ------------------------------------------------------------------ day files
    def load_days(self):
        out = {}
        for f in self.drive.list(self.days_dir):
            if DAY_RE.match(f["name"]):
                try:
                    out[f["name"][:10]] = {"file": f["id"], "data": self.drive.read_json_id(f["id"])}
                except Exception as e:  # noqa: BLE001
                    log.warning("unreadable day file %s: %s", f["name"], e)
        return out

    def edit_item(self, date, item_id, fn):
        """Re-read the day file right before writing (the web app edits the same file) and change only this item."""
        fid = self.days[date]["file"]
        data = self.drive.read_json_id(fid)
        for it in data.get("items", []):
            if it.get("id") == item_id:
                if not isinstance(it.get("result"), dict):
                    it["result"] = {"link": "", "at": "", "error": "", "attempts": 0}
                fn(it)
                if it.get("status") != "Uploading":
                    it["result"].pop("started", None)
                break
        data["updated"] = self.now().strftime("%Y-%m-%d %H:%M:%S")
        self.drive.write_json(self.days_dir, f"{date}.json", data, fid)
        self.days[date]["data"] = data
        return next((i for i in data["items"] if i.get("id") == item_id), None)

    def now(self):
        return datetime.now(self.tz)

    # ------------------------------------------------------------------ time of day
    def assign_times(self, date):
        """item.time == 'auto'  ->  a clock time inside the posting window, spread evenly per platform, stable jitter."""
        data = self.days[date]["data"]
        lim = self.cfg["limits"]
        ws, we = [int(x) for x in lim["window_start"].split(":")], [int(x) for x in lim["window_end"].split(":")]
        a, b = ws[0] * 60 + ws[1], we[0] * 60 + we[1]
        plan = {}
        for pf in {i["platform"] for i in data["items"]}:
            mine = sorted([i for i in data["items"] if i["platform"] == pf and i["status"] == "Planned"], key=lambda i: i.get("order", 0))
            for n, it in enumerate(mine):
                if it.get("time") in (None, "", "auto"):
                    jitter = int(hashlib.md5(it["id"].encode()).hexdigest()[:4], 16) % 21 - 10
                    m = int(a + (b - a) * (n + 0.5) / len(mine)) + jitter
                    m = max(a, min(b, m))
                    plan[it["id"]] = f"{m // 60:02d}:{m % 60:02d}"
        if not plan or self.args.dry_run:
            return
        fid = self.days[date]["file"]
        data = self.drive.read_json_id(fid)
        for it in data["items"]:
            if it["id"] in plan and it.get("time") in (None, "", "auto"):
                it["time"] = plan[it["id"]]
        self.drive.write_json(self.days_dir, f"{date}.json", data, fid)
        self.days[date]["data"] = data

    # ------------------------------------------------------------------ local files
    def fetch(self, fid, sub="src"):
        if fid in self.local and Path(self.local[fid]).exists():
            return self.local[fid]
        meta = self.drive._req("GET", f"https://www.googleapis.com/drive/v3/files/{fid}", params={"fields": "name"}).json()
        ext = Path(meta.get("name", "")).suffix or ".bin"
        p = self.work / sub / f"{fid}{ext}"
        if not p.exists():
            log.info("downloading %s (%s)", meta.get("name"), fid)
            self.drive.download(fid, p)
        self.local[fid] = str(p)
        return str(p)

    def source(self, v):
        if not v.get("file_id"):
            raise Fail("This video has no output file in Drive yet.")
        try:
            return self.fetch(v["file_id"])
        except RuntimeError as e:
            raise Fail(f"Could not download the video from Drive: {str(e)[:120]}") from e

    def brand_assets(self, kind):
        """kind video -> logo + extras; kind reel -> logo only. {slot: local path}"""
        out = {}
        wm = self.backend.get("video" if kind == "video" else "reel") or {}
        if wm.get("on") and wm.get("file_id"):
            out["wm_video" if kind == "video" else "wm_reel"] = self.fetch(wm["file_id"], "brand")
        if kind == "video":
            for slot, fid in (self.backend.get("extras") or {}).items():
                if fid:
                    out[slot] = self.fetch(fid, "brand")
        return out

    # ------------------------------------------------------------------ script / sheet
    def sheet_for(self, v):
        key = v.get("project_id")
        if key in self.sheet_ids:
            return self.sheet_ids[key]
        sid = None
        if v.get("single"):
            f = self.drive.find("Singles - Record", key, SHEET_MIME)
            sid = f["id"] if f else None
        elif self.app:
            ps = self.drive.find("Projects", self.app, SHEET_MIME)
            if ps:
                for r in self.drive.sheet_read(ps["id"], "Projects!A2:G"):
                    if len(r) > 2 and r[1] == key:
                        sid = r[2]
        self.sheet_ids[key] = sid
        return sid

    def row_of(self, v):
        sid = self.sheet_for(v)
        if not sid or not v.get("row"):
            return None, None, None
        head = (self.drive.sheet_read(sid, "Playlist!1:1") or [[]])[0]
        row = (self.drive.sheet_read(sid, f"Playlist!A{v['row']}:{col_letter(max(len(head), 1) - 1)}{v['row']}") or [[]])[0]
        return sid, head, row

    def script_for(self, v):
        try:
            sid, head, row = self.row_of(v)
            if not head:
                return ""
            get = lambda h: row[head.index(h)] if h in head and head.index(h) < len(row) else ""   # noqa: E731
            return get("Script").strip() if "script" in get("Input Type").lower() else ""
        except Exception as e:  # noqa: BLE001
            log.info("no script from sheet: %s", e)
            return ""

    def mark_uploaded(self, v):
        """All planned items of this video are out -> the playlist row becomes 'Uploaded'."""
        try:
            sid, head, row = self.row_of(v)
            if not head or "Status" not in head:
                return
            st = head.index("Status")
            if st < len(row) and str(row[st]).strip().lower() not in ("completed", "complete", "done"):
                return
            self.drive.sheet_put(sid, f"Playlist!{col_letter(st)}{v['row']}", [["Uploaded"]])
            if "Updated" in head:
                self.drive.sheet_put(sid, f"Playlist!{col_letter(head.index('Updated'))}{v['row']}",
                                     [[self.now().strftime("%Y-%m-%d %H:%M:%S")]])
            log.info("playlist row %s marked Uploaded", v["row"])
        except Exception as e:  # noqa: BLE001
            log.warning("could not mark row Uploaded: %s", e)

    # ------------------------------------------------------------------ subtitles: only a ready-made .srt is ever uploaded
    def srt_for(self, v):
        """Output/<same name>.srt if it exists, else [] (then the video simply goes up without subtitles)."""
        b = self.bucket(v["file_id"])
        if b.get("srt") is not None:
            return [tuple(c) for c in b["srt"]]
        cues = []
        try:
            meta = self.drive._req("GET", f"https://www.googleapis.com/drive/v3/files/{v['file_id']}", params={"fields": "name,parents"}).json()
            f = self.drive.find(Path(meta["name"]).stem + ".srt", (meta.get("parents") or [None])[0])
            if f:
                cues = M.parse_srt(self.drive.read_text(f["id"]))
        except Exception as e:  # noqa: BLE001
            log.debug("srt lookup: %s", e)
        b["srt"] = [list(c) for c in cues]
        log.info("subtitle file for %s: %s", v.get("title"), f"{len(cues)} lines" if cues else "none, uploading without")
        return cues

    # ------------------------------------------------------------------ cues (internal only: reel cuts + tags; never uploaded as subtitles)
    def cues_for(self, v, src):
        """Timed narration text used ONLY to choose reel cuts and write tags: .srt > script in the sheet > Gemini. Not uploaded."""
        b = self.bucket(v["file_id"])
        if b.get("cues") is not None:
            return [tuple(c) for c in b["cues"]]
        cues, how = None, ""
        try:
            meta = self.drive._req("GET", f"https://www.googleapis.com/drive/v3/files/{v['file_id']}", params={"fields": "name,parents"}).json()
            srt = self.drive.find(Path(meta["name"]).stem + ".srt", (meta.get("parents") or [None])[0])
            if srt:
                cues, how = M.parse_srt(self.drive.read_text(srt["id"])), "srt file"
        except Exception as e:  # noqa: BLE001
            log.debug("srt lookup: %s", e)
        if not cues:
            script = self.script_for(v)
            if script:
                pr = M.probe(src)
                cues, how = M.align_script(script, pr["dur"], M.silences(src)), "script"
        if not cues and self.cfg["ai"].get("transcribe") and self.gem:
            mp3 = self.work / "audio" / f"{v['file_id']}.mp3"
            mp3.parent.mkdir(exist_ok=True, parents=True)
            M.extract_audio(src, mp3)
            cues, how = AI.transcribe(self.gem, mp3, lang_code(v.get("language"))), "gemini"
        b["cues"] = [list(c) for c in cues or []]
        b["cues_from"] = how
        log.info("subtitles for %s: %d lines (%s)", v.get("title"), len(cues or []), how or "none")
        return list(cues or [])

    # ------------------------------------------------------------------ complete video (branded once, cached)
    def full_video(self, v):
        """-> (local path, drive file id, seconds the intro added). Same file serves YouTube and Facebook."""
        b = self.bucket(v["file_id"])
        c = b.get("full")
        if c and c.get("brand") == self.bhash:
            try:
                return self.fetch(c["file_id"], "branded"), c["file_id"], float(c.get("offset") or 0)
            except Exception as e:  # noqa: BLE001
                log.warning("cached branded video lost (%s), rebuilding", str(e)[:80])
        src = self.source(v)
        assets = self.brand_assets("video")
        if not assets:                       # nothing to add: the original is the video
            return src, v["file_id"], 0.0
        pr = M.probe(src)
        tmp = self.work / "brandtmp" / v["file_id"]
        shutil.rmtree(tmp, ignore_errors=True)
        out = self.work / "branded" / f"{v['file_id']}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        log.info("branding complete video: %s", v.get("title"))
        _, offset = M.brand_video(src, out, pr, self.backend, assets, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        folder = self.drive.ensure_folder("Branded", self.cache_dir)
        fid = self.drive.upload_file(out, f"{v['file_id']}_{self.bhash}.mp4", folder)
        if c and c.get("file_id"):
            self.drive.delete(c["file_id"])
        b["full"] = {"file_id": fid, "offset": offset, "brand": self.bhash}
        self.local[fid] = str(out)
        self.save_index()
        return str(out), fid, offset

    # ------------------------------------------------------------------ reels (cut once, reused by every platform and every day)
    @staticmethod
    def reel_key(reel):
        rng = ",".join(f"{x:.1f}" for x in reel["range_sec"]) if reel.get("range_sec") else "-"
        return f"{reel['of']}|{reel['cut']}|{reel.get('fit', 'blur')}|{int(bool(reel.get('part_label')))}|{rng}|{reel['part']}"

    def reel_file(self, v, reel):
        """-> (local path, drive file id, info{start,end,hook}). Renders the whole set the first time it is needed."""
        b = self.bucket(v["file_id"])
        key = f"{self.reel_key(reel)}|{self.bhash}"
        hit = b["reels"].get(key)
        if hit:
            try:
                return self.fetch(hit["file_id"], "reels"), hit["file_id"], hit
            except Exception as e:  # noqa: BLE001
                log.warning("cached reel lost (%s), rebuilding", str(e)[:80])
        src = self.source(v)
        pr = M.probe(src)
        cues = self.cues_for(v, src)
        sil = M.silences(src)
        cands = M.cut_candidates(sil, pr["dur"])
        n, of, mode = reel["part"], reel["of"], reel["cut"]
        if mode == "manual":
            if not reel.get("range_sec"):
                raise Fail("Manual reel without a time range.")
            plan = {n: (reel["range_sec"][0], min(reel["range_sec"][1], pr["dur"]), "")}
            if plan[n][0] >= pr["dur"]:
                raise Fail(f"Reel {n} starts after the video ends ({pr['dur']:.0f}s).")
        else:
            pk = f"{of}|{mode}"
            if pk not in b["plans"]:
                if mode == "auto" and self.cfg["ai"].get("reel_planning"):
                    cuts = AI.plan_reels_ai(self.gem, v.get("title", ""), lang_code(v.get("language")), pr["dur"], of, cues, cands)
                else:
                    cuts = [(a, z, "") for a, z in M.plan_cuts(pr["dur"], of, mode, cands)]
                b["plans"][pk] = [list(c) for c in cuts]
            plan = {i + 1: tuple(c) for i, c in enumerate(b["plans"][pk])}
            if n not in plan:
                raise Fail(f"This video only gives {len(plan)} reels, but reel {n} of {of} was planned.")
        logo = self.brand_assets("reel").get("wm_reel")
        folder = self.drive.ensure_folder(v["file_id"], self.drive.ensure_folder("Reels", self.cache_dir))
        for part, (a, z, hook) in plan.items():
            r = dict(reel, part=part, of=len(plan) if mode != "manual" else of)
            pkey = f"{self.reel_key(dict(reel, part=part, range_sec=reel.get('range_sec') if mode == 'manual' else None))}|{self.bhash}"
            if pkey in b["reels"]:
                continue
            out = self.work / "reels" / f"{v['file_id']}_{part:02d}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            log.info("rendering reel %d/%d (%.0fs-%.0fs) of %s", part, of, a, z, v.get("title"))
            M.render_reel(src, out, a, z, reel.get("fit", "blur"), part, r["of"], bool(reel.get("part_label")),
                          self.backend, logo)
            fid = self.drive.upload_file(out, f"part-{part:02d}_{self.bhash}.mp4", folder)
            b["reels"][pkey] = {"file_id": fid, "start": a, "end": z, "hook": hook}
            self.local[fid] = str(out)
            self.save_index()
        hit = b["reels"][key]
        return self.local[hit["file_id"]], hit["file_id"], hit

    # ------------------------------------------------------------------ metadata (frontend text first, Gemini fills the gaps)
    def metadata(self, v, item, text_range=None):
        reel = item.get("reel")
        k = f"p{reel['part']}of{reel['of']}" if reel else "full"
        b = self.bucket(v["file_id"])
        if k not in b["meta"]:
            cues = [tuple(c) for c in (b.get("cues") or [])]
            a, z = text_range or (0, 1e9)
            b["meta"][k] = AI.enrich(self.gem, v.get("title", ""), lang_code(v.get("language")), AI.cues_text(cues, a, z),
                                     item["kind"], reel and reel["part"], reel and reel["of"]) or {}
        return b["meta"][k]

    # ------------------------------------------------------------------ one item
    def public_url(self, fid):
        url = self.drive.make_public(fid)
        self.index["public"][fid] = time.time()
        return url

    def process(self, date, item):
        pf, kind, v = item["platform"], item["kind"], item["video"]
        if not v.get("file_id"):
            raise Fail("The video has no output file in Drive.")
        ai = self.cfg["ai"]
        thumb_path, units, link, extra = None, 0, "", {}
        if kind == "video":
            path, pub_id, offset = self.full_video(v)
            meta_range, info = None, {}
        else:
            path, pub_id, info = self.reel_file(v, item["reel"])
            meta_range, offset = (info["start"], info["end"]), 0.0
        meta = self.metadata(v, item, meta_range) if (ai.get("tags") or ai.get("hashtags") or ai.get("hook")) else {}
        hashtags = " ".join((meta.get("hashtags") or [])[:8])
        hook = meta.get("hook") if ai.get("hook") and kind == "reel" else ""

        if pf == "youtube":
            if not self.yt:
                raise NeedsUser("yt-token", "youtube", "YouTube is not connected. Connect it in Social > Accounts.")
            if self.yt_units_today() + YT_COST["upload"] > self.cfg["limits"]["youtube_units"]:
                raise Quota("YouTube daily quota is used up for today.")
            pub = item.get("publish") or {}
            desc = item.get("description") or ""
            if hook:
                desc = f"{hook}\n\n{desc}"
            if ai.get("hashtags") and hashtags and "#" not in re.sub(r"#shorts", "", desc, flags=re.I):
                desc = f"{desc}\n\n{' '.join(meta['hashtags'][:5])}".strip()
            tags = pub.get("tags") or (meta.get("tags") if ai.get("tags") else []) or []
            vid, link = self.yt.upload_video(path, item.get("title") or v.get("title"), desc, tags, pub.get("category"),
                                             pub.get("privacy"), pub.get("made_for_kids"), lang_code(v.get("language")),
                                             is_short=(kind == "reel"))
            units = YT_COST["upload"]
            extra["video_id"] = vid
            self.needs_clear.add("yt-token")
            if kind == "video":
                thumb_path = build_thumbnail(item.get("thumbnail"), self.drive, self.work / "thumb", self.brand.get("identity", {}).get("primary", "#4560e6"))
                if thumb_path and self.yt.set_thumbnail(vid, thumb_path):
                    units += YT_COST["thumb"]
                srt = self.srt_for(v)
                if srt:
                    shifted = [(s + offset, e + offset, t) for s, e, t in srt]
                    ok = self.yt.add_captions(vid, M.write_srt(shifted), lang_code(v.get("language")),
                                              AI._lang_name(lang_code(v.get("language"))))
                    units += YT_COST["caption"]
                    extra["captions"] = bool(ok)
                    if not ok:
                        self.needs_add["yt-captions"] = ("youtube", "Subtitles could not be added to YouTube. Connect YouTube again once so it can add subtitles.")
        else:
            acct = self.cfg["accounts"]["buffer"]
            acct = acct.get(pf) if (acct.get(pf) or {}).get("api_key") else acct
            if not (acct or {}).get("api_key"):
                raise NeedsUser(f"buffer-{pf}-key", pf, f"No Buffer API key for {pf}. Add it in Social > Accounts.")
            channel = (item.get("publish") or {}).get("channel_id") or (self.cfg["platforms"].get(pf) or {}).get("channel_id")
            if not channel:
                raise NeedsUser(f"buffer-{pf}-channel", pf, f"No Buffer channel chosen for {pf}. Pick it in Social > Accounts.")
            text = item.get("caption") or v.get("title", "")
            if hook:
                text = f"{hook}\n\n{text}"
            if ai.get("hashtags") and hashtags and "#" not in text:
                text = f"{text}\n\n{' '.join(meta['hashtags'][:5 if pf == 'tiktok' else 8])}"
            thumb_url = None
            if kind == "video" and pf == "facebook":
                thumb_path = build_thumbnail(item.get("thumbnail"), self.drive, self.work / "thumb", self.brand.get("identity", {}).get("primary", "#4560e6"))
                if thumb_path:
                    tid = self.drive.upload_file(thumb_path, f"thumb_{item['id']}.jpg", self.drive.ensure_folder("Thumbs", self.cache_dir), "image/jpeg")
                    thumb_url = self.public_url(tid)
                    self.index.setdefault("trash", []).append(tid)
            post_id = buffer_post(acct["api_key"], channel, pf, kind, text, self.public_url(pub_id), thumb_url)
            extra["buffer_id"] = post_id
            self.needs_clear.update({f"buffer-{pf}-key", f"buffer-{pf}-channel"})
        return link, units, extra

    def yt_units_today(self):
        today = self.today
        return sum(int((i.get("result") or {}).get("units") or 0) for d in self.days.values() for i in d["data"].get("items", [])
                   if i.get("platform") == "youtube" and i.get("status") == "Uploaded" and str((i.get("result") or {}).get("at", "")).startswith(today))

    # ------------------------------------------------------------------ housekeeping
    def need_requests(self):
        if not (self.needs_add or self.needs_clear) or self.args.dry_run:
            return
        fid, data = self.drive.read_json(self.social, "backend_requests.json")
        reqs = [r for r in (data or {}).get("requests", []) if r.get("id") not in self.needs_clear or r.get("id") in self.needs_add]
        have = {r["id"]: r for r in reqs}
        stamp = self.now().strftime("%Y-%m-%d %H:%M:%S")
        for rid, (pf, msg) in self.needs_add.items():
            have[rid] = {"id": rid, "platform": pf, "message": msg, "at": stamp}
        if fid or have:
            self.drive.write_json(self.social, "backend_requests.json", {**(data or {}), "requests": list(have.values())}, fid)

    def cleanup(self):
        """1) revoke public links older than PUBLIC_HOURS   2) delete cache files that are no longer public
        3) move the cache of videos with nothing left to post into the trash (deleted once Buffer can no longer need them)"""
        now = time.time()
        for fid, t in list(self.index["public"].items()):
            if now - t > PUBLIC_HOURS * 3600:
                self.drive.revoke_public(fid)
                del self.index["public"][fid]
        trash = self.index.setdefault("trash", [])
        horizon = self.now().date().toordinal() - 14
        pending = set()
        for date, d in self.days.items():
            if datetime.fromisoformat(date).toordinal() < horizon:
                continue
            for i in d["data"].get("items", []):
                if i.get("status") in ("Planned", "Uploading"):
                    pending.add((i.get("video") or {}).get("file_id"))
        for fid, b in list(self.index["videos"].items()):
            stale_full = bool(b.get("full")) and b["full"].get("brand") != self.bhash
            if fid in pending and not stale_full:
                continue
            if stale_full:
                trash.append(b["full"]["file_id"])
                b.pop("full", None)
            if fid in pending:
                continue
            trash.extend(p["file_id"] for p in b.get("reels", {}).values())
            if b.get("full"):
                trash.append(b["full"]["file_id"])
            del self.index["videos"][fid]
            log.info("cache of video %s queued for deletion (nothing left to post)", fid)
        keep = []
        for fid in trash:
            if fid in self.index["public"]:
                keep.append(fid)          # Buffer may still be fetching it
            else:
                self.drive.delete(fid)
        self.index["trash"] = keep
        if not self.args.dry_run:
            self.save_index()

    # ------------------------------------------------------------------ main loop
    def run(self):
        self.locate()
        self.days = self.load_days()
        self.today = self.args.date or self.now().strftime("%Y-%m-%d")
        if self.today not in self.days:
            log.info("nothing planned for %s", self.today)
            self.cleanup()
            return
        attempts_max = int(self.cfg["limits"]["attempts"])
        # stale 'Uploading' (a run that died): put back to Planned
        for it in list(self.days[self.today]["data"]["items"]):
            if it.get("status") == "Uploading":
                started = (it.get("result") or {}).get("started", 0)
                if time.time() - float(started or 0) > 3 * 3600 and not self.args.dry_run:
                    self.edit_item(self.today, it["id"], lambda x: x.update(status="Planned"))
        self.assign_times(self.today)
        hhmm = self.now().strftime("%H:%M")
        due = [i for i in self.days[self.today]["data"]["items"] if i.get("status") == "Planned"
               and (self.args.ignore_time or (i.get("time") in (None, "", "auto") or i["time"] <= hhmm))]
        due.sort(key=lambda i: (i.get("time") or "", i.get("order", 0)))
        log.info("%s %s: %d due now (%d planned today)", self.today, hhmm, len(due),
                 sum(1 for i in self.days[self.today]["data"]["items"] if i.get("status") == "Planned"))
        if self.args.dry_run:
            for i in due:
                print(f"DUE  {i.get('time')}  {i['platform']:9} {i['kind']:5} {('part %s/%s' % (i['reel']['part'], i['reel']['of'])) if i.get('reel') else '':10} {i['video']['title']}")
            return
        budget, cap = float(os.environ.get("TIME_BUDGET_MIN", 150)) * 60, int(os.environ.get("MAX_ITEMS", 10))
        for item in due[:]:
            if len(self.done) >= cap or time.time() - self.t0 > budget:
                log.info("run limit reached, the rest waits for the next run")
                break
            pf = item["platform"]
            if pf in self.blocked:
                continue
            n = int((item.get("result") or {}).get("attempts") or 0)
            self.edit_item(self.today, item["id"], lambda x: (x.update(status="Uploading"), x["result"].update(started=time.time())))
            stamp = lambda: self.now().strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
            try:
                log.info(">> %s %s %s", pf, item["kind"], item["video"]["title"])
                link, units, extra = self.process(self.today, item)
                self.edit_item(self.today, item["id"], lambda x: (x.update(status="Uploaded"), x["result"].update(
                    link=link, at=stamp(), error="", attempts=n + 1, units=units, **extra), x["result"].pop("started", None)))
                self.done.append(item)
                self.touched.add(item["video"].get("file_id"))
                self.save_index()
            except Quota as e:
                msg = f"Waiting: {e}"
                self.blocked.add(pf)
                self.edit_item(self.today, item["id"], lambda x: (x.update(status="Planned"), x["result"].update(error=msg)))
                log.warning("%s blocked for this run: %s", pf, e)
            except NeedsUser as e:
                self.blocked.add(pf)
                msg = str(e)
                self.needs_add[e.need_id] = (e.platform, msg)
                self.edit_item(self.today, item["id"], lambda x: (x.update(status="Planned"), x["result"].update(error=msg)))
                log.error("%s needs the user: %s", pf, e)
            except Fail as e:
                msg = str(e)[:300]
                self.edit_item(self.today, item["id"], lambda x: (x.update(status="Failed"), x["result"].update(error=msg, attempts=n + 1)))
                log.error("FAILED %s: %s", item["video"]["title"], e)
            except TokenRejected as e:
                self.blocked.update({"youtube", pf})
                self.needs_add["google-login"] = ("youtube", f"Google refused the backend login: {e}")
                self.edit_item(self.today, item["id"], lambda x: x.update(status="Planned"))
            except Exception as e:  # noqa: BLE001
                bad, msg = n + 1 >= attempts_max, str(e)[:300]
                self.edit_item(self.today, item["id"], lambda x: (x.update(status="Failed" if bad else "Planned"),
                                                                  x["result"].update(error=msg, attempts=n + 1)))
                log.exception("error on %s", item["video"]["title"])
        # playlist row -> Uploaded when every planned item of the video is out
        for fid in self.touched:
            allitems = [i for d in self.days.values() for i in d["data"].get("items", []) if (i.get("video") or {}).get("file_id") == fid]
            if allitems and all(i.get("status") == "Uploaded" for i in allitems):
                self.mark_uploaded(allitems[0]["video"])
        self.need_requests()
        self.cleanup()
        self.summary()

    def summary(self):
        lines = [f"Social run {self.today}: {len(self.done)} uploaded"] + [
            f"- {i['platform']} {i['kind']}{(' part ' + str(i['reel']['part'])) if i.get('reel') else ''}: {i['video']['title']}" for i in self.done]
        txt = "\n".join(lines)
        log.info("\n%s", txt)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a").write(txt + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-time", action="store_true", default=os.environ.get("IGNORE_TIME") == "1")
    ap.add_argument("--date")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        Runner(args).run()
    except TokenRejected as e:
        sys.exit(f"Google refused GOOGLE_REFRESH_TOKEN / client secret: {e}")


if __name__ == "__main__":
    main()
