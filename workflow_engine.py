"""
workflow_engine.py — generic, dynamic workflow executor.

A workflow is a JSON-defined sequence of steps:
  [
    {"action": "fetch_ads", "params": {"peer": "@X", "limit": 20}, "store_as": "ads"},
    {"action": "loop", "over": "ads", "as": "ad", "delay_between": 120, "steps": [
        {"action": "click_one_ad",
         "params": {"peer": "@X", "random_id_hex": "{{ad.random_id_hex}}"},
         "delay_after": 45},
        {"action": "click_one_ad",
         "params": {"peer": "@X", "random_id_hex": "{{ad.random_id_hex}}"},
         "delay_after": 60},
        {"action": "click_one_ad",
         "params": {"peer": "@X", "random_id_hex": "{{ad.random_id_hex}}"},
         "delay_after": 50},
        {"action": "click_one_ad",
         "params": {"peer": "@X", "random_id_hex": "{{ad.random_id_hex}}"},
         "delay_after": 90}
    ]},
    {"action": "disconnect"}
  ]

Variable substitution: {{var}} or {{var.path}} — looked up in context.

Custom actions: register via POST /actions/register with Python source code
that defines `async def action(client, params) -> dict`. Code is exec'd in a
restricted namespace.

Workflows persist to gist (workflows.json) — survive Render sleep/redeploy.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("workflow_engine")


class WorkflowEngine:
    """Action registry + multi-step workflow executor."""

    VAR_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")

    def __init__(self, tele_manager, gist_store=None, data_dir: Optional[Path] = None):
        self.tele_manager = tele_manager
        self.gist = gist_store
        self.data_dir = Path(data_dir) if data_dir else None
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.workflows_file = self.data_dir / "workflows.json"
        else:
            self.workflows_file = None

        self.workflows: Dict[str, Dict[str, Any]] = {}
        self.actions: Dict[str, Callable] = {}
        self._register_builtins()

    # ------------------------------------------------------------------ #
    # BUILT-IN ACTIONS
    # ------------------------------------------------------------------ #
    def _register_builtins(self):
        self.register("send_message", self._a_send_message)
        self.register("click_ads", self._a_click_ads)
        self.register("fetch_ads", self._a_fetch_ads)
        self.register("click_one_ad", self._a_click_one_ad)
        self.register("visit_chat", self._a_visit_chat)
        self.register("read_history", self._a_read_history)
        self.register("get_me", self._a_get_me)
        self.register("list_dialogs", self._a_list_dialogs)
        self.register("join_channel", self._a_join_channel)
        self.register("leave_channel", self._a_leave_channel)
        self.register("wait", self._a_wait)
        self.register("disconnect", self._a_disconnect)
        self.register("log", self._a_log)
        self.register("run_code", self._a_run_code)

    def register(self, name: str, func: Callable):
        self.actions[name] = func

    def register_custom(self, name: str, source_code: str) -> str:
        """Register a custom action from Python source.

        source_code MUST define:
            async def action(client, params):
                # your logic here
                return {"ok": True, ...}
        """
        ns = {"asyncio": asyncio, "json": json, "time": time}
        try:
            exec(source_code, ns)
        except Exception as e:
            raise ValueError(f"failed to compile source: {type(e).__name__}: {e}")
        if "action" not in ns or not callable(ns["action"]):
            raise ValueError("source must define `async def action(client, params)`")
        self.actions[name] = ns["action"]
        return name

    def list_actions(self) -> List[str]:
        return sorted(self.actions.keys())

    # ------------------------------------------------------------------ #
    # WORKFLOW PERSISTENCE (local + gist)
    # ------------------------------------------------------------------ #
    def _load_workflows(self) -> None:
        if self.workflows_file and self.workflows_file.exists():
            try:
                self.workflows = json.loads(self.workflows_file.read_text() or "{}")
            except Exception as e:
                log.warning("failed to load workflows.json: %s", e)
                self.workflows = {}

    def _save_workflows(self) -> None:
        if not self.workflows_file:
            return
        self.workflows_file.write_text(json.dumps(self.workflows, indent=2, ensure_ascii=False))
        self._push_workflows_to_gist()

    def _push_workflows_to_gist(self) -> None:
        if not self.gist or not self.gist.has_gist() or not self.workflows_file:
            return
        try:
            self.gist.write_file("workflows.json", self.workflows_file.read_bytes())
        except Exception as e:
            log.warning("gist push workflows failed: %s", e)

    def pull_from_gist(self) -> bool:
        if not self.gist or not self.gist.has_gist():
            return False
        try:
            data = self.gist.read_file("workflows.json")
            if data is None:
                return False
            if self.workflows_file:
                self.workflows_file.write_bytes(data)
            self.workflows = json.loads(data.decode() or "{}")
            return True
        except Exception as e:
            log.warning("gist pull workflows failed: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # WORKFLOW CRUD
    # ------------------------------------------------------------------ #
    def create_workflow(self, name: str, steps: List[Dict], description: str = "") -> Dict:
        wid = f"wf_{uuid.uuid4().hex[:10]}"
        wf = {
            "id": wid,
            "name": name,
            "description": description,
            "steps": steps,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "run_count": 0,
            "last_run": None,
            "last_result": None,
        }
        self.workflows[wid] = wf
        self._save_workflows()
        return wf

    def get_workflow(self, wid: str) -> Optional[Dict]:
        return self.workflows.get(wid)

    def list_workflows(self) -> List[Dict]:
        return list(self.workflows.values())

    def update_workflow(self, wid: str, patch: Dict) -> Dict:
        wf = self.workflows.get(wid)
        if not wf:
            raise KeyError(f"workflow {wid} not found")
        for k in ("name", "description", "steps"):
            if k in patch and patch[k] is not None:
                wf[k] = patch[k]
        wf["updated_at"] = int(time.time())
        self._save_workflows()
        return wf

    def delete_workflow(self, wid: str) -> bool:
        if wid not in self.workflows:
            return False
        del self.workflows[wid]
        self._save_workflows()
        return True

    # ------------------------------------------------------------------ #
    # VARIABLE SUBSTITUTION
    # ------------------------------------------------------------------ #
    def _resolve_value(self, value, context):
        """Recursively resolve {{var}} placeholders in any value."""
        if isinstance(value, str):
            return self._resolve_string(value, context)
        if isinstance(value, dict):
            return {k: self._resolve_value(v, context) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(v, context) for v in value]
        return value

    def _resolve_string(self, s: str, context):
        # If entire string is a single {{var}}, return raw value (preserve type)
        m = re.fullmatch(self.VAR_PATTERN, s)
        if m:
            return self._lookup(m.group(1), context)
        # Otherwise, replace each occurrence with str(value)
        def repl(match):
            v = self._lookup(match.group(1), context)
            return str(v) if v is not None else ""
        return self.VAR_PATTERN.sub(repl, s)

    def _lookup(self, path: str, context) -> Any:
        """Look up dotted path: dict keys + list indices + object attrs."""
        cur = context
        for p in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(p)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(p)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(cur, object):
                cur = getattr(cur, p, None)
            else:
                return None
            if cur is None:
                return None
        return cur

    # ------------------------------------------------------------------ #
    # EXECUTION
    # ------------------------------------------------------------------ #
    async def execute_workflow(
        self,
        account_id: str,
        steps: List[Dict],
        context: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Execute a list of steps. Returns {steps: [...], context: {...}}."""
        context = dict(context or {})
        results = []

        for i, step in enumerate(steps):
            action_name = step.get("action")
            params = self._resolve_value(step.get("params", {}), context)
            delay_after = float(step.get("delay_after", 0) or 0)
            store_as = step.get("store_as")

            log.info("workflow step %d: action=%s params=%s", i, action_name, params)

            if action_name == "loop":
                list_var = step.get("over") or params.get("over")
                item_var = step.get("as") or params.get("as", "item")
                delay_between = float(step.get("delay_between", 0) or 0)
                if list_var is None:
                    raise ValueError("loop requires 'over' (variable name or list)")
                if isinstance(list_var, str):
                    items = self._lookup(list_var, context) or []
                else:
                    items = list_var or []
                sub_steps = step.get("steps", [])
                sub_results = []
                for j, item in enumerate(items):
                    context[item_var] = item
                    sub_res = await self._execute_sub_steps(sub_steps, account_id, context)
                    sub_results.append({"index": j, "item": item, "steps": sub_res})
                    if delay_between > 0 and j < len(items) - 1:
                        log.info("loop delay_between: %.1fs", delay_between)
                        await asyncio.sleep(delay_between)
                result = {"iterations": len(items), "results": sub_results}
            elif action_name not in self.actions:
                raise ValueError(f"unknown action: {action_name}")
            else:
                client = await self.tele_manager._get_client(account_id)
                try:
                    result = await self.actions[action_name](client, params)
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    log.error("action %s failed: %s", action_name, e)

            if store_as:
                context[store_as] = result
            results.append({"step": i, "action": action_name, "result": result})

            if delay_after > 0:
                log.info("step %d delay_after: %.1fs", i, delay_after)
                await asyncio.sleep(delay_after)

        return {"steps": results, "context": context}

    async def _execute_sub_steps(
        self, steps: List[Dict], account_id: str, context: Dict
    ) -> List[Dict]:
        """Execute sub-steps (called by loop)."""
        out = []
        for i, step in enumerate(steps):
            action_name = step.get("action")
            params = self._resolve_value(step.get("params", {}), context)
            delay_after = float(step.get("delay_after", 0) or 0)
            store_as = step.get("store_as")

            if action_name == "loop":
                list_var = step.get("over") or params.get("over")
                item_var = step.get("as") or params.get("as", "item")
                delay_between = float(step.get("delay_between", 0) or 0)
                items = self._lookup(list_var, context) if isinstance(list_var, str) else list_var
                items = items or []
                sub_results = []
                for j, item in enumerate(items):
                    context[item_var] = item
                    inner = await self._execute_sub_steps(step.get("steps", []), account_id, context)
                    sub_results.append({"index": j, "steps": inner})
                    if delay_between > 0 and j < len(items) - 1:
                        await asyncio.sleep(delay_between)
                result = {"iterations": len(items), "results": sub_results}
            elif action_name not in self.actions:
                raise ValueError(f"unknown action: {action_name}")
            else:
                client = await self.tele_manager._get_client(account_id)
                try:
                    result = await self.actions[action_name](client, params)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}

            if store_as:
                context[store_as] = result
            out.append({"step": i, "action": action_name, "result": result})

            if delay_after > 0:
                await asyncio.sleep(delay_after)
        return out

    # ------------------------------------------------------------------ #
    # BUILT-IN ACTION IMPLEMENTATIONS
    # ------------------------------------------------------------------ #
    async def _a_send_message(self, client, params):
        peer = params["peer"]
        message = params["message"]
        msg = await client.send_message(peer, message)
        return {"ok": True, "peer": peer, "message_id": msg.id,
                "date": msg.date.isoformat() if msg.date else None}

    async def _a_click_ads(self, client, params):
        peer = params["peer"]
        limit = int(params.get("limit", 5))
        click_media = bool(params.get("click_media", True))
        clicks_per_ad = int(params.get("clicks_per_ad", 1))
        delay_between_clicks = float(params.get("delay_between_clicks", 30))

        from telethon.tl.functions.messages import (
            GetSponsoredMessagesRequest,
            ClickSponsoredMessageRequest,
        )
        ent = await client.get_entity(peer)
        res = await client(GetSponsoredMessagesRequest(peer=ent))
        msgs = getattr(res, "messages", []) or []
        ads_results = []
        clicked_total = 0
        for sm in msgs[:limit]:
            rid = sm.random_id
            ad_info = {
                "random_id_hex": rid.hex() if isinstance(rid, bytes) else str(rid),
                "url": getattr(sm, "url", None),
                "title": getattr(sm, "title", None),
                "clicks_succeeded": 0,
            }
            for _ in range(clicks_per_ad):
                try:
                    r = await client(ClickSponsoredMessageRequest(random_id=rid))
                    if r is True or r == True:
                        ad_info["clicks_succeeded"] += 1
                        clicked_total += 1
                except Exception as e:
                    ad_info["last_error"] = str(e)
                if delay_between_clicks > 0:
                    await asyncio.sleep(delay_between_clicks)
            ads_results.append(ad_info)
        return {
            "ok": True,
            "peer": peer,
            "ads_found": len(msgs),
            "ads_clicked": len(ads_results),
            "total_clicks_registered": clicked_total,
            "ads": ads_results,
        }

    async def _a_fetch_ads(self, client, params):
        peer = params["peer"]
        limit = int(params.get("limit", 20))
        from telethon.tl.functions.messages import GetSponsoredMessagesRequest
        ent = await client.get_entity(peer)
        res = await client(GetSponsoredMessagesRequest(peer=ent))
        msgs = getattr(res, "messages", []) or []
        ads = []
        for sm in msgs[:limit]:
            rid = sm.random_id
            ads.append({
                "random_id_hex": rid.hex() if isinstance(rid, bytes) else str(rid),
                "url": getattr(sm, "url", None),
                "title": getattr(sm, "title", None),
                "message": (getattr(sm, "message", None) or "")[:200],
                "button_text": getattr(sm, "button_text", None),
                "has_media": getattr(sm, "media", None) is not None,
            })
        return {"ok": True, "peer": peer, "ads_count": len(ads), "ads": ads}

    async def _a_click_one_ad(self, client, params):
        peer = params["peer"]
        random_id_hex = params["random_id_hex"]
        click_media = bool(params.get("click_media", True))
        from telethon.tl.functions.messages import ClickSponsoredMessageRequest
        try:
            rid = bytes.fromhex(random_id_hex)
        except ValueError:
            return {"ok": False, "error": f"invalid random_id_hex: {random_id_hex}"}
        result = {"ok": False, "random_id_hex": random_id_hex, "click_response": None, "error": None}
        try:
            resp = await client(ClickSponsoredMessageRequest(random_id=rid))
            result["ok"] = True
            result["click_response"] = repr(resp)
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            return result
        if click_media:
            try:
                resp_m = await client(ClickSponsoredMessageRequest(random_id=rid, media=True))
                result["media_click_response"] = repr(resp_m)
            except Exception as e:
                result["media_click_error"] = str(e)
        return result

    async def _a_visit_chat(self, client, params):
        peer = params["peer"]
        limit = int(params.get("limit", 20))
        ent = await client.get_entity(peer)
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
            })
        return {"ok": True, "peer": peer, "messages_fetched": len(msgs), "messages": msgs}

    async def _a_read_history(self, client, params):
        peer = params["peer"]
        ent = await client.get_entity(peer)
        await client.send_read_acknowledge(ent)
        return {"ok": True, "peer": peer}

    async def _a_get_me(self, client, params):
        me = await client.get_me()
        return {"id": me.id, "first_name": me.first_name, "username": me.username, "phone": me.phone}

    async def _a_list_dialogs(self, client, params):
        limit = int(params.get("limit", 100))
        filter_type = params.get("filter_type")
        from telethon.tl.types import Channel, Chat, User
        out = []
        async for d in client.iter_dialogs(limit=limit):
            ent = d.entity
            etype = "user"
            if isinstance(ent, Channel):
                etype = "group" if getattr(ent, "megagroup", False) else "channel"
            elif isinstance(ent, Chat):
                etype = "group"
            if filter_type and etype != filter_type:
                continue
            out.append({
                "id": d.id,
                "name": d.name,
                "type": etype,
                "username": getattr(ent, "username", None),
            })
        return {"dialogs": out, "count": len(out)}

    async def _a_join_channel(self, client, params):
        from telethon.tl.functions.channels import JoinChannelRequest
        peer = params["peer"]
        ent = await client.get_entity(peer)
        await client(JoinChannelRequest(ent))
        return {"ok": True, "joined": peer}

    async def _a_leave_channel(self, client, params):
        from telethon.tl.functions.channels import LeaveChannelRequest
        peer = params["peer"]
        ent = await client.get_entity(peer)
        await client(LeaveChannelRequest(ent))
        return {"ok": True, "left": peer}

    async def _a_wait(self, client, params):
        seconds = float(params.get("seconds", 30))
        await asyncio.sleep(seconds)
        return {"waited": seconds}

    async def _a_disconnect(self, client, params):
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        return {"disconnected": True}

    async def _a_log(self, client, params):
        msg = params.get("message", "")
        log.info("[workflow log] %s", msg)
        return {"logged": msg}

    async def _a_run_code(self, client, params):
        """Run arbitrary Python code. params: {code: "...", ...}

        Available vars: client, params, asyncio, json, time.
        Set `result` variable to return it.
        """
        code = params.get("code", "")
        if not code:
            return {"ok": False, "error": "no code provided"}
        ns = {
            "client": client, "params": params,
            "asyncio": asyncio, "json": json, "time": time,
            "result": None,
        }
        try:
            exec(code, ns)
            return {"ok": True, "result": ns.get("result")}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
