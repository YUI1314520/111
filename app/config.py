"""全局配置与 GQL 持久化查询表。

Twitch 的 GQL 使用「持久化查询」(persisted query)：客户端只发 operationName +
sha256Hash，服务端返回对应结果。哈希由 Twitch 前端 JS 决定，偶尔会变。
因此本文件把哈希集中在一处，并支持用数据目录下的 gql_queries.json 覆盖，
将来某条查询失效时无需改代码，只需改 JSON 后重启服务。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DROPS_DATA_DIR", "/var/lib/twitch-drops"))
DB_PATH = DATA_DIR / "drops.db"
GQL_OVERRIDE_PATH = DATA_DIR / "gql_queries.json"

# ---------------------------------------------------------------- 常量
VERSION = "1.0.0"

# Twitch 客户端（公开 client_id，无需 secret）
# 用 Android TV 客户端：它是少数支持「设备码登录」的公开 client_id，
# 而且同一 client_id 也能正常访问 GQL 与播放接口（web 客户端 id 拿到 device 接口会报 invalid client）。
CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
CLIENT_URL = "https://android.tv.twitch.tv"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 7.1; Smart Box C1) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

GQL_URL = "https://gql.twitch.tv/gql"
USHER_URL = "https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?sig={sig}&token={token}"

# 观看节奏：每 20 秒一次「观看」请求，与官方网页播放器一致
WATCH_INTERVAL = 20.0
# 每 60 秒查一次当前掉宝进度
PROGRESS_INTERVAL = 60.0
# 每次会话最长连续观看时长（秒），之后重新选频道，避免长期占用同一连接
MAX_SESSION_SECONDS = 3 * 3600
# 活动列表（inventory）刷新间隔（秒）
INVENTORY_INTERVAL = 10 * 60
# 无任务时的空转间隔（秒）
IDLE_INTERVAL = 120

HTTP_TIMEOUT = 25

# ---------------------------------------------------------------- 默认 GQL 查询
# 与 DevilXD/TwitchDropsMiner 保持一致（2026-11 校验）
DEFAULT_QUERIES: dict[str, dict] = {
    "Inventory": {
        "operationName": "Inventory",
        "sha256Hash": "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
        "variables": {"fetchRewardCampaigns": False},
        "doc": "当前用户进行中的掉宝活动（含每个 drop 的 self 进度）",
    },
    "Campaigns": {
        "operationName": "ViewerDropsDashboard",
        "sha256Hash": "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb",
        "variables": {"fetchRewardCampaigns": False},
        "doc": "全部可用掉宝活动列表",
    },
    "CampaignDetails": {
        "operationName": "DropCampaignDetails",
        "sha256Hash": "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1",
        # channelLogin 传 user_id 字符串，dropID 传活动 ID
        "variables": {"channelLogin": "...", "dropID": "..."},
        "doc": "单个活动详情（timeBasedDrops / allow 频道 / self 状态）",
    },
    "ClaimDrop": {
        "operationName": "DropsPage_ClaimDropRewards",
        "sha256Hash": "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
        "variables": {"input": {"dropInstanceID": "..."}},
        "doc": "领取奖励",
    },
    "CurrentDrop": {
        "operationName": "DropCurrentSessionContext",
        "sha256Hash": "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        "variables": {"channelID": "...", "channelLogin": ""},
        "doc": "当前会话正在累计的 drop 进度",
    },
    "AvailableDrops": {
        "operationName": "DropsHighlightService_AvailableDrops",
        "sha256Hash": "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
        "variables": {"channelID": "..."},
        "doc": "某频道是否开启了掉宝",
    },
    "PlaybackAccessToken": {
        "operationName": "PlaybackAccessToken",
        "sha256Hash": "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
        "variables": {
            "isLive": True, "isVod": False, "login": "...", "platform": "web",
            "playerType": "site", "vodID": "",
        },
        "doc": "直播流播放令牌（取 m3u8 用）",
    },
    "GameDirectory": {
        "operationName": "DirectoryPage_Game",
        "sha256Hash": "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd",
        "variables": {
            "limit": 30, "slug": "...", "imageWidth": 50, "includeCostreaming": False,
            "options": {
                "broadcasterLanguages": [], "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "VIEWER_COUNT", "systemFilters": ["DROPS_ENABLED"],
                "tags": [], "requestID": "JIRA-VXP-2397",
            },
            "sortTypeIsRecency": False,
        },
        "doc": "某个游戏下正在直播（且开启掉宝）的频道",
    },
    "GetStreamInfo": {
        "operationName": "VideoPlayerStreamInfoOverlayChannel",
        "sha256Hash": "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
        "variables": {"channel": "..."},
        "doc": "频道的直播状态与当前游戏",
    },
    "SlugRedirect": {
        "operationName": "DirectoryGameRedirect",
        "sha256Hash": "1f0300090caceec51f33c5e20647aceff9017f740f223c3c532ba6fa59f6b6cc",
        "variables": {"name": "..."},
        "doc": "游戏名 -> 游戏 slug",
    },
}


def load_queries() -> dict[str, dict]:
    """读取默认表，并允许数据目录的 json 局部覆盖。"""
    queries = json.loads(json.dumps(DEFAULT_QUERIES))  # deep copy
    if GQL_OVERRIDE_PATH.exists():
        try:
            override = json.loads(GQL_OVERRIDE_PATH.read_text("utf-8"))
        except Exception:  # 覆盖文件写坏了不能拖垮整个服务
            return queries
        for name, data in override.items():
            if name in queries and isinstance(data, dict):
                queries[name].update({k: v for k, v in data.items() if k != "doc"})
    return queries


QUERIES = load_queries()
