"""
gdrive.py - tiny Google Drive + Sheets client for the social backend.

Uses the SAME refresh token the video runner uses (scope drive.file), so it can see every file the
Frame AI web app created: Social/, Brand/, playlist folders, Output/ videos.
Env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("social")
D = "https://www.googleapis.com/drive/v3"
UP = "https://www.googleapis.com/upload/drive/v3"
S = "https://sheets.googleapis.com/v4/spreadsheets"
FOLDER = "application/vnd.google-apps.folder"


def refresh_access_token(client_id, client_secret, refresh_token):
    """Returns (access_token, expires_at). Also used for the YouTube token."""
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=30)
    j = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in j:
        raise TokenRejected(j.get("error_description") or j.get("error") or f"HTTP {r.status_code}")
    return j["access_token"], time.time() + int(j.get("expires_in", 3600)) - 60


class TokenRejected(RuntimeError):
    """Google refused the refresh token (revoked / expired / wrong client)."""


class Drive:
    def __init__(self, client_id=None, client_secret=None, refresh_token=None):
        self.cid = client_id or os.environ["GOOGLE_CLIENT_ID"]
        self.secret = client_secret or os.environ["GOOGLE_CLIENT_SECRET"]
        self.refresh = refresh_token or os.environ["GOOGLE_REFRESH_TOKEN"]
        self._tok, self._exp = None, 0.0

    # ---- plumbing ---------------------------------------------------------
    def token(self):
        if not self._tok or time.time() > self._exp:
            self._tok, self._exp = refresh_access_token(self.cid, self.secret, self.refresh)
        return self._tok

    def _h(self, extra=None):
        h = {"Authorization": f"Bearer {self.token()}"}
        h.update(extra or {})
        return h

    def _req(self, method, url, **kw):
        extra, timeout = kw.pop("headers", None), kw.pop("timeout", 120)
        for attempt in range(4):
            r = requests.request(method, url, headers=self._h(extra), timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Drive {method} {url.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
            return r
        raise RuntimeError("unreachable")

    # ---- files ------------------------------------------------------------
    def find(self, name, parent=None, mime=None):
        q = [f"name='{name.replace(chr(39), chr(92) + chr(39))}'", "trashed=false"]
        if parent:
            q.append(f"'{parent}' in parents")
        if mime:
            q.append(f"mimeType='{mime}'")
        r = self._req("GET", f"{D}/files", params={"q": " and ".join(q), "fields": "files(id,name,mimeType,size)"})
        files = r.json().get("files", [])
        return files[0] if files else None

    def list(self, parent, name_contains=None):
        q = f"'{parent}' in parents and trashed=false"
        if name_contains:
            q += f" and name contains '{name_contains}'"
        out, tok = [], None
        while True:
            p = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,size)", "pageSize": 1000}
            if tok:
                p["pageToken"] = tok
            j = self._req("GET", f"{D}/files", params=p).json()
            out += j.get("files", [])
            tok = j.get("nextPageToken")
            if not tok:
                return out

    def ensure_folder(self, name, parent):
        f = self.find(name, parent, FOLDER)
        if f:
            return f["id"]
        r = self._req("POST", f"{D}/files", params={"fields": "id"}, json={"name": name, "mimeType": FOLDER, "parents": [parent]})
        return r.json()["id"]

    def read_json(self, parent, name):
        f = self.find(name, parent)
        if not f:
            return None, None
        r = self._req("GET", f"{D}/files/{f['id']}", params={"alt": "media"})
        try:
            return f["id"], r.json()
        except ValueError:
            return f["id"], None

    def read_json_id(self, file_id):
        return self._req("GET", f"{D}/files/{file_id}", params={"alt": "media"}).json()

    def write_json(self, parent, name, data, file_id=None):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode()
        if file_id:
            self._req("PATCH", f"{UP}/files/{file_id}", params={"uploadType": "media"}, data=body,
                      headers={"Content-Type": "application/json"})
            return file_id
        return self.upload_bytes(body, name, parent, "application/json")

    def read_text(self, file_id):
        return self._req("GET", f"{D}/files/{file_id}", params={"alt": "media"}).content.decode("utf-8", "replace").lstrip("\ufeff")

    def download(self, file_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._req("GET", f"{D}/files/{file_id}", params={"alt": "media"}, stream=True, timeout=600) as r, open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        return dest

    def upload_bytes(self, data, name, parent, mime):
        r = self._req("POST", f"{UP}/files", params={"uploadType": "resumable", "fields": "id"},
                      headers={"Content-Type": "application/json", "X-Upload-Content-Type": mime},
                      json={"name": name, "parents": [parent]})
        p = self._req("PUT", r.headers["Location"], data=data, headers={"Content-Type": mime}, timeout=600)
        return p.json()["id"]

    def upload_file(self, path, name, parent, mime="video/mp4"):
        """Resumable upload of a (large) local file."""
        path = Path(path)
        size = path.stat().st_size
        r = self._req("POST", f"{UP}/files", params={"uploadType": "resumable", "fields": "id"},
                      headers={"Content-Type": "application/json", "X-Upload-Content-Type": mime,
                               "X-Upload-Content-Length": str(size)},
                      json={"name": name, "parents": [parent]})
        loc = r.headers["Location"]
        with open(path, "rb") as f:
            p = requests.put(loc, data=f, headers={"Content-Length": str(size), "Content-Type": mime}, timeout=1800)
        if p.status_code >= 400:
            raise RuntimeError(f"Drive upload failed: {p.status_code} {p.text[:200]}")
        return p.json()["id"]

    def delete(self, file_id):
        try:
            self._req("DELETE", f"{D}/files/{file_id}")
        except RuntimeError as e:
            log.debug("delete %s: %s", file_id, e)

    # ---- public link (Buffer must fetch media from a public URL) -----------
    def make_public(self, file_id):
        self._req("POST", f"{D}/files/{file_id}/permissions", json={"type": "anyone", "role": "reader"})
        return public_url(file_id)

    def revoke_public(self, file_id):
        try:
            r = self._req("GET", f"{D}/files/{file_id}/permissions", params={"fields": "permissions(id,type)"})
            for p in r.json().get("permissions", []):
                if p.get("type") == "anyone":
                    self._req("DELETE", f"{D}/files/{file_id}/permissions/{p['id']}")
        except RuntimeError as e:
            log.debug("revoke %s: %s", file_id, e)

    # ---- sheets -----------------------------------------------------------
    def sheet_read(self, sheet_id, rng):
        r = self._req("GET", f"{S}/{sheet_id}/values/{requests.utils.quote(rng, safe='')}")
        return r.json().get("values", [])

    def sheet_put(self, sheet_id, rng, rows):
        self._req("PUT", f"{S}/{sheet_id}/values/{requests.utils.quote(rng, safe='')}",
                  params={"valueInputOption": "USER_ENTERED"}, json={"values": rows})

    def sheet_append(self, sheet_id, tab, row):
        self._req("POST", f"{S}/{sheet_id}/values/{requests.utils.quote(tab + '!A1', safe='')}:append",
                  params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, json={"values": [row]})


def public_url(file_id):
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def col_letter(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
