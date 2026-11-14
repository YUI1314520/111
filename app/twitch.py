"""Twitch 协议客户端：GQL 查询、设备码登录、观看心跳、领取奖励。

全部基于 HTTP，不需要浏览器 / 不需要播放视频。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

import aiohttp

from .config import (
    CLIENT_ID, CLIENT_URL, GQL_URL, HTTP_TIMEOUT, QUERIES, USER_AGENT, USHER_URL,
)


class TwitchError(Exception):
    pass


class AuthError(TwitchError):
    """token 失效，需要重新登录 / 刷新。"""


class GQLError(TwitchError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def new_device_id() -> str:
    return secrets.token_hex(16)


def _base_headers(token: str | None, device_id: str) -> dict[str, str]:
    h = {
        "Client-Id": CLIENT_ID,
        "Client-Session-Id": secrets.token_hex(8),
        "X-Device-Id": device_id,
        "User-Agent": USER_AGENT,
        "Origin": CLIENT_URL,
        "Referer": CLIENT_URL + "/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if token:
        h["Authorization"] = f"OAuth {token}"
    return h


# ================================================================ 设备码登录
async def request_device_code(session: aiohttp.ClientSession, device_id: str) -> dict:
    """第 1 步：拿 device_code / 用户码。"""
    async with session.post(
        "https://id.twitch.tv/oauth2/device",
        data={"client_id": CLIENT_ID, "scopes": ""},
        headers={"X-Device-Id": device_id, "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
    ) as r:
        text = await r.text()
        if r.status != 200:
            raise TwitchError(f"设备码请求失败 HTTP {r.status}: {text[:200]}")
        return json.loads(text)


async def poll_device_token(session: aiohttp.ClientSession, device_id: str,
                            device_code: str) -> dict | None:
    """单次轮询。返回 token dict，或 None（用户还没确认）。"""
    async with session.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"X-Device-Id": device_id, "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
    ) as r:
        text = await r.text()
        if r.status == 200:
            return json.loads(text)
        try:
            msg = json.loads(text).get("message", "")
        except Exception:
            msg = text[:200]
        if "pending" in msg.lower() or "authorization_pending" in text:
            return None
        if "slow" in msg.lower() or "too many" in msg.lower():
            return None
        raise TwitchError(f"登录失败: {msg}")


async def refresh_access_token(session: aiohttp.ClientSession, refresh_token: str,
                               device_id: str) -> dict:
    async with session.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"X-Device-Id": device_id, "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
    ) as r:
        text = await r.text()
        if r.status != 200:
            raise AuthError(f"刷新 token 失败 HTTP {r.status}: {text[:200]}")
        return json.loads(text)


async def validate_token(session: aiohttp.ClientSession, token: str,
                         device_id: str) -> dict:
    async with session.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}", "X-Device-Id": device_id,
                 "User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
    ) as r:
        text = await r.text()
        if r.status != 200:
            raise AuthError(f"token 无效 HTTP {r.status}: {text[:150]}")
        return json.loads(text)


# ================================================================ 客户端
class TwitchClient:
    """一个账号一个实例，复用 aiohttp 连接池。"""

    def __init__(self, token: str, device_id: str | None = None, proxy: str = ""):
        self.token = token
        self.device_id = device_id or new_device_id()
        self.proxy = proxy or None
        self.user_id: str | None = None
        self._session: aiohttp.ClientSession | None = None

    # ---------------------------------------------------------- 底层
    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def gql(self, ops: list[tuple[str, dict]]) -> list[dict]:
        """批量执行持久化查询，返回与 ops 等长的 data 列表。

        ops: [(查询名, variables覆盖), ...]
        """
        payload = []
        for name, variables in ops:
            q = QUERIES[name]
            merged = dict(q["variables"])
            merged.update(variables or {})
            payload.append({
                "operationName": q["operationName"],
                "variables": merged,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": q["sha256Hash"]}},
            })
        s = await self.session()
        headers = _base_headers(self.token, self.device_id)
        headers["Content-Type"] = "application/json"
        async with s.post(GQL_URL, data=json.dumps(payload), headers=headers,
                          proxy=self.proxy) as r:
            if r.status == 401:
                raise AuthError("GQL 401：token 已失效")
            if r.status == 429:
                raise TwitchError("GQL 429：被限流，稍后重试")
            if r.status != 200:
                raise TwitchError(f"GQL HTTP {r.status}: {(await r.text())[:200]}")
            body = await r.json(content_type=None)

        if not isinstance(body, list):
            body = [body]
        out: list[dict] = []
        for item in body:
            if item is None:
                out.append({})
                continue
            if "errors" in item and item["errors"]:
                err = item["errors"][0]
                msg = err.get("message", "unknown")
                if "PersistedQueryNotFound" in str(err.get("message", "")):
                    raise GQLError(f"持久化查询已失效（{msg}），需要更新 hash", "PersistedQueryNotFound")
                if "Unauthorized" in msg or "unauthenticated" in msg.lower():
                    raise AuthError(msg)
                raise GQLError(msg, str(err.get("extensions", {}).get("code", "")))
            out.append(item.get("data") or {})
        return out

    async def one(self, name: str, variables: dict | None = None) -> dict:
        res = await self.gql([(name, variables or {})])
        return res[0]

    # ---------------------------------------------------------- 账号信息
    async def validate(self) -> dict:
        s = await self.session()
        info = await validate_token(s, self.token, self.device_id)
        self.user_id = str(info.get("user_id") or self.user_id or "")
        return info

    async def whoami(self) -> dict:
        """拿 login / displayName（顺带确认 token 有效）。"""
        data = await self.one("Inventory", {})
        user = (data.get("currentUser") or {})
        if not user:
            raise AuthError("无法读取当前用户，token 可能已失效")
        self.user_id = str(user.get("id") or self.user_id or "")
        return {
            "user_id": self.user_id,
            "login": user.get("login"),
            "display_name": user.get("displayName"),
            "inventory": user.get("inventory") or {},
        }

    # ---------------------------------------------------------- 掉宝数据
    async def inventory(self) -> dict:
        data = await self.one("Inventory", {})
        user = data.get("currentUser") or {}
        return user.get("inventory") or {}

    async def campaigns(self) -> list[dict]:
        data = await self.one("Campaigns", {})
        user = data.get("currentUser") or {}
        return user.get("dropCampaigns") or []

    async def campaign_details(self, campaign_ids: list[str]) -> list[dict]:
        """按 20 个一批补全详情。channelLogin 必须是 user_id 字符串。"""
        if not campaign_ids:
            return []
        if not self.user_id:
            await self.whoami()
        uid = self.user_id or ""
        results: list[dict] = []
        for i in range(0, len(campaign_ids), 20):
            chunk = campaign_ids[i:i + 20]
            ops = [("CampaignDetails", {"channelLogin": uid, "dropID": cid}) for cid in chunk]
            try:
                datas = await self.gql(ops)
            except GQLError:
                # 单条失败不应影响其它活动
                datas = []
                for op in ops:
                    try:
                        datas.append((await self.gql([op]))[0])
                    except TwitchError:
                        datas.append({})
            for d in datas:
                camp = (d.get("dropCampaign") or d.get("currentUser", {}).get("dropCampaign"))
                if camp:
                    results.append(camp)
            await asyncio.sleep(0.2)
        return results

    async def claim(self, drop_instance_id: str) -> str:
        data = await self.one("ClaimDrop", {"input": {"dropInstanceID": drop_instance_id}})
        status = str(((data.get("claimDropRewards") or {}).get("status")) or "")
        if status in ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"):
            return status
        raise TwitchError(f"领取失败，返回状态: {status or 'unknown'}")

    async def current_drop(self, channel_id: str) -> dict | None:
        data = await self.one("CurrentDrop", {"channelID": str(channel_id), "channelLogin": ""})
        return (data.get("currentUser") or {}).get("dropCurrentSession")

    # ---------------------------------------------------------- 频道
    async def game_streams(self, slug: str, limit: int = 30) -> list[dict]:
        variables = {
            "limit": limit,
            "slug": slug,
            "imageWidth": 50,
            "includeCostreaming": False,
            "sortTypeIsRecency": False,
            "options": {
                "broadcasterLanguages": [],
                "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "VIEWER_COUNT",
                "systemFilters": ["DROPS_ENABLED"],
                "tags": [],
                "requestID": "JIRA-VXP-2397",
            },
        }
        data = await self.one("GameDirectory", variables)
        game = data.get("game") or {}
        streams = []
        for e in (game.get("streams") or {}).get("edges", []):
            node = e.get("node") or {}
            if node.get("id"):
                streams.append({
                    "id": str(node["id"]),
                    "login": node.get("broadcaster", {}).get("login"),
                    "display_name": node.get("broadcaster", {}).get("displayName"),
                    "viewers": node.get("viewersCount") or 0,
                })
        return streams

    async def stream_info(self, login: str) -> dict:
        data = await self.one("GetStreamInfo", {"channel": login})
        user = data.get("user") or {}
        stream = user.get("stream")
        game = (user.get("game") or {}).get("slug") or ""
        return {
            "id": str(user.get("id") or ""),
            "login": user.get("login") or login,
            "live": bool(stream),
            "viewers": (stream or {}).get("viewersCount") or 0,
            "game": game,
        }

    async def streams_info(self, logins: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(logins), 20):
            chunk = logins[i:i + 20]
            ops = [("GetStreamInfo", {"channel": lg}) for lg in chunk]
            try:
                datas = await self.gql(ops)
            except TwitchError:
                datas = []
            for lg, d in zip(chunk, datas):
                user = (d or {}).get("user") or {}
                if not user:
                    continue
                stream = user.get("stream") or {}
                out.append({
                    "id": str(user.get("id") or ""),
                    "login": user.get("login") or lg,
                    "live": bool(stream),
                    "viewers": stream.get("viewersCount") or 0,
                    "game": ((user.get("game") or {}).get("slug") or ""),
                })
        return out

    async def drops_enabled(self, channel_id: str) -> bool:
        try:
            data = await self.one("AvailableDrops", {"channelID": str(channel_id)})
        except TwitchError:
            return False
        return bool((data.get("channel") or {}).get("dropsEnabled"))

    async def game_slug(self, name: str) -> str:
        data = await self.one("SlugRedirect", {"name": name})
        game = data.get("game") or {}
        return game.get("slug") or ""

    async def search_game(self, name: str) -> list[dict]:
        """用 GQL 找游戏 slug；Twitch 没有公开搜索 GQL，退化为 redirect。"""
        slug = await self.game_slug(name)
        if not slug:
            return []
        return [{"slug": slug, "name": name}]

    # ---------------------------------------------------------- 观看
    async def playback_token(self, login: str) -> tuple[str, str]:
        variables = {
            "isLive": True,
            "isVod": False,
            "login": login,
            "platform": "web",
            "playerType": "site",
            "vodID": "",
        }
        data = await self.one("PlaybackAccessToken", variables)
        tok = (data.get("streamPlaybackAccessToken") or {})
        value, sig = tok.get("value"), tok.get("signature")
        if not value or not sig:
            raise TwitchError("拿不到播放令牌（频道可能离线）")
        return value, sig

    async def playlist_url(self, login: str) -> str | None:
        """解析出最低码率 m3u8 直链。返回 None 表示频道离线。"""
        value, sig = await self.playback_token(login)
        s = await self.session()
        url = USHER_URL.format(login=login, sig=sig, token=value)
        headers = {"User-Agent": USER_AGENT, "Origin": CLIENT_URL,
                   "Referer": CLIENT_URL + "/", "Client-Id": CLIENT_ID}
        async with s.get(url, headers=headers, proxy=self.proxy) as r:
            if r.status == 404:
                return None
            if r.status != 200:
                raise TwitchError(f"usher HTTP {r.status}")
            text = await r.text()
        variants = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.startswith("#") and ".m3u8" in ln]
        if not variants:
            return None
        return variants[-1]  # 最低码率


class WatchSession:
    """一次「观看」会话：持有 chunk 列表，每 20 秒对它们发 HEAD。

    Twitch 的观看时长统计只认这些 CDN 分片请求，不需要真的下载内容。
    """

    def __init__(self, client: TwitchClient, login: str):
        self.client = client
        self.login = login
        self.chunks: list[str] = []
        self.playlist_url: str | None = None
        self.opened_at = 0.0
        self.beats = 0

    async def open(self) -> bool:
        self.playlist_url = await self.client.playlist_url(self.login)
        if not self.playlist_url:
            return False
        await self._load_chunks()
        self.opened_at = time.monotonic()
        return bool(self.chunks)

    async def _load_chunks(self) -> None:
        """每次观看心跳都重新拉一遍分片列表——分片 URL 会过期，
        官方播放器也是每次都拿最新的，照做最稳。"""
        s = await self.client.session()
        headers = {"User-Agent": USER_AGENT, "Origin": CLIENT_URL,
                   "Referer": CLIENT_URL + "/", "Connection": "close"}
        async with s.get(self.playlist_url, headers=headers,
                         proxy=self.client.proxy) as r:
            if r.status >= 400:
                self.chunks = []
                return
            text = await r.text()
        if text.lstrip().startswith("{"):
            # {"error": "..."} —— 频道离线 / 令牌失效
            self.chunks = []
            return
        self.chunks = [ln.strip() for ln in text.splitlines()
                       if ln.strip().startswith("http")]

    async def beat(self) -> int:
        """发一轮观看请求：GET 分片列表 + 对每个分片 HEAD。返回成功数。"""
        await self._load_chunks()
        if not self.chunks:
            return 0
        s = await self.client.session()
        ok = 0
        headers = {"User-Agent": USER_AGENT, "Origin": CLIENT_URL,
                   "Referer": CLIENT_URL + "/"}
        for url in self.chunks[:20]:
            try:
                async with s.head(url, headers=headers, proxy=self.client.proxy,
                                  timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status < 400:
                        ok += 1
                    elif r.status == 404:
                        break  # 分片失效，本轮作废
            except Exception:
                continue
        if ok:
            self.beats += 1
        return ok
