"""
social_platforms.py - the actual publishing calls.

  YouTube  -> YouTube Data API v3 (resumable upload, thumbnails.set, captions.insert), login = refresh token
              saved by the web app in social_config.json (accounts.youtube.refresh_token)
  Facebook / Instagram / TikTok -> Buffer GraphQL API (https://api.buffer.com). Buffer has no file upload:
              it needs a PUBLIC url, so the caller makes the Drive file public just for the post.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests

from social_drive import TokenRejected, refresh_access_token

log = logging.getLogger("social")


class Quota(Exception):
    """Platform says: no more uploads today / for now.
    scope: "channel" = this channel/platform only (e.g. Buffer scheduled-post cap per channel),
           "key" = everything that uses the same API key (e.g. Buffer request rate limit, which is per API key)."""
    def __init__(self, message="", scope="channel"):
        super().__init__(message)
        self.scope = scope


class NeedsUser(Exception):
    """Login / key problem only the user can fix. .need_id and .platform go to backend_requests.json."""
    def __init__(self, need_id, platform, message):
        super().__init__(message)
        self.need_id, self.platform = need_id, platform


# =========================================================================== YouTube
YT_UP = "https://www.googleapis.com/upload/youtube/v3"
YT = "https://www.googleapis.com/youtube/v3"
CHUNK = 16 * 1024 * 1024


def _clean(s, n):
    return re.sub(r"[<>]", "", s or "").strip()[:n]


def _yt_tags(tags):
    out, total = [], 0
    for t in tags or []:
        t = _clean(str(t).lstrip("#"), 60)
        if t and total + len(t) + 3 <= 480 and t.lower() not in [x.lower() for x in out]:
            out.append(t)
            total += len(t) + 3
    return out


class YouTube:
    def __init__(self, client_id, client_secret, refresh_token):
        self.cid, self.secret, self.refresh = client_id, client_secret, refresh_token
        self._tok, self._exp = None, 0.0

    def token(self):
        if not self._tok or time.time() > self._exp:
            try:
                self._tok, self._exp = refresh_access_token(self.cid, self.secret, self.refresh)
            except TokenRejected as e:
                raise NeedsUser("yt-token", "youtube", "YouTube refused the saved login. Connect it again in Social > Accounts.") from e
        return self._tok

    def _h(self, extra=None):
        return {"Authorization": f"Bearer {self.token()}", **(extra or {})}

    def _raise(self, r):
        low = r.text.lower()
        if r.status_code == 401:
            raise NeedsUser("yt-token", "youtube", "YouTube refused the saved login. Connect it again in Social > Accounts.")
        if any(k in low for k in ("uploadlimitexceeded", "quotaexceeded", "dailylimitexceeded", "ratelimitexceeded")):
            raise Quota(f"YouTube quota: {r.text[:160]}")
        raise RuntimeError(f"YouTube {r.status_code}: {r.text[:300]}")

    def upload_video(self, path, title, description, tags, category, privacy, made_for_kids, language, is_short):
        desc = _clean(description, 4900)
        if is_short and "#shorts" not in desc.lower() and "#shorts" not in title.lower():
            desc = (desc + "\n\n#Shorts").strip()
        snippet = {"title": _clean(title, 100) or "Untitled", "description": desc, "categoryId": str(category or "27"),
                   "tags": _yt_tags(tags)}
        if language:
            snippet["defaultLanguage"] = language
            snippet["defaultAudioLanguage"] = language
        body = {"snippet": snippet, "status": {"privacyStatus": privacy or "public", "selfDeclaredMadeForKids": bool(made_for_kids),
                                                "embeddable": True, "license": "youtube"}}
        path = Path(path)
        size = path.stat().st_size
        r = requests.post(f"{YT_UP}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                          headers=self._h({"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}),
                          json=body, timeout=60)
        if r.status_code != 200:
            self._raise(r)
        loc, sent = r.headers["Location"], 0
        with open(path, "rb") as f:
            while sent < size:
                f.seek(sent)
                chunk = f.read(CHUNK)
                end = sent + len(chunk) - 1
                for attempt in range(5):
                    try:
                        p = requests.put(loc, data=chunk, timeout=600, headers={
                            "Content-Length": str(len(chunk)), "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {sent}-{end}/{size}"})
                        break
                    except requests.RequestException as e:
                        log.warning("YouTube chunk retry %d: %s", attempt + 1, e)
                        time.sleep(5 * (attempt + 1))
                        q = requests.put(loc, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}, timeout=60)
                        if q.status_code == 308 and q.headers.get("Range"):
                            sent = int(q.headers["Range"].split("-")[1]) + 1
                            f.seek(sent)
                            chunk = f.read(CHUNK)
                            end = sent + len(chunk) - 1
                else:
                    raise RuntimeError("YouTube upload kept failing")
                if p.status_code in (200, 201):
                    vid = p.json()["id"]
                    return vid, f"https://youtu.be/{vid}"
                if p.status_code == 308:
                    sent = int(p.headers["Range"].split("-")[1]) + 1 if p.headers.get("Range") else sent + len(chunk)
                    continue
                self._raise(p)
        raise RuntimeError("YouTube upload ended without a video id")

    def set_thumbnail(self, video_id, jpg):
        r = requests.post(f"{YT_UP}/thumbnails/set", params={"videoId": video_id, "uploadType": "media"},
                          headers=self._h({"Content-Type": "image/jpeg"}), data=Path(jpg).read_bytes(), timeout=120)
        if r.status_code != 200:
            log.warning("thumbnail not set (%s): %s", r.status_code, r.text[:160])   # channel not verified for custom thumbnails, etc.
            return False
        return True

    # ---- playlists (needs scope youtube / youtube.force-ssl, which the web app already asks for) ----
    def find_playlist(self, title):
        """Id of the channel's own playlist with exactly this title (case-insensitive), or None. 1 quota unit per page."""
        want, token = title.strip().lower(), None
        while True:
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if token:
                params["pageToken"] = token
            r = requests.get(f"{YT}/playlists", params=params, headers=self._h(), timeout=60)
            if r.status_code != 200:
                self._raise(r)
            j = r.json()
            for it in j.get("items", []):
                if (it["snippet"].get("title") or "").strip().lower() == want:
                    return it["id"]
            token = j.get("nextPageToken")
            if not token:
                return None

    def create_playlist(self, title, description="", privacy="public", language=None):
        snippet = {"title": _clean(title, 150), "description": _clean(description, 4900)}
        if language:
            snippet["defaultLanguage"] = language
        r = requests.post(f"{YT}/playlists", params={"part": "snippet,status"}, headers=self._h(), timeout=60,
                          json={"snippet": snippet, "status": {"privacyStatus": privacy or "public"}})
        if r.status_code != 200:
            self._raise(r)
        return r.json()["id"]

    def add_to_playlist(self, playlist_id, video_id):
        """Returns True, 'gone' when the playlist no longer exists, or raises."""
        r = requests.post(f"{YT}/playlistItems", params={"part": "snippet"}, headers=self._h(), timeout=60,
                          json={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}})
        if r.status_code == 200:
            return True
        if r.status_code == 404 and "playlistnotfound" in r.text.lower():
            return "gone"
        self._raise(r)

    def add_captions(self, video_id, srt_text, language, name="Subtitles"):
        """Needs scope youtube.force-ssl. Returns True, or False when the saved login lacks that scope."""
        meta = {"snippet": {"videoId": video_id, "language": language or "en", "name": name, "isDraft": False}}
        import json as _json
        boundary = "frameai-boundary"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{_json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n{srt_text}\r\n--{boundary}--").encode("utf-8")
        r = requests.post(f"{YT_UP}/captions", params={"uploadType": "multipart", "part": "snippet"},
                          headers=self._h({"Content-Type": f"multipart/related; boundary={boundary}"}), data=body, timeout=120)
        if r.status_code == 200:
            return True
        if r.status_code == 403 and ("insufficient" in r.text.lower() or "scope" in r.text.lower()):
            return False
        log.warning("captions not added (%s): %s", r.status_code, r.text[:200])
        return True   # some other problem: do not nag the user to reconnect


# =========================================================================== Buffer
BUFFER = "https://api.buffer.com"
CREATE = """mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}"""


def _buffer_meta(platform, kind):
    if platform == "instagram":
        return {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    if platform == "facebook":
        return {"facebook": {"type": "reel" if kind == "reel" else "post"}}
    if platform == "tiktok":
        return {"tiktok": {"type": "post"}}
    return None


def _buffer_request(api_key, inp):
    """One createPost request. A 429 with a short Retry-After is waited out once (limits are per API key)."""
    for attempt in (1, 2):
        try:
            r = requests.post(BUFFER, json={"query": CREATE, "variables": {"input": inp}}, timeout=90,
                              headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"})
        except requests.RequestException as e:
            raise RuntimeError(f"Buffer not reachable: {e}") from e
        wait = int(r.headers.get("Retry-After") or 0) if r.status_code == 429 else 0
        if attempt == 1 and 0 < wait <= 90:
            time.sleep(wait + 1)
            continue
        return r


def buffer_post(api_key, channel_id, platform, kind, text, video_url, thumb_url=None, due_at=None):
    """due_at (UTC ISO string, e.g. 2026-10-13T16:30:00.000Z) -> Buffer schedules the post for that time (customScheduled);
    without it the post is published now (shareNow). Returns the Buffer post id. The media url must stay public until Buffer has fetched it.

    thumb_url is accepted for backward compatibility but NOT sent: Buffer rejects video.thumbnailUrl
    ("social networks do not accept custom video thumbnail images")."""
    base = {"text": text or "", "channelId": channel_id, "schedulingType": "automatic", "mode": "shareNow",
            "assets": [{"video": {"url": video_url}}]}
    if due_at:
        base.update(mode="customScheduled", dueAt=due_at)
    meta = _buffer_meta(platform, kind)
    tries = [dict(base, metadata=meta)] if meta else []
    tries.append(base)                       # if Buffer rejects the metadata shape, post without it
    last, first = "", ""
    for inp in tries:
        r = _buffer_request(api_key, inp)
        if r.status_code in (401, 403):
            raise NeedsUser(f"buffer-{platform}-key", platform, f"Buffer refused the {platform} API key. Paste a new one in Social > Accounts.")
        if r.status_code == 429:
            raise Quota("Buffer API rate limit for this API key (resets automatically)", scope="key")
        j = r.json() if r.content else {}
        errs = j.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "RATE_LIMIT_EXCEEDED" for e in errs):
            raise Quota("Buffer API rate limit for this API key (resets automatically)", scope="key")
        if errs:
            last = errs[0].get("message", str(errs[0]))
            first = first or last
            if inp is not tries[-1] and any(k in last.lower() for k in ("metadata", "enum", "variable")):
                continue                        # try again without metadata
            raise RuntimeError(f"Buffer: {first[:300]}")   # first error = the real reason (the retry's error is only noise)
        res = (j.get("data") or {}).get("createPost") or {}
        if res.get("post"):
            return res["post"]["id"]
        last = res.get("message") or str(j)[:300]
        first = first or last
        if "limit" in last.lower() or "quota" in last.lower():
            raise Quota(f"Buffer: {last[:200]}")
        if inp is not tries[-1] and "metadata" in last.lower():
            continue
        raise RuntimeError(f"Buffer: {first[:300]}")
    raise RuntimeError(f"Buffer: {first[:300] or last[:300]}")
