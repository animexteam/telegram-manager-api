"""
gist_store.py — use a private GitHub Gist as persistent storage for
Telethon session files & metadata.

Why this exists:
  Render's free-tier filesystem is ephemeral — anything written to disk
  disappears when the service sleeps. So we mirror every account's
  session.session + meta.json into a *private* gist (only visible to the
  token owner). On boot, we pull from gist → disk. After every write
  (upload / login / delete), we push the affected account dir → gist.

File layout in gist:
  acc_<id>/session.session    (base64-encoded text, because GitHub Gist
                               files are text-only and a .session is a
                               binary SQLite file)
  acc_<id>/meta.json          (plain JSON)
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx


class GistStorage:
    BINARY_EXT = {".session"}  # base64-encode these in the gist

    def __init__(self, token: str, gist_id: Optional[str] = None):
        self.token = token
        self.gist_id = gist_id or ""
        self.base = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ------------------------------------------------------------------ #
    # CONFIG CHECKS
    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        return bool(self.token)

    def has_gist(self) -> bool:
        return bool(self.gist_id)

    def status(self) -> Dict:
        return {
            "has_token": self.is_configured(),
            "gist_id": self.gist_id or None,
            "gist_url": (f"https://gist.github.com/{self.gist_id}" if self.gist_id else None),
        }

    # ------------------------------------------------------------------ #
    # GIST LIFECYCLE
    # ------------------------------------------------------------------ #
    def create_gist(self, description: str = "Telegram Manager API session store") -> Dict:
        """Create a new private gist. Sets self.gist_id and returns API JSON."""
        body = {
            "description": description,
            "public": False,
            "files": {
                ".gistkeep": {"content": f"created at {int(time.time())}"},
            },
        }
        r = httpx.post(f"{self.base}/gists", headers=self.headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        self.gist_id = data["id"]
        return data

    def get_gist(self) -> Dict:
        if not self.gist_id:
            return {}
        r = httpx.get(f"{self.base}/gists/{self.gist_id}", headers=self.headers, timeout=30)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    def list_files(self) -> Dict[str, str]:
        """Return {filename: raw_url} for all files in the gist."""
        data = self.get_gist()
        files = data.get("files", {}) or {}
        return {name: f["raw_url"] for name, f in files.items()}

    # ------------------------------------------------------------------ #
    # READ / WRITE INDIVIDUAL FILES
    # ------------------------------------------------------------------ #
    def read_file(self, filename: str) -> Optional[bytes]:
        urls = self.list_files()
        if filename not in urls:
            return None
        r = httpx.get(urls[filename], timeout=30)
        r.raise_for_status()
        ext = os.path.splitext(filename)[1].lower()
        if ext in self.BINARY_EXT:
            return base64.b64decode(r.text)
        return r.content

    def write_file(self, filename: str, data: bytes) -> None:
        ext = os.path.splitext(filename)[1].lower()
        if ext in self.BINARY_EXT:
            content = base64.b64encode(data).decode("ascii")
        else:
            content = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        body = {"files": {filename: {"content": content}}}
        r = httpx.patch(
            f"{self.base}/gists/{self.gist_id}",
            headers=self.headers,
            json=body,
            timeout=30,
        )
        r.raise_for_status()

    def delete_file(self, filename: str) -> None:
        body = {"files": {filename: None}}  # null = delete
        r = httpx.patch(
            f"{self.base}/gists/{self.gist_id}",
            headers=self.headers,
            json=body,
            timeout=30,
        )
        # 200 if deleted, 404 if gist itself missing — both acceptable
        if r.status_code not in (200, 404):
            r.raise_for_status()

    # ------------------------------------------------------------------ #
    # HIGH-LEVEL SYNC (gist <-> local sessions dir)
    # ------------------------------------------------------------------ #
    # NOTE: gist filenames cannot contain "/" — we use "__" as separator.
    #   acc_<id>/session.session   (on disk)
    #   -> acc_<id>__session.session  (in gist)

    SEP = "__"

    def _to_gist_name(self, acc_id: str, fname: str) -> str:
        return f"{acc_id}{self.SEP}{fname}"

    def _from_gist_name(self, gist_name: str) -> Optional[Tuple[str, str]]:
        """Returns (acc_id, filename) or None if not in acc_<id>__<file> format."""
        if gist_name == ".gistkeep":
            return None
        if self.SEP not in gist_name:
            return None
        acc_id, _, fname = gist_name.partition(self.SEP)
        if not acc_id.startswith("acc_"):
            return None
        return acc_id, fname

    def pull_to_dir(self, target_dir: Path) -> Dict[str, List[str]]:
        """Pull all account files from gist → local target_dir.

        Returns {acc_id: [filenames...]} that were pulled.
        Skips '.gistkeep' and any file outside an acc_* namespace.
        """
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        pulled: Dict[str, List[str]] = {}
        for gist_name in self.list_files().keys():
            parsed = self._from_gist_name(gist_name)
            if not parsed:
                continue
            acc_id, fname = parsed
            data = self.read_file(gist_name)
            if data is None:
                continue
            local_path = target_dir / acc_id / fname
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(data)
            pulled.setdefault(acc_id, []).append(fname)
        return pulled

    def push_account(self, sessions_dir: Path, acc_id: str) -> List[str]:
        """Push all files under sessions_dir/acc_id/ → gist.

        Each file is uploaded as "acc_id__<filename>" (double underscore,
        because gist filenames cannot contain "/").
        Returns the list of pushed gist filenames.
        """
        if not self.has_gist():
            raise RuntimeError("GIST_ID not set. Call /sync/init first.")
        acc_dir = sessions_dir / acc_id
        if not acc_dir.exists():
            return []
        pushed: List[str] = []
        for f in sorted(acc_dir.iterdir()):
            if not f.is_file():
                continue
            if f.name.endswith(".session-journal"):
                continue
            gist_name = self._to_gist_name(acc_id, f.name)
            self.write_file(gist_name, f.read_bytes())
            pushed.append(gist_name)
        return pushed

    def push_all(self, sessions_dir: Path) -> Dict[str, List[str]]:
        """Push every account dir → gist. Returns {acc_id: [filenames]}."""
        sessions_dir = Path(sessions_dir)
        out: Dict[str, List[str]] = {}
        for d in sorted(sessions_dir.glob("acc_*")):
            if not d.is_dir():
                continue
            out[d.name] = self.push_account(sessions_dir, d.name)
        return out

    def delete_account(self, acc_id: str) -> List[str]:
        """Delete all files for an account from the gist."""
        if not self.has_gist():
            return []
        prefix = f"{acc_id}{self.SEP}"
        deleted: List[str] = []
        for gist_name in list(self.list_files().keys()):
            if gist_name.startswith(prefix):
                self.delete_file(gist_name)
                deleted.append(gist_name)
        return deleted
