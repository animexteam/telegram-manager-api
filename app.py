"""
app.py — FastAPI entrypoint for the Telegram Manager API.

Run locally:
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Run on Render (start command in render.yaml / Render dashboard):
    uvicorn app:app --host 0.0.0.0 --port $PORT
"""
import os
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tele_manager import TeleManager
from gist_store import GistStorage
from automations import AutomationManager


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Optional GitHub Gist persistence (recommended on Render free tier)
GITHUB_GIST_TOKEN = os.getenv("GITHUB_GIST_TOKEN", "").strip()
GIST_ID = os.getenv("GIST_ID", "").strip()

STARTED_AT = time.time()
log = logging.getLogger("tg_manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ----------------------------------------------------------------------
# APP
# ----------------------------------------------------------------------
app = FastAPI(
    title="Telegram Manager API",
    version="1.0.0",
    description=(
        "Operate & manage multiple Telegram accounts/sessions via HTTP. "
        "Upload existing Telethon .session files (zip or single), or log in "
        "fresh with phone + OTP + 2FA. Then list chats, send messages, "
        "visit channels, and click sponsored ads."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = TeleManager(
    SESSIONS_DIR,
    gist_store=GistStorage(GITHUB_GIST_TOKEN, GIST_ID) if GITHUB_GIST_TOKEN else None,
)

# Automation manager — scheduled task runner (cron-driven)
automations_mgr = AutomationManager(
    DATA_DIR,
    tele_manager=manager,
    gist_store=GistStorage(GITHUB_GIST_TOKEN, GIST_ID) if GITHUB_GIST_TOKEN else None,
)


# ----------------------------------------------------------------------
# STARTUP — auto-pull sessions from gist so Render sleep doesn't lose them
# ----------------------------------------------------------------------
@app.on_event("startup")
async def _startup():
    if manager.gist and manager.gist.is_configured():
        if manager.gist.has_gist():
            try:
                pulled = manager.sync_pull_all()
                log.info("startup: pulled %d account(s) from gist: %s",
                         len(pulled), list(pulled.keys()))
            except Exception as e:
                log.warning("startup: gist pull failed: %s", e)
        else:
            log.warning("startup: GITHUB_GIST_TOKEN set but GIST_ID not set. "
                        "Call POST /sync/init to create a gist, then set GIST_ID env var.")
    else:
        log.info("startup: gist persistence not configured (GITHUB_GIST_TOKEN empty)")

    # Start the automation scheduler (also loads automations.json from gist)
    try:
        automations_mgr.start()
        log.info("startup: automations scheduler started, %d job(s) loaded",
                 len(automations_mgr.list_automations()))
    except Exception as e:
        log.error("startup: automations scheduler failed: %s", e)


# ----------------------------------------------------------------------
# AUTH DEPENDENCY (optional — enforced only if MASTER_API_KEY is set)
# ----------------------------------------------------------------------
def require_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if MASTER_API_KEY:
        if x_api_key != MASTER_API_KEY:
            raise HTTPException(401, "Invalid or missing X-API-Key header")
    return True


# ----------------------------------------------------------------------
# KEEP-ALIVE / HEALTH
# ----------------------------------------------------------------------
@app.get("/ping", tags=["health"])
async def ping():
    """Wake-up endpoint. Hits this to bring the Render service out of sleep."""
    return {
        "status": "ok",
        "service": "telegram-manager-api",
        "uptime_sec": int(time.time() - STARTED_AT),
        "ts": int(time.time()),
        "accounts": len([p for p in SESSIONS_DIR.glob("acc_*") if p.is_dir()]),
    }


@app.get("/", tags=["health"])
async def root():
    return {
        "name": "Telegram Manager API",
        "docs": "/docs",
        "ping": "/ping",
        "openapi": "/openapi.json",
    }


# ----------------------------------------------------------------------
# MODELS
# ----------------------------------------------------------------------
class NewAccountReq(BaseModel):
    api_id: int
    api_hash: str
    phone: str
    name: Optional[str] = None


class OtpReq(BaseModel):
    code: str


class TwoFAReq(BaseModel):
    password: str


class MetaReq(BaseModel):
    api_id: int
    api_hash: str
    phone: Optional[str] = None
    name: Optional[str] = None


class SendMsgReq(BaseModel):
    peer: str
    message: str


class VisitReq(BaseModel):
    peer: str
    limit: int = 20


class ClickAdsReq(BaseModel):
    peer: str
    limit: int = 5


# ----------------------------------------------------------------------
# ACCOUNT MANAGEMENT
# ----------------------------------------------------------------------
@app.get("/accounts", tags=["accounts"], dependencies=[Depends(require_key)])
async def list_accounts():
    return await manager.list_accounts()


@app.post("/accounts/new", tags=["accounts"], dependencies=[Depends(require_key)])
async def new_account(req: NewAccountReq):
    """Start a new login flow. Returns phone_code_hash + next steps."""
    import uuid
    acc_id = f"acc_{uuid.uuid4().hex[:12]}"
    result = await manager.start_login(acc_id, req.api_id, req.api_hash, req.phone, req.name)
    return {"account_id": acc_id, **result}


@app.get("/accounts/{acc_id}", tags=["accounts"], dependencies=[Depends(require_key)])
async def get_account(acc_id: str):
    return await manager.get_account_info(acc_id)


@app.post("/accounts/{acc_id}/meta", tags=["accounts"], dependencies=[Depends(require_key)])
async def set_meta(acc_id: str, req: MetaReq):
    return await manager.set_meta(acc_id, req.dict())


@app.delete("/accounts/{acc_id}", tags=["accounts"], dependencies=[Depends(require_key)])
async def delete_account(acc_id: str):
    await manager.delete_account(acc_id)
    return {"deleted": acc_id}


@app.post("/accounts/{acc_id}/upload", tags=["accounts"], dependencies=[Depends(require_key)])
async def upload_session(acc_id: str, file: UploadFile = File(...)):
    """Upload an existing Telethon session.

    Accepted formats:
      - single .session file
      - .zip containing:
          session.session        (required)
          meta.json              (optional: {api_id, api_hash, phone, name})

    The acc_id can be any string but must match what you'll use in subsequent calls.
    Convention: acc_<12hex>. Tip: hit GET /accounts to see existing IDs first.
    """
    # Make sure the account dir exists even if this is the first upload
    manager.acc_dir(acc_id).mkdir(parents=True, exist_ok=True)
    return await manager.upload_session(acc_id, file)


@app.get("/accounts/{acc_id}/download", tags=["accounts"], dependencies=[Depends(require_key)])
async def download_session(acc_id: str):
    """Download the session+meta as a zip — useful to back up before Render sleeps."""
    return await manager.download_session(acc_id)


# ----------------------------------------------------------------------
# LOGIN FLOW
# ----------------------------------------------------------------------
@app.post("/accounts/{acc_id}/otp", tags=["login"], dependencies=[Depends(require_key)])
async def submit_otp(acc_id: str, req: OtpReq):
    return await manager.submit_otp(acc_id, req.code)


@app.post("/accounts/{acc_id}/2fa", tags=["login"], dependencies=[Depends(require_key)])
async def submit_2fa(acc_id: str, req: TwoFAReq):
    return await manager.submit_2fa(acc_id, req.password)


# ----------------------------------------------------------------------
# TELEGRAM OPERATIONS
# ----------------------------------------------------------------------
@app.get("/accounts/{acc_id}/me", tags=["ops"], dependencies=[Depends(require_key)])
async def get_me(acc_id: str):
    return await manager.get_me(acc_id)


@app.get("/accounts/{acc_id}/chats", tags=["ops"], dependencies=[Depends(require_key)])
async def list_chats(acc_id: str, limit: int = 100):
    return await manager.list_dialogs(acc_id, limit=limit)


@app.get("/accounts/{acc_id}/channels", tags=["ops"], dependencies=[Depends(require_key)])
async def list_channels(acc_id: str, limit: int = 100):
    return await manager.list_dialogs(acc_id, limit=limit, filter_type="channel")


@app.get("/accounts/{acc_id}/groups", tags=["ops"], dependencies=[Depends(require_key)])
async def list_groups(acc_id: str, limit: int = 100):
    return await manager.list_dialogs(acc_id, limit=limit, filter_type="group")


@app.post("/accounts/{acc_id}/send", tags=["ops"], dependencies=[Depends(require_key)])
async def send_message(acc_id: str, req: SendMsgReq):
    return await manager.send_message(acc_id, req.peer, req.message)


@app.post("/accounts/{acc_id}/visit", tags=["ops"], dependencies=[Depends(require_key)])
async def visit_chat(acc_id: str, req: VisitReq):
    return await manager.visit_chat(acc_id, req.peer, req.limit)


@app.post("/accounts/{acc_id}/click-ads", tags=["ops"], dependencies=[Depends(require_key)])
async def click_ads(acc_id: str, req: ClickAdsReq):
    return await manager.click_ads(acc_id, req.peer, req.limit)


# ----------------------------------------------------------------------
# ERROR HANDLING
# ----------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": str(exc)},
    )


# ----------------------------------------------------------------------
# SYNC (GitHub Gist persistence)
# ----------------------------------------------------------------------
@app.get("/sync/status", tags=["sync"], dependencies=[Depends(require_key)])
async def sync_status():
    if not manager.gist:
        return {
            "configured": False,
            "reason": "GITHUB_GIST_TOKEN env var not set",
        }
    s = manager.gist.status()
    return {"configured": True, **s}


@app.post("/sync/init", tags=["sync"], dependencies=[Depends(require_key)])
async def sync_init():
    """Create a new private gist for session storage. Returns gist_id.

    After calling this, copy the returned gist_id into the GIST_ID env var
    on Render (or in .env locally) and restart the service.
    """
    if not manager.gist:
        raise HTTPException(400, "GITHUB_GIST_TOKEN env var not set. Cannot init gist.")
    if manager.gist.has_gist():
        return {
            "already_initialized": True,
            "gist_id": manager.gist.gist_id,
            "gist_url": f"https://gist.github.com/{manager.gist.gist_id}",
            "note": "GIST_ID is already set. To use a fresh gist, update GIST_ID env var.",
        }
    data = manager.gist.create_gist()
    return {
        "created": True,
        "gist_id": data["id"],
        "gist_url": data["html_url"],
        "owner": data["owner"]["login"],
        "public": data["public"],
        "next_step": (
            "Copy gist_id into the GIST_ID env var on Render "
            "(or locally in .env), then restart the service."
        ),
    }


@app.post("/sync/pull", tags=["sync"], dependencies=[Depends(require_key)])
async def sync_pull():
    """Pull all session files from gist → local disk."""
    if not manager.gist or not manager.gist.has_gist():
        raise HTTPException(400, "Gist not configured. Set GITHUB_GIST_TOKEN and GIST_ID env vars.")
    pulled = manager.sync_pull_all()
    return {"pulled": pulled, "count": len(pulled)}


@app.post("/sync/push", tags=["sync"], dependencies=[Depends(require_key)])
async def sync_push():
    """Push all local accounts → gist."""
    if not manager.gist or not manager.gist.has_gist():
        raise HTTPException(400, "Gist not configured. Set GITHUB_GIST_TOKEN and GIST_ID env vars.")
    pushed = manager.sync_push_all()
    return {"pushed": pushed, "count": len(pushed)}


# ----------------------------------------------------------------------
# AUTOMATIONS (scheduled tasks)
# ----------------------------------------------------------------------
class AutomationCreateReq(BaseModel):
    name: str
    account_id: str
    action: str = "send_message"
    peer: Optional[str] = None
    message: Optional[str] = None
    cron: str                       # standard 5-field cron (UTC)
    enabled: bool = True


class AutomationUpdateReq(BaseModel):
    name: Optional[str] = None
    account_id: Optional[str] = None
    action: Optional[str] = None
    peer: Optional[str] = None
    message: Optional[str] = None
    cron: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/automations", tags=["automations"], dependencies=[Depends(require_key)])
async def list_automations():
    return automations_mgr.list_automations()


@app.post("/automations", tags=["automations"], dependencies=[Depends(require_key)])
async def create_automation(req: AutomationCreateReq):
    """Create a new scheduled automation.

    Cron format (5 fields, UTC):
      minute hour day-of-month month day-of-week

    Examples:
      "0 9 * * *"        → daily at 09:00 UTC
      "*/30 * * * *"     → every 30 minutes
      "0 9 * * 1-5"      → weekdays at 09:00 UTC
      "0 0 1 * *"        → 1st of every month at midnight UTC
    """
    try:
        return automations_mgr.create_automation(
            name=req.name,
            account_id=req.account_id,
            action=req.action,
            peer=req.peer,
            message=req.message,
            cron=req.cron,
            enabled=req.enabled,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/automations/{aid}", tags=["automations"], dependencies=[Depends(require_key)])
async def get_automation(aid: str):
    a = automations_mgr.get_automation(aid)
    if not a:
        raise HTTPException(404, f"automation {aid} not found")
    return a


@app.patch("/automations/{aid}", tags=["automations"], dependencies=[Depends(require_key)])
async def update_automation(aid: str, req: AutomationUpdateReq):
    try:
        updated = automations_mgr.update_automation(aid, req.dict(exclude_unset=True))
        return updated
    except KeyError:
        raise HTTPException(404, f"automation {aid} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/automations/{aid}", tags=["automations"], dependencies=[Depends(require_key)])
async def delete_automation(aid: str):
    deleted = automations_mgr.delete_automation(aid)
    if not deleted:
        raise HTTPException(404, f"automation {aid} not found")
    return {"deleted": aid}


@app.post("/automations/{aid}/trigger", tags=["automations"], dependencies=[Depends(require_key)])
async def trigger_automation_now(aid: str):
    """Run an automation immediately, outside its cron schedule.

    Useful for testing. Result is async — check /automations/{aid} for last_result.
    """
    try:
        return automations_mgr.trigger_now(aid)
    except KeyError:
        raise HTTPException(404, f"automation {aid} not found")


# ----------------------------------------------------------------------
# SHUTDOWN — disconnect all transient clients
# ----------------------------------------------------------------------
@app.on_event("shutdown")
async def _shutdown():
    # stop scheduler first
    try:
        automations_mgr.stop()
    except Exception as e:
        log.warning("shutdown: automations stop error: %s", e)

    for store in (manager._login_clients, manager._op_clients):
        for c in list(store.values()):
            try:
                if c.is_connected():
                    await c.disconnect()
            except Exception:
                pass
        store.clear()
