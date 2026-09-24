"""
tele_manager.py — Telethon-based Telegram account/session manager.

Design:
  - Each account has an acc_id (acc_<12hex>) and lives under SESSIONS_DIR/acc_<id>/
    * session.session     — Telethon SQLite session file
    * meta.json           — {api_id, api_hash, phone, name, user_id, created_at}
  - Login flow uses a transient in-memory TelegramClient kept between OTP / 2FA calls.
  - Operations (get_me, list_chats, send, visit, click_ads) connect on demand and
    disconnect after a short idle TTL.
"""
import os
import io
import json
import time
import uuid
import asyncio
import zipfile
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from telethon import TelegramClient
from telethon.tl import types
from telethon.tl.functions.messages import GetSponsoredMessagesRequest
from telethon.tl.types import (
    Channel,
    Chat,
    User,
)


class TeleManager:
    def __init__(self, sessions_dir: Path, gist_store=None):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        # transient clients during login (acc_id -> TelegramClient)
        self._login_clients: Dict[str, TelegramClient] = {}
        # transient clients during operation, with last-use timestamps
        self._op_clients: Dict[str, TelegramClient] = {}
        self._lock = asyncio.Lock()
        # Optional persistent storage backend (GitHub Gist)
        self.gist = gist_store

    # ------------------------------------------------------------------ #
    # GIST SYNC HELPERS
    # ------------------------------------------------------------------ #
    def _sync_push_account(self, acc_id: str) -> List[str]:
        """Push this account's files to gist. No-op if gist not configured."""
        if not self.gist or not self.gist.has_gist():
            return []
        try:
            return self.gist.push_account(self.sessions_dir, acc_id)
        except Exception as e:
            # Don't fail the user-facing call if sync fails — log and move on.
            print(f"[gist] push_account({acc_id}) failed: {e}", flush=True)
            return []

    def _sync_delete_account(self, acc_id: str) -> List[str]:
        if not self.gist or not self.gist.has_gist():
            return []
        try:
            return self.gist.delete_account(acc_id)
        except Exception as e:
            print(f"[gist] delete_account({acc_id}) failed: {e}", flush=True)
            return []

    def sync_pull_all(self) -> Dict[str, List[str]]:
        """Pull all accounts from gist → local disk. Public for /sync/pull."""
        if not self.gist or not self.gist.has_gist():
            return {}
        return self.gist.pull_to_dir(self.sessions_dir)

    def sync_push_all(self) -> Dict[str, List[str]]:
        """Push all local accounts → gist. Public for /sync/push."""
        if not self.gist or not self.gist.has_gist():
            return {}
        return self.gist.push_all(self.sessions_dir)

    # ------------------------------------------------------------------ #
    # PATH HELPERS
    # ------------------------------------------------------------------ #
    def acc_dir(self, acc_id: str) -> Path:
        d = self.sessions_dir / acc_id
        return d

    def session_file(self, acc_id: str) -> Path:
        return self.acc_dir(acc_id) / "session.session"

    def meta_file(self, acc_id: str) -> Path:
        return self.acc_dir(acc_id) / "meta.json"

    def load_meta(self, acc_id: str) -> Dict[str, Any]:
        f = self.meta_file(acc_id)
        if not f.exists():
            return {}
        return json.loads(f.read_text() or "{}")

    def save_meta(self, acc_id: str, meta: Dict[str, Any]) -> None:
        f = self.meta_file(acc_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    def require_account(self, acc_id: str) -> Dict[str, Any]:
        if not self.acc_dir(acc_id).exists():
            raise HTTPException(404, f"Account {acc_id} not found")
        return self.load_meta(acc_id)

    # ------------------------------------------------------------------ #
    # LIST / DELETE / INFO
    # ------------------------------------------------------------------ #
    async def list_accounts(self) -> List[Dict[str, Any]]:
        out = []
        for d in sorted(self.sessions_dir.glob("acc_*")):
            if not d.is_dir():
                continue
            meta = self.load_meta(d.name)
            out.append({
                "account_id": d.name,
                "phone": meta.get("phone"),
                "name": meta.get("name"),
                "user_id": meta.get("user_id"),
                "api_id": meta.get("api_id"),
                "has_session_file": self.session_file(d.name).exists(),
                "created_at": meta.get("created_at"),
                "last_used": meta.get("last_used"),
            })
        return out

    async def get_account_info(self, acc_id: str) -> Dict[str, Any]:
        meta = self.require_account(acc_id)
        return {
            "account_id": acc_id,
            "phone": meta.get("phone"),
            "name": meta.get("name"),
            "user_id": meta.get("user_id"),
            "api_id": meta.get("api_id"),
            "has_session_file": self.session_file(acc_id).exists(),
            "created_at": meta.get("created_at"),
            "last_used": meta.get("last_used"),
        }

    async def delete_account(self, acc_id: str) -> None:
        # close any transient client
        for store in (self._login_clients, self._op_clients):
            c = store.pop(acc_id, None)
            if c and c.is_connected():
                try:
                    await c.disconnect()
                except Exception:
                    pass
        d = self.acc_dir(acc_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        # remove from gist too
        deleted_in_gist = self._sync_delete_account(acc_id)
        return deleted_in_gist

    # ------------------------------------------------------------------ #
    # LOGIN FLOW (phone -> OTP -> optional 2FA)
    # ------------------------------------------------------------------ #
    async def start_login(
        self,
        acc_id: str,
        api_id: int,
        api_hash: str,
        phone: str,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Create dir
        d = self.acc_dir(acc_id)
        d.mkdir(parents=True, exist_ok=True)

        # Persist meta
        meta = {
            "api_id": api_id,
            "api_hash": api_hash,
            "phone": phone,
            "name": name or phone,
            "created_at": int(time.time()),
        }
        self.save_meta(acc_id, meta)

        # Create a transient TelegramClient for the login flow
        client = TelegramClient(
            str(self.session_file(acc_id)).replace(".session", ""),
            api_id,
            api_hash,
        )
        await client.connect()
        result = await client.send_code_request(phone)
        self._login_clients[acc_id] = client

        # persist meta.json to gist so we don't lose api_id/api_hash on Render sleep
        synced = self._sync_push_account(acc_id)

        return {
            "phone_code_hash": result.phone_code_hash,
            "next_step": "POST /accounts/{acc_id}/otp  body={code}",
            "note": "If 2FA enabled, an additional POST /accounts/{acc_id}/2fa will be required.",
            "gist_synced": synced,
        }

    async def submit_otp(self, acc_id: str, code: str) -> Dict[str, Any]:
        client = self._login_clients.get(acc_id)
        if not client:
            raise HTTPException(400, "No active login flow. Call /accounts/new first.")
        meta = self.load_meta(acc_id)
        try:
            await client.sign_in(
                phone=meta["phone"],
                code=code,
                password=None,
            )
        except Exception as e:
            err = str(e)
            # 2FA required?
            if "Two-steps verification" in err or "SessionPasswordNeeded" in type(e).__name__:
                return {
                    "status": "2fa_required",
                    "next_step": "POST /accounts/{acc_id}/2fa  body={password}",
                }
            raise HTTPException(400, f"OTP sign-in failed: {err}")

        # Success — finalize
        me = await client.get_me()
        meta["user_id"] = me.id
        meta["username"] = me.username
        meta["first_name"] = me.first_name
        meta["last_used"] = int(time.time())
        self.save_meta(acc_id, meta)

        # Move the transient client into op pool (already authorized)
        self._op_clients[acc_id] = client
        self._login_clients.pop(acc_id, None)

        # session.session was just created/updated by Telethon — push to gist
        synced = self._sync_push_account(acc_id)

        return {
            "status": "authorized",
            "user": _serialize_user(me),
            "gist_synced": synced,
        }

    async def submit_2fa(self, acc_id: str, password: str) -> Dict[str, Any]:
        client = self._login_clients.get(acc_id) or self._op_clients.get(acc_id)
        if not client:
            raise HTTPException(400, "No active login flow. Call /accounts/new first.")
        try:
            await client.sign_in(password=password)
        except Exception as e:
            raise HTTPException(400, f"2FA sign-in failed: {e}")

        me = await client.get_me()
        meta = self.load_meta(acc_id)
        meta["user_id"] = me.id
        meta["username"] = me.username
        meta["first_name"] = me.first_name
        meta["last_used"] = int(time.time())
        self.save_meta(acc_id, meta)

        self._op_clients[acc_id] = client
        self._login_clients.pop(acc_id, None)

        # session updated after 2FA success — push to gist
        synced = self._sync_push_account(acc_id)

        return {
            "status": "authorized",
            "user": _serialize_user(me),
            "gist_synced": synced,
        }

    # ------------------------------------------------------------------ #
    # SESSION UPLOAD (zip or single .session)
    # ------------------------------------------------------------------ #
    async def upload_session(self, acc_id: str, file: UploadFile) -> Dict[str, Any]:
        # Accept either:
        #   1) a single .session file   -> saved as session.session
        #   2) a zip containing:
        #        session.session           (required)
        #        meta.json                 (optional: api_id, api_hash, phone, name)
        raw = await file.read()
        d = self.acc_dir(acc_id)
        d.mkdir(parents=True, exist_ok=True)

        fname = (file.filename or "").lower()
        if fname.endswith(".zip") or raw[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = zf.namelist()
                # find session file
                sess_name = None
                for n in names:
                    if n.lower().endswith(".session") and not n.startswith("__MACOSX"):
                        sess_name = n
                        break
                if not sess_name:
                    raise HTTPException(400, "Zip must contain a *.session file")
                # extract session
                self.session_file(acc_id).parent.mkdir(parents=True, exist_ok=True)
                self.session_file(acc_id).write_bytes(zf.read(sess_name))
                # extract meta if present
                for n in names:
                    base = os.path.basename(n).lower()
                    if base == "meta.json" and not n.startswith("__MACOSX"):
                        try:
                            incoming = json.loads(zf.read(n))
                            existing = self.load_meta(acc_id)
                            existing.update(incoming)
                            existing.setdefault("created_at", int(time.time()))
                            self.save_meta(acc_id, existing)
                        except Exception:
                            pass
        elif fname.endswith(".session"):
            self.session_file(acc_id).parent.mkdir(parents=True, exist_ok=True)
            self.session_file(acc_id).write_bytes(raw)
        else:
            raise HTTPException(
                400,
                "Upload must be a .session file or a .zip containing a .session file",
            )

        meta = self.load_meta(acc_id)
        synced = self._sync_push_account(acc_id)
        return {
            "account_id": acc_id,
            "saved": True,
            "session_file": str(self.session_file(acc_id)),
            "has_meta": bool(meta),
            "meta": meta if meta else None,
            "gist_synced": synced,
            "next_step": (
                "If meta has no api_id/api_hash, POST /accounts/{acc_id}/meta with them."
            ),
        }

    async def set_meta(self, acc_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        meta = self.load_meta(acc_id)
        # only allow whitelisted keys
        for k in ("api_id", "api_hash", "phone", "name"):
            if k in patch and patch[k] is not None:
                meta[k] = patch[k]
        meta.setdefault("created_at", int(time.time()))
        self.save_meta(acc_id, meta)
        synced = self._sync_push_account(acc_id)
        return {"account_id": acc_id, "meta": meta, "gist_synced": synced}

    async def download_session(self, acc_id: str):
        """Stream the account dir as a zip (for backup before Render sleeps)."""
        self.require_account(acc_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if self.session_file(acc_id).exists():
                zf.write(self.session_file(acc_id), arcname="session.session")
            if self.meta_file(acc_id).exists():
                zf.write(self.meta_file(acc_id), arcname="meta.json")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={acc_id}.zip"},
        )

    # ------------------------------------------------------------------ #
    # OPERATION EXECUTION
    # ------------------------------------------------------------------ #
    async def _get_client(self, acc_id: str) -> TelegramClient:
        meta = self.require_account(acc_id)
        if "api_id" not in meta or "api_hash" not in meta:
            raise HTTPException(400, "Missing api_id/api_hash for this account. Set via /accounts/{id}/meta")
        # Use existing op client if present and connected
        client = self._op_clients.get(acc_id)
        if client and client.is_connected():
            return client
        # else create a fresh client from the saved session file
        sess_path = str(self.session_file(acc_id)).replace(".session", "")
        client = TelegramClient(sess_path, meta["api_id"], meta["api_hash"])
        await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(401, "Session not authorized. Re-login or upload a valid session.")
        self._op_clients[acc_id] = client
        # update last_used + push meta to gist
        meta["last_used"] = int(time.time())
        self.save_meta(acc_id, meta)
        # also push session.session (Telethon may have written updates to it)
        self._sync_push_account(acc_id)
        return client

    async def run(self, acc_id: str, fn: Callable):
        client = await self._get_client(acc_id)
        return await fn(client)

    # ------------------------------------------------------------------ #
    # HIGH-LEVEL OPERATIONS
    # ------------------------------------------------------------------ #
    async def get_me(self, acc_id: str) -> Dict[str, Any]:
        client = await self._get_client(acc_id)
        me = await client.get_me()
        return _serialize_user(me)

    async def list_dialogs(
        self,
        acc_id: str,
        limit: int = 100,
        filter_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        client = await self._get_client(acc_id)
        out: List[Dict[str, Any]] = []
        async for d in client.iter_dialogs(limit=limit):
            ent = d.entity
            etype = _entity_type(ent)
            if filter_type and etype != filter_type:
                continue
            out.append({
                "id": d.id,
                "name": d.name,
                "type": etype,
                "username": getattr(ent, "username", None),
                "is_channel": isinstance(ent, Channel),
                "is_group": isinstance(ent, (Chat,)) or (isinstance(ent, Channel) and getattr(ent, "megagroup", False)),
                "unread_count": d.unread_count,
                "last_message_date": d.date.isoformat() if d.date else None,
            })
        return out

    async def send_message(self, acc_id: str, peer: str, message: str) -> Dict[str, Any]:
        client = await self._get_client(acc_id)
        ent = await client.get_entity(peer)
        msg = await client.send_message(ent, message)
        return {
            "ok": True,
            "peer": peer,
            "peer_id": getattr(ent, "id", None),
            "message_id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
        }

    async def visit_chat(self, acc_id: str, peer: str, limit: int = 20) -> Dict[str, Any]:
        """Mark a chat as read and pull recent messages."""
        client = await self._get_client(acc_id)
        ent = await client.get_entity(peer)
        # mark as read
        try:
            await client.send_read_acknowledge(ent)
        except Exception:
            pass
        msgs = []
        async for m in client.iter_messages(ent, limit=limit):
            msgs.append({
                "id": m.id,
                "date": m.date.isoformat() if m.date else None,
                "sender_id": m.sender_id,
                "text": (m.text or "")[:300],
                "is_sponsored": getattr(m, "sponsored", False) is True,
            })
        return {
            "peer": peer,
            "peer_id": getattr(ent, "id", None),
            "marked_read": True,
            "messages": msgs,
        }

    async def click_ads(self, acc_id: str, peer: str, limit: int = 5, click_media: bool = False) -> Dict[str, Any]:
        """Fetch sponsored messages in a channel and click them via Telegram's
        ClickSponsoredMessageRequest.

        Returns ad metadata (title, message, button_text, url, random_id) +
        click_result (ok: bool, response, error) for each ad.

        Args:
            click_media: if True, also send media=True (registers the click as
                         "user viewed the media" — for ads with photo/video).
        """
        client = await self._get_client(acc_id)
        ent = await client.get_entity(peer)

        try:
            res = await client(GetSponsoredMessagesRequest(peer=ent))
        except Exception as e:
            return {"ok": False, "peer": peer, "error": f"getSponsoredMessages failed: {e}"}

        msgs = getattr(res, "messages", []) or []
        from telethon.tl.functions.messages import ClickSponsoredMessageRequest

        clicked = 0
        ads = []
        for sm in msgs[:limit]:
            random_id = getattr(sm, "random_id", None)
            url = getattr(sm, "url", None)
            title = getattr(sm, "title", None)
            message = getattr(sm, "message", None)
            button_text = getattr(sm, "button_text", None)
            has_media = getattr(sm, "media", None) is not None

            click_info = {"ok": False, "response": None, "error": None}
            if random_id is not None:
                try:
                    resp = await client(ClickSponsoredMessageRequest(random_id=random_id))
                    click_info["ok"] = True
                    click_info["response"] = repr(resp)
                    clicked += 1
                    # Also send media=True if the ad has media and caller requested it
                    if click_media and has_media:
                        try:
                            resp_m = await client(ClickSponsoredMessageRequest(
                                random_id=random_id, media=True,
                            ))
                            click_info["media_click_response"] = repr(resp_m)
                        except Exception as e2:
                            click_info["media_click_error"] = str(e2)
                except Exception as e:
                    click_info["error"] = f"{type(e).__name__}: {e}"

            ads.append({
                "random_id_hex": random_id.hex() if isinstance(random_id, bytes) else (
                    str(random_id) if random_id is not None else None
                ),
                "url": url,
                "title": title,
                "message": (message or "")[:300],
                "button_text": button_text,
                "has_media": has_media,
                "click_result": click_info,
            })
        return {
            "ok": True,
            "peer": peer,
            "peer_id": getattr(ent, "id", None),
            "sponsored_count": len(ads),
            "clicked_count": clicked,
            "ads": ads,
        }

    async def fetch_ads(self, acc_id: str, peer: str, limit: int = 20) -> Dict[str, Any]:
        """Fetch sponsored messages WITHOUT clicking. Returns ads with random_id_hex.

        Used by workflows that need to inspect ads first, then click each one
        with custom delays / refresh between clicks.
        """
        client = await self._get_client(acc_id)
        ent = await client.get_entity(peer)
        try:
            res = await client(GetSponsoredMessagesRequest(peer=ent))
        except Exception as e:
            return {"ok": False, "peer": peer, "error": f"getSponsoredMessages failed: {e}"}

        msgs = getattr(res, "messages", []) or []
        ads = []
        for sm in msgs[:limit]:
            rid = getattr(sm, "random_id", None)
            ads.append({
                "random_id_hex": rid.hex() if isinstance(rid, bytes) else (
                    str(rid) if rid is not None else None
                ),
                "url": getattr(sm, "url", None),
                "title": getattr(sm, "title", None),
                "message": (getattr(sm, "message", None) or "")[:300],
                "button_text": getattr(sm, "button_text", None),
                "has_media": getattr(sm, "media", None) is not None,
            })
        return {
            "ok": True,
            "peer": peer,
            "peer_id": getattr(ent, "id", None),
            "ads_count": len(ads),
            "ads": ads,
        }

    async def click_one_ad(
        self,
        acc_id: str,
        peer: str,
        random_id_hex: str,
        click_media: bool = True,
    ) -> Dict[str, Any]:
        """Click ONE specific sponsored ad by its random_id_hex.

        Workflow-friendly: pair with `fetch_ads` + a loop in a workflow to
        click ads one at a time with custom delays between each click.

        Args:
            random_id_hex: hex string of the ad's random_id (from fetch_ads response)
            click_media: also send media=True (registers media view for ads with photo/video)
        """
        from telethon.tl.functions.messages import ClickSponsoredMessageRequest

        client = await self._get_client(acc_id)
        ent = await client.get_entity(peer)

        # Convert hex string back to bytes
        try:
            rid_bytes = bytes.fromhex(random_id_hex)
        except ValueError:
            return {"ok": False, "error": f"invalid random_id_hex: {random_id_hex}"}

        result = {"ok": False, "random_id_hex": random_id_hex, "click_response": None, "error": None}
        try:
            resp = await client(ClickSponsoredMessageRequest(random_id=rid_bytes))
            result["ok"] = True
            result["click_response"] = repr(resp)
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            return result

        if click_media:
            try:
                resp_m = await client(ClickSponsoredMessageRequest(random_id=rid_bytes, media=True))
                result["media_click_response"] = repr(resp_m)
            except Exception as e:
                result["media_click_error"] = str(e)

        return result

    async def disconnect_client(self, acc_id: str) -> Dict[str, Any]:
        """Forcefully disconnect a Telegram client (to save quota / avoid 24/7 online).

        Use after a workflow finishes clicking ads so the account appears offline
        to Telegram (less suspicious than always-online).
        """
        disconnected = False
        for store in (self._login_clients, self._op_clients):
            c = store.pop(acc_id, None)
            if c:
                try:
                    if c.is_connected():
                        await c.disconnect()
                    disconnected = True
                except Exception:
                    pass
        return {"disconnected": disconnected, "account_id": acc_id}


# ---------------------------------------------------------------------- #
# SERIALIZERS
# ---------------------------------------------------------------------- #
def _serialize_user(me) -> Dict[str, Any]:
    return {
        "id": me.id,
        "first_name": me.first_name,
        "last_name": me.last_name,
        "username": me.username,
        "phone": getattr(me, "phone", None),
        "bot": getattr(me, "bot", False),
        "premium": getattr(me, "premium", False),
    }


def _entity_type(ent) -> str:
    if isinstance(ent, User):
        return "user"
    if isinstance(ent, Channel):
        if getattr(ent, "megagroup", False):
            return "group"
        return "channel"
    if isinstance(ent, Chat):
        return "group"
    return "unknown"
