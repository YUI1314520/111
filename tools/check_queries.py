"""在服务器上验证 config 里的持久化查询 hash 是否仍然有效（无需登录）。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/twitch-drops")

import aiohttp

from app import config

CID = config.CLIENT_ID
UA = config.USER_AGENT
Q = config.QUERIES


async def main():
    checks = [
        ("GameDirectory", {"limit": 3, "slug": "apex", "imageWidth": 144,
                           "includeCfreeVideos": False, "sort": "VIEWER_COUNT",
                           "options": {"sort": "VIEWER_COUNT",
                                       "systemFilters": ["DROPS_ENABLED"]}}),
        ("SlugRedirect", {"name": "apex"}),
    ]
    headers = {
        "Client-Id": CID,
        "Client-Session-Id": "0123456789abcdef",
        "X-Device-Id": "0123456789abcdef0123456789abcdef",
        "User-Agent": UA,
        "Origin": config.CLIENT_URL,
        "Referer": config.CLIENT_URL + "/",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as s:
        for name, variables in checks:
            entry = Q.get(name)
            if not entry:
                print(f"{name}: 配置里没有这个查询")
                continue
            body = {
                "operationName": entry.get("operationName") or name,
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1,
                                                  "sha256Hash": entry["sha256Hash"]}},
            }
            async with s.post(config.GQL_URL, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                print(f"{name}: HTTP {r.status} {text[:220]}")
        # 登录态才会返回的查询：看是不是 401/PersistedQueryNotFound
        for name in ("Inventory", "Campaigns"):
            entry = Q[name]
            body = {"operationName": entry.get("operationName") or name,
                    "variables": {"dropID": ""},
                    "extensions": {"persistedQuery": {"version": 1,
                                                      "sha256Hash": entry["sha256Hash"]}}}
            async with s.post(config.GQL_URL, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                print(f"{name}(无 token): HTTP {r.status} {text[:200]}")


asyncio.run(main())
