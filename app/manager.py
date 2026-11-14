"""矿机管理器：账号任务的生命周期 + 面板需要的聚合状态。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .miner import Miner, campaign_eligible, campaign_progress
from .twitch import TwitchClient, validate_token


def _iso_in(seconds: int | None) -> str | None:
    if not seconds:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


VERSION = "1.0.0"

# 内部状态 → 面板徽章只有 5 种：idle / watching / scanning / error / disabled
_UI_STATE = {
    "watching": "watching",
    "claimed": "watching",
    "monitoring": "scanning",
    "scanning": "scanning",
    "starting": "scanning",
    "idle": "idle",
    "error": "error",
    "auth_required": "error",
    "stopped": "disabled",
    "disabled": "disabled",
}


def _ui_state(acc: dict, rt: dict) -> str:
    if not acc["enabled"]:
        return "disabled"
    return _UI_STATE.get(rt.get("status") or "", "idle")


class Manager:
    def __init__(self) -> None:
        self.miners: dict[int, Miner] = {}
        self._lock = asyncio.Lock()
        self.started_at = time.time()

    # ---------------------------------------------------------- 生命周期
    async def start_all(self) -> None:
        for acc in db.list_accounts():
            if acc["enabled"]:
                await self.start_account(acc["id"], quiet=True)

    async def start_account(self, account_id: int, quiet: bool = False) -> None:
        async with self._lock:
            acc = db.get_account(account_id)
            if not acc:
                return
            miner = self.miners.get(account_id)
            if miner and miner._task and not miner._task.done():
                return
            miner = Miner(acc, self._on_token, self._on_log)
            self.miners[account_id] = miner
            miner.start()
            if not quiet:
                db.add_log("账号已启动", account_id)
            await asyncio.sleep(0)

    async def stop_account(self, account_id: int) -> None:
        async with self._lock:
            miner = self.miners.pop(account_id, None)
            if miner:
                await miner.stop()
            db.add_log("账号已停止", account_id)

    async def restart_account(self, account_id: int) -> None:
        await self.stop_account(account_id)
        await self.start_account(account_id, quiet=True)

    async def shutdown(self) -> None:
        for account_id in list(self.miners):
            miner = self.miners.pop(account_id, None)
            if miner:
                await miner.stop()

    # ---------------------------------------------------------- 回调
    def _on_token(self, account_id: int, access_token: str, refresh_token: str,
                  expires_in: int | None) -> None:
        db.update_account(
            account_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=_iso_in(expires_in),
        )

    def _on_log(self, account_id: int | None, message: str, level: str = "info") -> None:
        db.add_log(message, account_id, level)

    # ---------------------------------------------------------- 状态
    def runtime(self, account_id: int) -> dict[str, Any]:
        miner = self.miners.get(account_id)
        if not miner:
            return {"status": "stopped", "message": "未运行", "game": "", "channel": "",
                    "campaign": "", "drop": "", "watched": 0, "required": 0,
                    "updated_at": 0}
        return dict(miner.state)

    def campaigns(self, account_id: int) -> list[dict]:
        miner = self.miners.get(account_id)
        if not miner:
            return []
        out = []
        for c in miner.campaigns.values():
            if c["status"] not in ("ACTIVE", "UPCOMING"):
                continue
            done, need = campaign_progress(c)
            out.append({
                "id": c["id"], "name": c["name"], "status": c["status"],
                "game": c["game_name"], "ends_at": c["ends_at"],
                "link_url": c["link_url"], "is_connected": c["is_connected"],
                "progress": done, "required": need,
                "drops": [{
                    "name": d["name"], "required": d["required_minutes"],
                    "current": d["current_minutes"], "claimed": d["is_claimed"],
                    "rewards": d["rewards"],
                } for d in c["drops"]],
            })
        out.sort(key=lambda c: (c["ends_at"] or "9", c["game"]))
        return out

    # ---------------------------------------------------------- 面板状态
    def panel_state(self) -> dict[str, Any]:
        """跨账号聚合出前端 /api/state 需要的完整结构。"""
        accounts = db.list_accounts()
        acc_items: list[dict] = []
        camps: dict[str, dict] = {}
        games: dict[str, str] = {}
        mining = 0

        for acc in accounts:
            aid = acc["id"]
            rt = self.runtime(aid)
            stats = db.account_stats(aid)
            stats["minutes_24h"] = db.watch_minutes_24h(aid)
            state = _ui_state(acc, rt)
            if state in ("watching", "scanning"):
                mining += 1
            acc_items.append({
                "id": aid,
                "name": acc["name"],
                "login": acc["login"],
                "display_name": acc["display_name"],
                "enabled": bool(acc["enabled"]),
                "mode": acc["mode"] or "farm",
                "games": acc["games"] or [],
                "last_error": acc["last_error"] or "",
                "stats": stats,
                "status": {
                    "state": state,
                    "text": rt.get("message") or "",
                    "game": rt.get("game") or "",
                    "channel": rt.get("channel") or "",
                    "campaign": rt.get("campaign") or "",
                    "drop": rt.get("drop") or "",
                    "current_minutes": int(rt.get("watched") or 0),
                    "required_minutes": int(rt.get("required") or 0),
                },
            })

            miner = self.miners.get(aid)
            if not miner:
                continue
            for c in miner.campaigns.values():
                if c["status"] not in ("ACTIVE", "UPCOMING"):
                    continue
                item = camps.get(c["id"])
                if item is None:
                    item = camps[c["id"]] = {
                        "id": c["id"],
                        "name": c["name"],
                        "status": "UPCOMING" if c["status"] == "UPCOMING" else "ACTIVE",
                        "starts_at": c["starts_at"],
                        "ends_at": c["ends_at"],
                        "link_url": c["link_url"] or "",
                        "linked": bool(c["is_connected"]),
                        "game": {
                            "slug": c["game_slug"],
                            "name": c["game_name"],
                            "box_art": c["game_box_art"],
                        },
                        "total_drops": len(c["drops"]),
                        "claimed_drops": 0,
                        "accounts": [],
                        "drops": [],
                    }
                    if c["game_slug"]:
                        games.setdefault(c["game_slug"], c["game_name"] or c["game_slug"])
                if aid not in item["accounts"]:
                    item["accounts"].append(aid)
                if c["is_connected"]:
                    item["linked"] = True
                if not item["link_url"] and c["link_url"]:
                    item["link_url"] = c["link_url"]

                by_name = {d["name"]: d for d in item["drops"]}
                for d in c["drops"]:
                    cur = by_name.get(d["name"])
                    claimable = bool(
                        d["instance_id"]
                        and not d["is_claimed"]
                        and d["required_minutes"] > 0
                        and d["current_minutes"] >= d["required_minutes"]
                    )
                    if cur is None:
                        cur = {
                            "name": d["name"],
                            "rewards": [{"name": r} for r in d["rewards"]],
                            "current_minutes": d["current_minutes"],
                            "required_minutes": d["required_minutes"],
                            "claimed": bool(d["is_claimed"]),
                            "claimable": claimable,
                        }
                        item["drops"].append(cur)
                        by_name[d["name"]] = cur
                    else:
                        cur["current_minutes"] = max(cur["current_minutes"], d["current_minutes"])
                        cur["required_minutes"] = max(cur["required_minutes"], d["required_minutes"])
                        cur["claimed"] = bool(cur["claimed"] or d["is_claimed"])
                        cur["claimable"] = bool(cur["claimable"] or claimable)

        for item in camps.values():
            item["claimed_drops"] = sum(1 for d in item["drops"] if d["claimed"])
            item["total_drops"] = max(item["total_drops"], len(item["drops"]))

        default_games = [g for g in (db.get_setting("default_games") or []) if g]
        catalog = db.get_setting("game_catalog") or {}
        for slug in default_games:
            games.setdefault(slug, catalog.get(slug) or slug)

        from .config import INVENTORY_INTERVAL

        return {
            "accounts": acc_items,
            "campaigns": sorted(
                camps.values(),
                key=lambda c: (c["status"] == "UPCOMING", c["ends_at"] or "9999"),
            ),
            "games": [{"slug": s, "name": n} for s, n in sorted(games.items())],
            "settings": {
                "default_games": default_games,
                "scan_interval_min": max(1, int(INVENTORY_INTERVAL // 60)),
            },
            "server": {
                "version": VERSION,
                "uptime_s": int(time.time() - self.started_at),
                "accounts": len(accounts),
                "mining": mining,
            },
        }

    def summary(self) -> dict[str, Any]:
        state = self.panel_state()
        return {
            "accounts": state["accounts"],
            "running": state["server"]["mining"],
            "total": state["server"]["accounts"],
            "server_time": time.time(),
        }

    # ---------------------------------------------------------- 账号操作
    async def verify_and_import(self, name: str, token: str, mode: str = "farm",
                                games: list[str] | None = None,
                                proxy: str = "", device_id: str | None = None) -> dict:
        """校验 token → 写入账号 → 启动矿机。"""
        from .twitch import new_device_id
        device_id = device_id or new_device_id()
        client = TwitchClient(token, device_id=device_id, proxy=proxy)
        try:
            info = await validate_token(await client.session(), token, device_id)
            who = await client.whoami()
        finally:
            await client.close()
        exists = [a for a in db.list_accounts() if a["twitch_user_id"] == who["user_id"]]
        if exists:
            raise ValueError(f"账号 {who.get('login')} 已经添加过了")
        acc = db.create_account(
            name=name or who.get("display_name") or who.get("login") or "Twitch 账号",
            login=who.get("login"), display_name=who.get("display_name"),
            twitch_user_id=who["user_id"], access_token=token, refresh_token="",
            token_expires_at=_iso_in(info.get("expires_in")), token_kind="token",
            device_id=device_id, enabled=1, mode=mode,
            games=games or [], proxy=proxy,
        )
        await self.start_account(acc["id"], quiet=True)
        db.add_log(f"账号 {acc['name']} 添加成功", acc["id"])
        return acc

    async def claim_now(self, account_id: int) -> int:
        miner = self.miners.get(account_id)
        if not miner:
            raise ValueError("账号未在运行，请先启用")
        return await miner.claim_now()

    async def refresh_now(self, account_id: int) -> None:
        miner = self.miners.get(account_id)
        if not miner:
            raise ValueError("账号未在运行，请先启用")
        await miner.refresh_now()


manager = Manager()
