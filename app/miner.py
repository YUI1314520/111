"""每个账号一台「矿机」：挑活动 → 挑频道 → 观看 → 领取。

设计要点
- 不用浏览器、不播视频；靠 m3u8 分片 HEAD 请求累计观看时长（与官方网页播放器同源）。
- 一个 asyncio 任务跑一个账号，互相隔离，单个账号出错不影响其它账号。
- 进度以服务端返回的 currentMinutesWatched 为准，从不相信本地计时。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import db
from .config import (
    IDLE_INTERVAL, INVENTORY_INTERVAL, MAX_SESSION_SECONDS, PROGRESS_INTERVAL,
    WATCH_INTERVAL,
)
from .twitch import AuthError, GQLError, TwitchClient, TwitchError, WatchSession


# ---------------------------------------------------------------- 工具
def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _minutes(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- 数据归一化
def _box_art(url: str) -> str:
    """Twitch 的 boxArtURL 带 {width}/{height} 占位符，换成固定尺寸。"""
    if not url:
        return ""
    return url.replace("{width}", "144").replace("{height}", "192")


def normalize_campaign(raw: dict, details: bool = False) -> dict | None:
    """把 Dashboard / Details 的原始结构统一成内部结构。"""
    cid = raw.get("id")
    if not cid:
        return None
    game = raw.get("game") or {}
    allow = raw.get("allow") or {}
    channels = [ch.get("login") for ch in (allow.get("channels") or []) if ch.get("login")]
    drops = []
    for d in raw.get("timeBasedDrops") or []:
        sel = d.get("self") or {}
        drops.append({
            "id": d.get("id"),
            "name": d.get("name") or "奖励",
            "required_minutes": _minutes(d.get("requiredMinutesWatched")),
            "instance_id": sel.get("dropInstanceID"),
            "is_claimed": bool(sel.get("isClaimed")),
            "current_minutes": _minutes(sel.get("currentMinutesWatched")),
            "rewards": [b.get("name") for b in (d.get("benefitEdges") or [])
                        if b.get("benefit", {}).get("name")],
            "starts_at": d.get("startAt"),
            "ends_at": d.get("endAt"),
        })
    me = raw.get("self") or {}
    return {
        "id": cid,
        "name": raw.get("name") or "掉宝活动",
        "status": raw.get("status") or "",
        "game_name": game.get("displayName") or game.get("name") or "",
        "game_slug": game.get("slug") or "",
        "game_id": game.get("id") or "",
        "game_box_art": _box_art(game.get("boxArtURL") or ""),
        "starts_at": raw.get("startAt"),
        "ends_at": raw.get("endAt"),
        "link_url": raw.get("accountLinkURL") or "",
        "is_connected": bool(me.get("isAccountConnected")),
        "acl_channels": channels,
        "drops": drops,
        "has_details": details,
    }


def needs_link(c: dict) -> bool:
    """活动要求绑定账号，但当前账号没绑 —— 这种活动做了也领不到。"""
    return bool(c.get("link_url")) and not c.get("is_connected")


def campaign_eligible(c: dict) -> bool:
    now = _now()
    ends = _parse_ts(c.get("ends_at"))
    if ends and ends < now:
        return False
    starts = _parse_ts(c.get("starts_at"))
    if starts and starts > now:
        return False
    if c.get("status") and c["status"] not in ("ACTIVE", "UPCOMING"):
        return False
    return True


def campaign_progress(c: dict) -> tuple[int, int]:
    """返回（已获得时长之和, 所需时长之和）。"""
    done = sum(min(d["current_minutes"], d["required_minutes"]) for d in c["drops"])
    need = sum(d["required_minutes"] for d in c["drops"])
    return done, need


def merge_campaigns(base: dict[str, dict], extra: list[dict]) -> None:
    """Details 数据补进 Dashboard 数据：已有字段不覆盖，只补空缺。"""
    for c in extra:
        cur = base.get(c["id"])
        if cur is None:
            base[c["id"]] = c
            continue
        for key, value in c.items():
            if key == "has_details":
                cur["has_details"] = bool(cur.get("has_details") or value)
                continue
            if key == "drops":
                by_id = {d["id"]: d for d in cur["drops"]}
                for d in value:
                    old = by_id.get(d["id"])
                    if old is None:
                        cur["drops"].append(d)
                    else:
                        for k, v in d.items():
                            if old.get(k) in (None, "", 0, False) and v not in (None, ""):
                                old[k] = v
            elif cur.get(key) in (None, "", [], False):
                cur[key] = value


# ---------------------------------------------------------------- 矿机
class Miner:
    def __init__(self, account: dict, on_token_update: Callable[..., None],
                 on_log: Callable[..., None]):
        self.account = account
        self.account_id = account["id"]
        self.on_token_update = on_token_update
        self.on_log = on_log
        self.client = TwitchClient(
            account["access_token"],
            device_id=account.get("device_id") or "",
            proxy=account.get("proxy") or "",
        )
        self.campaigns: dict[str, dict] = {}
        self._last_inventory = 0.0
        self._channel_cache: dict[str, tuple[float, dict | None]] = {}
        self._offline: dict[str, float] = {}
        self._claim_fail: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._validated = 0.0
        self.state: dict[str, Any] = {
            "status": "starting", "message": "启动中…", "game": "", "channel": "",
            "campaign": "", "drop": "", "watched": 0, "required": 0,
            "updated_at": time.time(),
        }

    # ---------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"miner-{self.account_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self.client.close()

    def _set(self, **kw: Any) -> None:
        self.state.update(kw)
        self.state["updated_at"] = time.time()

    def _log(self, message: str, level: str = "info") -> None:
        self.on_log(self.account_id, message, level)

    # ---------------------------------------------------------- 主循环
    async def _run(self) -> None:
        self._log("矿机启动")
        auth_failures = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._cycle()
                    auth_failures = 0
                except AuthError as e:
                    auth_failures += 1
                    self._log(f"登录失效：{e}（第 {auth_failures} 次）", "warn")
                    if auth_failures <= 3 and self.account.get("refresh_token"):
                        try:
                            await self._refresh()
                            self._validated = 0.0
                            await self._sleep(5)
                            continue
                        except Exception as re_err:
                            self._log(f"刷新 token 失败：{re_err}", "error")
                    self._set(status="auth_required",
                              message="登录已失效，请在面板里重新登录该账号")
                    await self._sleep(600)
                except GQLError as e:
                    self._set(status="error", message=str(e))
                    self._log(f"查询失败：{e}", "error")
                    await self._sleep(120)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # 网络波动等
                    self._set(status="error", message=f"{type(e).__name__}: {e}")
                    self._log(f"异常：{type(e).__name__}: {e}", "error")
                    await self._sleep(45)
        finally:
            await self.client.close()
            self._set(status="stopped", message="已停止")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _cycle(self) -> None:
        acc = db.get_account(self.account_id) or self.account
        self.account = acc
        if not acc.get("enabled"):
            self._set(status="disabled", message="账号已停用")
            await self._sleep(60)
            return

        await self._ensure_auth()
        await self._maybe_refresh_inventory()

        campaigns = [c for c in self.campaigns.values() if campaign_eligible(c)]
        # 已有可领奖励先领掉
        await self._claim_all()

        # 只挖选定游戏的活动；账号没选游戏时跟随面板的默认游戏列表
        games = [g for g in (acc.get("games") or []) if g]
        if not games:
            games = [g for g in (db.get_setting("default_games") or []) if g]
        if games:
            allowed = set(games)
            campaigns = [c for c in campaigns if c["game_slug"] in allowed or not c["game_slug"]]

        if acc.get("mode") == "monitor":
            active = [c for c in campaigns if c["status"] == "ACTIVE"]
            self._set(status="monitoring", message=f"监控中：{len(active)} 个进行中活动",
                      game="", channel="", campaign="", drop="", watched=0, required=0)
            await self._sleep(120)
            return

        target, channel = await self._pick_target(campaigns)
        if not target or not channel:
            self._set(status="idle", message="没有可进行的掉宝任务，等待新活动",
                      game="", channel="", campaign="", drop="", watched=0, required=0)
            await self._sleep(IDLE_INTERVAL)
            return

        await self._watch_session(target, channel)

    # ---------------------------------------------------------- 认证
    async def _ensure_auth(self) -> None:
        if time.monotonic() - self._validated < 1800:
            return
        info = await self.client.validate()
        self._validated = time.monotonic()
        if not self.client.user_id:
            who = await self.client.whoami()
            self.client.user_id = who["user_id"]
            db.update_account(self.account_id, login=who.get("login"),
                              display_name=who.get("display_name"),
                              twitch_user_id=who["user_id"])
        expires_in = info.get("expires_in") or 0
        if expires_in and expires_in < 3600 and self.account.get("refresh_token"):
            await self._refresh()

    async def _refresh(self) -> None:
        refresh_token = self.account.get("refresh_token")
        if not refresh_token:
            raise AuthError("无可用的 refresh_token，请重新登录")
        s = await self.client.session()
        from .twitch import refresh_access_token
        data = await refresh_access_token(s, refresh_token, self.client.device_id)
        self.client.token = data["access_token"]
        self.on_token_update(
            self.account_id,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or refresh_token,
            expires_in=data.get("expires_in"),
        )
        self.account["access_token"] = data["access_token"]
        self.account["refresh_token"] = data.get("refresh_token") or refresh_token
        self._log("token 已自动刷新")

    # ---------------------------------------------------------- 活动数据
    async def _maybe_refresh_inventory(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_inventory < INVENTORY_INTERVAL:
            return
        inv = await self.client.inventory()
        merged: dict[str, dict] = {}
        for raw in inv.get("dropCampaignsInProgress") or []:
            c = normalize_campaign(raw)
            if c:
                merged[c["id"]] = c
        try:
            dashboard = await self.client.campaigns()
        except TwitchError:
            dashboard = []
        for raw in dashboard:
            c = normalize_campaign(raw)
            if c and c["id"] not in merged:
                merged[c["id"]] = c
            elif c:
                merge_campaigns(merged, [c])
        # 补全详情：self.isAccountConnected / 每个 drop 的准确进度都在 details 里
        need = [cid for cid, c in merged.items()
                if not c["has_details"] and campaign_eligible(c)]
        if need:
            details = await self.client.campaign_details(need)
            merge_campaigns(merged, [c for c in (normalize_campaign(d, details=True)
                                                 for d in details) if c])
        self.campaigns = merged
        self._last_inventory = time.monotonic()

    # ---------------------------------------------------------- 选目标
    async def _pick_target(self, campaigns: list[dict]) -> tuple[dict | None, dict | None]:
        cands = []
        for c in campaigns:
            if c["status"] != "ACTIVE":
                continue
            if not c["is_connected"] and needs_link(c):
                continue
            pending = [d for d in c["drops"] if not d["is_claimed"]]
            if not pending:
                continue
            ends = _parse_ts(c["ends_at"]) or _now()
            cands.append((ends, -campaign_progress(c)[0], c))
        cands.sort(key=lambda t: (t[0], t[1]))
        for _, _, c in cands:
            channel = await self._channel_for(c)
            if channel:
                return c, channel
        return None, None

    async def _channel_for(self, campaign: dict) -> dict | None:
        slug = campaign["game_slug"]
        acl = tuple(sorted(campaign["acl_channels"]))
        key = f"{slug}|{','.join(acl)}"
        cached = self._channel_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]

        candidates: list[dict] = []
        if acl:
            infos = await self.client.streams_info(list(acl))
            for info in infos:
                if not info["live"]:
                    continue
                if info["game"] != slug:
                    continue
                candidates.append(info)
        else:
            try:
                streams = await self.client.game_streams(slug, limit=25)
            except TwitchError:
                streams = []
            for s in streams:
                if s["login"] in self._offline and time.time() - self._offline[s["login"]] < 300:
                    continue
                candidates.append({**s, "game": slug, "live": True})

        candidates.sort(key=lambda c: c.get("viewers") or 0, reverse=True)
        pick = None
        for cand in candidates[:5]:
            if cand.get("id") and await self.client.drops_enabled(cand["id"]):
                pick = cand
                break
        if pick is None and candidates:
            pick = candidates[0]  # 兜底：drops 标记查不到时也用最热门的
        self._channel_cache[key] = (time.monotonic(), pick)
        return pick

    # ---------------------------------------------------------- 观看
    async def _watch_session(self, campaign: dict, channel: dict) -> None:
        login = channel.get("login")
        if not login:
            return
        ws = WatchSession(self.client, login)
        try:
            opened = await ws.open()
        except TwitchError as e:
            self._offline[login] = time.time()
            self._channel_cache.clear()
            self._log(f"{login} 打开失败：{e}", "warn")
            await self._sleep(20)
            return
        if not opened:
            self._offline[login] = time.time()
            self._channel_cache.clear()
            self._log(f"{login} 离线或无法播放，换频道", "warn")
            await self._sleep(15)
            return

        drop = self._current_drop(campaign)
        self._log(f"开始观看 {login}（{campaign['game_name']} / {campaign['name']}）")
        started = time.monotonic()
        last_check = 0.0
        prev_beat = time.monotonic()
        while not self._stop.is_set() and time.monotonic() - started < MAX_SESSION_SECONDS:
            beat_start = time.monotonic()
            ok = await ws.beat()
            if ok == 0:
                self._offline[login] = time.time()
                self._channel_cache.clear()
                self._log(f"{login} 无响应，重新选频道", "warn")
                return
            # 累计观看时长（用于面板「今日已看」），单轮最多记 WATCH_INTERVAL*2 秒
            now = time.monotonic()
            elapsed = int(min(now - prev_beat, WATCH_INTERVAL * 2))
            prev_beat = now
            if elapsed > 0:
                db.add_watch_seconds(self.account_id, elapsed)
            if time.monotonic() - last_check >= PROGRESS_INTERVAL:
                last_check = time.monotonic()
                alive = await self._check_progress(campaign, channel, drop)
                if not alive:
                    return
                # 活动做完就退出，去选下一个
                fresh = self.campaigns.get(campaign["id"])
                if fresh and all(d["is_claimed"] for d in fresh["drops"]):
                    self._log(f"活动《{campaign['name']}》全部完成")
                    return
            wait = WATCH_INTERVAL - (time.monotonic() - beat_start)
            await self._sleep(max(1.0, wait))

    def _current_drop(self, campaign: dict) -> dict | None:
        for d in campaign["drops"]:
            if not d["is_claimed"] and d["current_minutes"] < d["required_minutes"]:
                return d
        return None

    async def _check_progress(self, campaign: dict, channel: dict,
                              drop: dict | None) -> bool:
        """查服务端进度、更新状态、必要时领取。返回 False 表示该结束会话。"""
        try:
            ctx = await self.client.current_drop(str(channel.get("id") or ""))
        except TwitchError:
            ctx = None
        watched = 0
        if ctx:
            watched = _minutes(ctx.get("currentMinutesWatched"))
            if drop is None and ctx.get("dropID"):
                for d in campaign["drops"]:
                    if d["id"] == ctx.get("dropID"):
                        drop = d
                        break
            if drop:
                drop["current_minutes"] = max(drop["current_minutes"], watched)

        if drop:
            self._set(
                status="watching", game=campaign["game_name"], channel=channel.get("login", ""),
                campaign=campaign["name"], drop=drop["name"],
                watched=drop["current_minutes"], required=drop["required_minutes"],
                message=f"观看中 {drop['current_minutes']}/{drop['required_minutes']} 分钟",
            )
        else:
            self._set(status="watching", game=campaign["game_name"],
                      channel=channel.get("login", ""), campaign=campaign["name"],
                      drop="", watched=0, required=0, message="观看中")

        if drop and drop["current_minutes"] >= drop["required_minutes"]:
            await self._claim_all()
            return False
        return True

    # ---------------------------------------------------------- 领取
    async def _claim_all(self) -> int:
        """领取所有已达标的奖励。返回新增领取数。"""
        claimed = 0
        for cid, camp in list(self.campaigns.items()):
            if needs_link(camp):
                continue
            for d in camp["drops"]:
                if d["is_claimed"]:
                    continue
                ready = d["required_minutes"] > 0 and d["current_minutes"] >= d["required_minutes"]
                if not ready and not d["instance_id"]:
                    continue
                if self._claim_fail.get(d["id"], 0) + 1800 > time.time():
                    continue  # 半小时内失败过，先别烦 Twitch
                instance = d["instance_id"]
                if not instance:
                    if not self.client.user_id:
                        continue
                    instance = f"{self.client.user_id}#{cid}#{d['id']}"
                try:
                    await self.client.claim(instance)
                except TwitchError as e:
                    self._claim_fail[d["id"]] = time.time()
                    self._log(f"领取《{d['name']}》失败：{e}", "warn")
                    continue
                d["is_claimed"] = True
                claimed += 1
                db.add_claim(self.account_id, d["id"], cid, camp["name"],
                             camp["game_name"], d["name"], d["rewards"])
                self._log(f"已领取《{camp['game_name']}》{d['name']}")
                self._set(status="claimed",
                          message=f"已领取 {d['name']}", drop=d["name"])
                await asyncio.sleep(1)
        if claimed:
            # 领取后刷新一次服务端状态
            try:
                await self._maybe_refresh_inventory(force=True)
            except TwitchError:
                pass
        return claimed

    async def claim_now(self) -> int:
        await self._maybe_refresh_inventory(force=True)
        return await self._claim_all()

    async def refresh_now(self) -> None:
        await self._maybe_refresh_inventory(force=True)
