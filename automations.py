"""
automations.py — scheduled task automation for Telegram accounts.

Design:
  - Each automation has: id, name, account_id, action, peer, message, cron, enabled
  - Persisted locally in automations.json + mirrored to GitHub gist (so survives Render sleep/redeploy)
  - APScheduler's AsyncIOScheduler runs jobs in the same event loop as FastAPI
  - Supported actions: send_message (more can be added)
  - Each job logs last_run + last_result so you can inspect via /automations/{id}
"""
import os
import json
import time
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor

log = logging.getLogger("automations")


class AutomationManager:
    FILENAME = "automations.json"  # gist filename + local filename

    def __init__(self, data_dir: Path, tele_manager, gist_store=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.local_file = self.data_dir / self.FILENAME
        self.tele_manager = tele_manager
        self.gist = gist_store

        # scheduler
        self.scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": AsyncIOExecutor()},
            timezone="UTC",
        )
        # in-memory index: automation_id -> automation dict
        self._automations: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # PERSISTENCE (local + gist)
    # ------------------------------------------------------------------ #
    def _load_local(self) -> Dict[str, Dict[str, Any]]:
        if not self.local_file.exists():
            return {}
        try:
            data = json.loads(self.local_file.read_text() or "{}")
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning("failed to parse local automations.json: %s", e)
            return {}

    def _save_local(self) -> None:
        self.local_file.write_text(json.dumps(self._automations, indent=2, ensure_ascii=False))

    def _push_to_gist(self) -> List[str]:
        if not self.gist or not self.gist.has_gist():
            return []
        try:
            # write whole file as a single gist file
            self.gist.write_file(self.FILENAME, self.local_file.read_bytes())
            return [self.FILENAME]
        except Exception as e:
            log.warning("gist push failed: %s", e)
            return []

    def pull_from_gist(self) -> bool:
        """Pull automations.json from gist → local. Returns True if pulled."""
        if not self.gist or not self.gist.has_gist():
            return False
        try:
            data = self.gist.read_file(self.FILENAME)
            if data is None:
                return False
            self.local_file.write_bytes(data)
            self._automations = self._load_local()
            return True
        except Exception as e:
            log.warning("gist pull failed: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # LIFECYCLE
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Load persisted automations + start the scheduler."""
        # 1. Try gist first (in case local was wiped by Render sleep)
        pulled = self.pull_from_gist()
        if not pulled:
            self._automations = self._load_local()
            log.info("loaded %d automations from local file", len(self._automations))
        else:
            log.info("loaded %d automations from gist", len(self._automations))

        # 2. Start scheduler
        if not self.scheduler.running:
            self.scheduler.start()
            log.info("scheduler started")

        # 3. Re-schedule every enabled automation
        for aid, auto in list(self._automations.items()):
            if auto.get("enabled", True):
                self._schedule_job(auto)
            else:
                log.info("automation %s disabled — not scheduling", aid)

    def stop(self) -> None:
        if self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
                log.info("scheduler stopped")
            except Exception as e:
                log.warning("scheduler shutdown error: %s", e)

    # ------------------------------------------------------------------ #
    # SCHEDULING
    # ------------------------------------------------------------------ #
    def _schedule_job(self, auto: Dict[str, Any]) -> None:
        aid = auto["id"]
        # remove existing job if any
        try:
            self.scheduler.remove_job(aid)
        except Exception:
            pass

        try:
            trigger = CronTrigger.from_crontab(auto["cron"])
        except Exception as e:
            log.error("automation %s has invalid cron '%s': %s", aid, auto["cron"], e)
            auto["last_error"] = f"invalid cron: {e}"
            return

        self.scheduler.add_job(
            self._run_automation,
            trigger=trigger,
            args=[aid],
            id=aid,
            replace_existing=True,
            misfire_grace_time=300,
        )
        log.info("scheduled automation %s (%s) cron=%s", aid, auto.get("name"), auto["cron"])

    def _unschedule_job(self, aid: str) -> None:
        try:
            self.scheduler.remove_job(aid)
        except Exception:
            pass

    async def _run_automation(self, aid: str) -> None:
        auto = self._automations.get(aid)
        if not auto:
            log.warning("automation %s not found — skipping", aid)
            return
        if not auto.get("enabled", True):
            log.info("automation %s disabled — skipping run", aid)
            return

        action = auto.get("action", "send_message")
        acc_id = auto.get("account_id")
        peer = auto.get("peer")
        message = auto.get("message", "")

        log.info("running automation %s (%s): action=%s account=%s peer=%s",
                 aid, auto.get("name"), action, acc_id, peer)

        result: Dict[str, Any]
        try:
            if action == "send_message":
                if not peer or not message:
                    raise ValueError("send_message requires peer + message")
                result = await self.tele_manager.send_message(acc_id, peer, message)
            else:
                raise ValueError(f"unknown action: {action}")

            auto["last_run"] = int(time.time())
            auto["last_result"] = {"ok": True, **result}
            auto["last_error"] = None
            auto["run_count"] = auto.get("run_count", 0) + 1
            self._save_local()
            self._push_to_gist()
            log.info("automation %s OK: %s", aid, result)

        except Exception as e:
            auto["last_run"] = int(time.time())
            auto["last_error"] = str(e)
            auto["last_result"] = {"ok": False, "error": str(e)}
            auto["run_count"] = auto.get("run_count", 0) + 1
            self._save_local()
            self._push_to_gist()
            log.error("automation %s FAILED: %s", aid, e)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def list_automations(self) -> List[Dict[str, Any]]:
        out = []
        for aid, auto in self._automations.items():
            item = dict(auto)
            # attach scheduler status
            try:
                job = self.scheduler.get_job(aid)
                item["scheduled"] = job is not None
                item["next_run"] = (
                    job.next_run_time.isoformat() if job and job.next_run_time else None
                )
            except Exception:
                item["scheduled"] = False
                item["next_run"] = None
            out.append(item)
        return out

    def get_automation(self, aid: str) -> Optional[Dict[str, Any]]:
        auto = self._automations.get(aid)
        if not auto:
            return None
        item = dict(auto)
        try:
            job = self.scheduler.get_job(aid)
            item["scheduled"] = job is not None
            item["next_run"] = (
                job.next_run_time.isoformat() if job and job.next_run_time else None
            )
        except Exception:
            item["scheduled"] = False
            item["next_run"] = None
        return item

    def create_automation(
        self,
        name: str,
        account_id: str,
        action: str,
        peer: Optional[str],
        message: Optional[str],
        cron: str,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        # validate account exists
        try:
            self.tele_manager.require_account(account_id)
        except Exception as e:
            raise ValueError(f"invalid account_id: {e}")

        # validate cron
        try:
            CronTrigger.from_crontab(cron)
        except Exception as e:
            raise ValueError(f"invalid cron expression: {e}")

        # validate action
        if action not in ("send_message",):
            raise ValueError(f"unsupported action: {action}")
        if action == "send_message":
            if not peer or not message:
                raise ValueError("send_message requires peer + message")

        aid = f"auto_{uuid.uuid4().hex[:10]}"
        auto = {
            "id": aid,
            "name": name,
            "account_id": account_id,
            "action": action,
            "peer": peer,
            "message": message,
            "cron": cron,
            "enabled": enabled,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "run_count": 0,
            "last_run": None,
            "last_result": None,
            "last_error": None,
        }
        self._automations[aid] = auto
        self._save_local()
        self._push_to_gist()
        if enabled:
            self._schedule_job(auto)
        return self.get_automation(aid)

    def update_automation(self, aid: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        auto = self._automations.get(aid)
        if not auto:
            raise KeyError(f"automation {aid} not found")

        # allowed patchable fields
        for k in ("name", "account_id", "action", "peer", "message", "cron", "enabled"):
            if k in patch and patch[k] is not None:
                # validate critical fields
                if k == "cron":
                    try:
                        CronTrigger.from_crontab(patch[k])
                    except Exception as e:
                        raise ValueError(f"invalid cron: {e}")
                if k == "account_id":
                    try:
                        self.tele_manager.require_account(patch[k])
                    except Exception as e:
                        raise ValueError(f"invalid account_id: {e}")
                if k == "action" and patch[k] not in ("send_message",):
                    raise ValueError(f"unsupported action: {patch[k]}")
                auto[k] = patch[k]

        auto["updated_at"] = int(time.time())
        self._save_local()
        self._push_to_gist()

        # reschedule if needed
        if auto.get("enabled", True):
            self._schedule_job(auto)
        else:
            self._unschedule_job(aid)

        return self.get_automation(aid)

    def delete_automation(self, aid: str) -> bool:
        if aid not in self._automations:
            return False
        self._unschedule_job(aid)
        del self._automations[aid]
        self._save_local()
        self._push_to_gist()
        return True

    def trigger_now(self, aid: str) -> Dict[str, Any]:
        """Run an automation immediately (outside its cron schedule)."""
        if aid not in self._automations:
            raise KeyError(f"automation {aid} not found")
        # run as a task in the event loop
        asyncio.create_task(self._run_automation(aid))
        return {"triggered": aid, "note": "running in background, check /automations/{id} for result"}
