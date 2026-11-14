"""FastAPI 应用：REST API + 静态面板。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import APP_DIR, DATA_DIR, VERSION
from .manager import manager
from .twitch import (
    CLIENT_ID, request_device_code, poll_device_token, new_device_id,
)

STATIC_DIR = APP_DIR / "static"
SESSION_COOKIE = "drops_session"
SESSION_TTL = 30 * 24 * 3600

# 待完成的设备码登录会话：user_code -> 状态
device_flows: dict[str, dict] = {}


# ---------------------------------------------------------------- 口令
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()


def set_password(password: str) -> None:
    salt = secrets.token_hex(16)
    db.set_setting("panel_salt", salt)
    db.set_setting("panel_hash", _hash(password, salt))


def check_password(password: str) -> bool:
    salt = db.get_setting("panel_salt")
    expected = db.get_setting("panel_hash")
    if not salt or not expected:
        return False
    return hmac.compare_digest(_hash(password, salt), expected)


def _secret() -> str:
    value = db.get_setting("panel_secret")
    if not value:
        value = secrets.token_hex(32)
        db.set_setting("panel_secret", value)
    return value


def make_session() -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = f"{exp}"
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    want = hmac.new(_secret().encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


def require_session(request: Request) -> None:
    if not valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="未登录")


# ---------------------------------------------------------------- 应用
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if not db.get_setting("panel_hash"):
        password = os.environ.get("DROPS_PASSWORD") or secrets.token_urlsafe(9)
        set_password(password)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "INITIAL_PASSWORD.txt").write_text(password + "\n", "utf-8")
        print(f"[init] 面板初始密码: {password}", flush=True)
        db.add_log("已生成面板初始密码（见 /var/lib/twitch-drops/INITIAL_PASSWORD.txt）")
    await manager.start_all()
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Twitch Drops 自动化面板", version=VERSION, lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    """前端统一读 payload.error，所以错误响应也用 error 字段。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail or "请求失败"},
    )


@app.exception_handler(Exception)
async def any_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
    )


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- 模型
class LoginBody(BaseModel):
    password: str


class TokenBody(BaseModel):
    token: str
    name: str = ""
    mode: str = "farm"
    games: list[str] = []
    proxy: str = ""


class AccountPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    mode: str | None = None
    games: list[str] | None = None
    proxy: str | None = None


class PasswordBody(BaseModel):
    current: str
    new: str


# ---------------------------------------------------------------- 认证
@app.post("/api/login")
async def api_login(body: LoginBody, response: Response):
    if not check_password(body.password):
        db.add_log("面板登录失败（密码错误）", level="warn")
        raise HTTPException(status_code=401, detail="密码错误")
    response.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/session")
async def api_session(request: Request):
    return {"authenticated": valid_session(request.cookies.get(SESSION_COOKIE))}


@app.post("/api/password", dependencies=[Depends(require_session)])
async def api_password(body: PasswordBody):
    if not check_password(body.current):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if len(body.new) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    set_password(body.new)
    db.add_log("面板密码已修改")
    return {"ok": True}


# ---------------------------------------------------------------- 状态
@app.get("/api/state", dependencies=[Depends(require_session)])
async def api_state():
    data = manager.panel_state()
    data["ok"] = True
    data["claims_recent"] = db.recent_claims(30)
    return data


@app.get("/api/logs", dependencies=[Depends(require_session)])
async def api_logs(limit: int = 200, account_id: int | None = None):
    return {"logs": db.list_logs(min(limit, 500), account_id)}


@app.get("/api/claims", dependencies=[Depends(require_session)])
async def api_claims(limit: int = 100):
    return {"claims": db.recent_claims(min(limit, 300))}


@app.get("/api/accounts/{account_id}/campaigns", dependencies=[Depends(require_session)])
async def api_campaigns(account_id: int):
    return {"campaigns": manager.campaigns(account_id)}


# ---------------------------------------------------------------- 设备码登录
def _flow_expired(flow: dict) -> bool:
    return time.time() - flow["created"] > flow["expires_in"]


@app.post("/api/accounts/device", dependencies=[Depends(require_session)])
async def api_device_start():
    device_id = new_device_id()
    async with aiohttp.ClientSession() as session:
        data = await request_device_code(session, device_id)
    device_code = data.get("device_code") or ""
    user_code = data.get("user_code") or ""
    if not device_code or not user_code:
        raise HTTPException(status_code=502, detail="Twitch 没有返回设备码，请稍后重试")
    uri = data.get("verification_uri_complete") or data.get("verification_uri") \
        or "https://www.twitch.tv/activate"
    if "device-code=" not in uri and user_code:
        uri = f"{uri}?device-code={user_code}"
    expires_in = int(data.get("expires_in") or 1800)
    interval = int(data.get("interval") or 5)
    device_flows[device_code] = {
        "device_code": device_code,
        "user_code": user_code,
        "device_id": device_id,
        "created": time.time(),
        "expires_in": expires_in,
        "interval": interval,
        "status": "pending",
        "error": "",
        "account_id": None,
    }
    # 顺手清理过期会话，防止字典无限增长
    for code in [c for c, f in device_flows.items() if _flow_expired(f)]:
        device_flows.pop(code, None)
    return {
        "ok": True,
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": uri,
        "expires_in": expires_in,
        "interval": interval,
    }


@app.get("/api/accounts/device/{device_code}", dependencies=[Depends(require_session)])
async def api_device_poll(device_code: str):
    flow = device_flows.get(device_code)
    if not flow:
        return {"status": "expired", "error": "登录会话已失效，请重新生成设备码"}
    if flow["status"] == "done":
        return {"status": "done", "account_id": flow["account_id"]}
    if flow["status"] == "error":
        return {"status": "error", "error": flow["error"]}
    if _flow_expired(flow):
        flow["status"] = "expired"
        device_flows.pop(device_code, None)
        return {"status": "expired", "error": "验证码已过期，请重新生成"}
    try:
        async with aiohttp.ClientSession() as session:
            token = await poll_device_token(session, flow["device_id"], device_code)
    except Exception as e:
        # 网络抖动不算失败，让前端继续轮询
        return {"status": "pending", "error": ""}
    if not token:
        return {"status": "pending"}
    try:
        acc = await manager.verify_and_import(
            name="", token=token["access_token"], mode="farm",
            games=[g for g in (db.get_setting("default_games") or []) if g],
            device_id=flow["device_id"],
        )
        db.update_account(acc["id"], refresh_token=token.get("refresh_token") or "",
                          token_kind="device")
    except Exception as e:
        flow["status"] = "error"
        flow["error"] = str(e)
        return {"status": "error", "error": str(e)}
    flow["status"] = "done"
    flow["account_id"] = acc["id"]
    return {"status": "done", "account_id": acc["id"]}


@app.post("/api/accounts/device/{device_code}/cancel", dependencies=[Depends(require_session)])
async def api_device_cancel(device_code: str):
    device_flows.pop(device_code, None)
    return {"ok": True}


@app.delete("/api/accounts/device/{device_code}", dependencies=[Depends(require_session)])
async def api_device_cancel_legacy(device_code: str):
    device_flows.pop(device_code, None)
    return {"ok": True}


@app.post("/api/accounts/token", dependencies=[Depends(require_session)])
async def api_account_token(body: TokenBody):
    token = body.token.strip()
    if token.lower().startswith("oauth:"):
        token = token[6:]
    if len(token) < 20:
        raise HTTPException(status_code=400, detail="token 格式不正确")
    try:
        acc = await manager.verify_and_import(
            name=body.name, token=token, mode=body.mode,
            games=body.games, proxy=body.proxy,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"校验失败：{e}")
    return {"ok": True, "account_id": acc["id"]}


# ---------------------------------------------------------------- 账号操作
@app.patch("/api/accounts/{account_id}", dependencies=[Depends(require_session)])
async def api_account_patch(account_id: int, body: AccountPatch):
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    fields = body.model_dump(exclude_none=True)
    if "mode" in fields and fields["mode"] not in ("farm", "monitor"):
        raise HTTPException(status_code=400, detail="mode 只能是 farm 或 monitor")
    if fields:
        db.update_account(account_id, **fields)
    # 代理/游戏/模式变更需要重启矿机生效
    if any(k in fields for k in ("proxy", "games", "mode")):
        await manager.restart_account(account_id)
    if "enabled" in fields:
        if fields["enabled"]:
            await manager.start_account(account_id)
        else:
            await manager.stop_account(account_id)
    return {"ok": True, "account": db.get_account(account_id)}


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_session)])
async def api_account_delete(account_id: int):
    await manager.stop_account(account_id)
    db.delete_account(account_id)
    db.add_log(f"账号 {account_id} 已删除")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/refresh", dependencies=[Depends(require_session)])
async def api_account_refresh(account_id: int):
    try:
        await manager.refresh_now(account_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "runtime": manager.runtime(account_id)}


@app.post("/api/accounts/{account_id}/claim", dependencies=[Depends(require_session)])
async def api_account_claim(account_id: int):
    try:
        count = await manager.claim_now(account_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "claimed": count}


# ---------------------------------------------------------------- 其它
@app.get("/api/games/search", dependencies=[Depends(require_session)])
async def api_games(q: str = ""):
    """游戏名搜索：先查本地缓存表，查不到再问 Twitch。"""
    q = q.strip()
    if not q:
        return {"games": []}
    catalog = db.get_setting("game_catalog") or {}
    if not isinstance(catalog, dict):
        catalog = {g.get("slug"): g.get("name") for g in catalog if isinstance(g, dict)}
    low = q.lower()
    hits = [{"slug": s, "name": n} for s, n in catalog.items()
            if s and n and low in str(n).lower()][:10]
    try:
        account = next((a for a in db.list_accounts() if a["access_token"]), None)
        if not account:
            return {"games": hits}
        from .twitch import TwitchClient
        client = TwitchClient(account["access_token"], account.get("device_id"))
        try:
            remote = await client.search_game(q)
        finally:
            await client.close()
    except Exception:
        return {"games": hits}
    merged = {g["slug"]: g["name"] for g in hits}
    for g in remote or []:
        if g.get("slug") and g.get("name"):
            merged[g["slug"]] = g["name"]
    if remote:
        catalog.update(merged)
        db.set_setting("game_catalog", catalog)
    return {"games": [{"slug": s, "name": n} for s, n in merged.items()][:20]}


@app.get("/api/health")
async def api_health():
    return {"ok": True, "version": VERSION, "running": len(manager.miners)}


@app.get("/api/settings", dependencies=[Depends(require_session)])
async def api_settings():
    return {
        "default_games": db.get_setting("default_games", []),
        "version": VERSION,
        "accounts": len(db.list_accounts()),
    }


@app.post("/api/settings", dependencies=[Depends(require_session)])
async def api_settings_save(request: Request):
    body = await request.json()
    for key in ("default_games", "game_catalog"):
        if key in body:
            db.set_setting(key, body[key])
    return {"ok": True}


# ---------------------------------------------------------------- 静态面板
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

# index.html 用相对路径引用 style.css / app.js，所以这两个文件也要挂在根路径
STATIC_ROOT_FILES = {"style.css", "app.js"}


@app.get("/")
@app.get("/index.html")
async def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({"ok": True, "message": "面板前端未安装", "version": VERSION})


@app.get("/{filename}")
async def static_root_file(filename: str):
    if filename in STATIC_ROOT_FILES and (STATIC_DIR / filename).is_file():
        return FileResponse(STATIC_DIR / filename)
    raise HTTPException(status_code=404, detail="Not Found")
