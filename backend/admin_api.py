# -*- coding: utf-8 -*-
"""网页管理后台的 API。

挂载在 /admin/api 下,页面本体是 main.py 直接返回的 backend/admin.html。

鉴权:.env 里的 ADMIN_PASSWORD(未设置时启动会自动生成随机密码写回 .env,
并在日志里打印)。登录后发 hmac 签名的 HttpOnly cookie,12 小时有效;
所有请求还要带 X-Admin-Request 头,防跨站表单类误触。

注意:改 ADMIN_PASSWORD 后需要重启服务才生效(密码同时是 cookie 的签名密钥)。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile

from settings_store import SettingsStore, AnnouncementStore
from templates_store import covers_dir, resolve_cover, resolve_covers

# 需要打码的密钥字段: (节路径) -> 字段列表
_MASK_SCHEMA = {
    ("providers", "worldcodes"): ("api_key",),
    ("providers", "fal"): ("api_key",),
    ("providers", "baidu"): ("api_key", "secret_key"),
    ("wechat",): ("app_secret",),
    ("tencent",): ("secret_id", "secret_key"),
}

log = logging.getLogger("rescue.admin")

COOKIE_NAME = "admin_session"
SESSION_TTL = 12 * 3600
_CSRF_HEADER = "x-admin-request"


# --------------------------------------------------------------------------- #
# 密码引导:没配就生成随机的写回 .env
# --------------------------------------------------------------------------- #
def ensure_admin_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if password:
        return password

    password = secrets.token_urlsafe(9)
    os.environ["ADMIN_PASSWORD"] = password

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        replaced = False
        for idx, line in enumerate(lines):
            if line.strip().startswith("ADMIN_PASSWORD="):
                lines[idx] = "ADMIN_PASSWORD=%s\n" % password
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append("ADMIN_PASSWORD=%s\n" % password)
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        log.warning("=" * 60)
        log.warning("管理后台随机密码已生成并写入 .env(请尽快登录后修改):")
        log.warning("ADMIN_PASSWORD=%s", password)
        log.warning("=" * 60)
    except OSError as exc:
        # .env 写不进去也得能用:密码只在本次进程内有效
        os.environ["ADMIN_PASSWORD"] = password
        log.warning(".env 写入失败(%s),本次启动的临时密码: %s", exc, password)
    return password


def _password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "").strip()


# --------------------------------------------------------------------------- #
# 签名 cookie
# --------------------------------------------------------------------------- #
def _sign(payload: str, password: str) -> str:
    key = hashlib.sha256(("rescue-admin:%s" % password).encode("utf-8")).digest()
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_token(password: str) -> str:
    expiry = str(int(time.time()) + SESSION_TTL)
    return "%s.%s" % (expiry, _sign(expiry, password))


def _verify_token(token: str, password: str) -> bool:
    try:
        expiry, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(expiry, password)):
            return False
        return int(expiry) > time.time()
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# 登录限速:连续失败 5 次锁 5 分钟(按来源 IP)
# --------------------------------------------------------------------------- #
_failures: Dict[str, Tuple[int, float]] = {}
_failures_lock = threading.Lock()


def _login_allowed(ip: str) -> bool:
    with _failures_lock:
        fails, until = _failures.get(ip, (0, 0.0))
        return time.time() >= until


def _login_fail(ip: str) -> None:
    with _failures_lock:
        fails, until = _failures.get(ip, (0, 0.0))
        fails += 1
        lock_until = time.time() + 300 if fails >= 5 else until
        if fails >= 5:
            fails = 0
        _failures[ip] = (fails, lock_until)


def _login_ok(ip: str) -> None:
    with _failures_lock:
        _failures.pop(ip, None)


# --------------------------------------------------------------------------- #
# 密钥打码
# --------------------------------------------------------------------------- #
def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 4 else "••••"


def _masked_settings(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    doc = json.loads(json.dumps(snapshot))  # 深拷贝
    for path, fields in _MASK_SCHEMA.items():
        node = doc
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue
        for field in fields:
            if node.get(field):
                node[field] = mask_secret(str(node[field]))
    return doc


def _unmask_secrets(patch: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """打码值回传 = 保持原值;空字符串 = 清空;其余 = 新密钥。"""
    patch = json.loads(json.dumps(patch))
    for path, fields in _MASK_SCHEMA.items():
        patch_node = patch
        cur_node = current
        for part in path:
            patch_node = patch_node.get(part) if isinstance(patch_node, dict) else None
            cur_node = cur_node.get(part) if isinstance(cur_node, dict) else {}
        if not isinstance(patch_node, dict):
            continue
        for field in fields:
            if field not in patch_node:
                continue
            value = patch_node[field]
            if value is None or (isinstance(value, str) and value.startswith("••••")):
                patch_node[field] = (cur_node or {}).get(field, "")
    return patch


# --------------------------------------------------------------------------- #
# 路由工厂
# --------------------------------------------------------------------------- #
def make_admin_router(*, settings: SettingsStore,
                      announcements: AnnouncementStore,
                      templates,                     # main.TemplateStore
                      jobs,                      # main.JobStore
                      users,                     # main.UserStore
                      health_fn: Callable[[], Dict[str, Any]],
                      stats_fn: Callable[[], Dict[str, Any]]) -> APIRouter:
    router = APIRouter(prefix="/admin/api")

    def _guard(request: Request) -> None:
        if request.headers.get(_CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="缺少请求头")
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or not _verify_token(token, _password()):
            raise HTTPException(status_code=401, detail="未登录或会话已过期")

    @router.post("/login")
    async def login(request: Request, response: Response) -> Dict[str, Any]:
        ip = request.client.host if request.client else "?"
        if not _login_allowed(ip):
            raise HTTPException(status_code=429, detail="失败次数过多，5 分钟后再试")
        body = await _json_body(request)
        password = str(body.get("password") or "")
        if not password or not hmac.compare_digest(password, _password()):
            _login_fail(ip)
            raise HTTPException(status_code=401, detail="密码错误")
        _login_ok(ip)
        response.set_cookie(
            COOKIE_NAME, _make_token(_password()),
            max_age=SESSION_TTL, httponly=True, samesite="lax", path="/")
        return {"ok": True}

    @router.post("/logout")
    def logout(request: Request, response: Response) -> Dict[str, Any]:
        _guard(request)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @router.get("/session")
    def session_check(request: Request) -> Dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME, "")
        if token and _verify_token(token, _password()):
            return {"ok": True}
        raise HTTPException(status_code=401, detail="未登录")

    @router.get("/overview")
    def overview(request: Request) -> Dict[str, Any]:
        _guard(request)
        snapshot = settings.snapshot()
        configured = {
            "worldcodes": bool(snapshot["providers"]["worldcodes"].get("api_key")),
            "fal": bool(snapshot["providers"]["fal"].get("api_key"))
                   or bool(os.environ.get("FAL_KEY", "").strip()),
            "baidu": bool(snapshot["providers"]["baidu"].get("api_key")
                          and snapshot["providers"]["baidu"].get("secret_key")),
            "local": True,
        }
        return {
            "stats": stats_fn(),
            "chain": snapshot["chain"],
            "maintenance": snapshot["maintenance"],
            "free_mode": bool(snapshot.get("free_mode")),
            "moderation": snapshot.get("moderation", {}),
            "moderation_detail": {
                "enabled": bool(snapshot.get("moderation", {}).get("enabled")),
                "block_on_error": bool(snapshot.get("moderation", {}).get("block_on_error")),
                "wechat_sec_ready": settings.wechat_sec_ready() if hasattr(settings, "wechat_sec_ready") else bool(
                    snapshot.get("moderation", {}).get("enabled")
                    and snapshot.get("moderation", {}).get("wechat_push_token")
                    and snapshot.get("wechat", {}).get("app_id")
                    and snapshot.get("wechat", {}).get("app_secret")
                    and settings.cos_ready()
                ),
                "tencent_ims_ready": settings.moderation_ready(),
                "wechat_has_appid": bool(snapshot.get("wechat", {}).get("app_id")),
                "wechat_has_secret": bool(snapshot.get("wechat", {}).get("app_secret")),
                "wechat_has_token": bool(snapshot.get("moderation", {}).get("wechat_push_token")),
                "cos_ready": settings.cos_ready(),
            },
            "cos_ready": bool(snapshot.get("tencent", {}).get("secret_id")
                              and snapshot.get("tencent", {}).get("secret_key")
                              and snapshot.get("tencent", {}).get("cos_bucket")),
            "configured": configured,
        }

    @router.post("/test_wechat")
    def test_wechat(request: Request) -> Dict[str, Any]:
        _guard(request)
        import wechat_sec
        try:
            token = wechat_sec.get_access_token(settings, force_refresh=True)
            return {"ok": True, "message": "微信 access_token 获取成功！（前缀：%s...）" % token[:10]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.get("/settings")
    def get_settings(request: Request) -> Dict[str, Any]:
        _guard(request)
        return _masked_settings(settings.snapshot())

    @router.put("/settings")
    async def put_settings(request: Request) -> Dict[str, Any]:
        _guard(request)
        patch = _unmask_secrets(await _json_body(request), settings.snapshot())
        try:
            updated = settings.update(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _masked_settings(updated)

    @router.get("/announcements")
    def list_announcements(request: Request) -> Dict[str, Any]:
        _guard(request)
        return {"items": announcements.list_all()}

    @router.post("/announcements")
    async def create_announcement(request: Request) -> Dict[str, Any]:
        _guard(request)
        body = await _json_body(request)
        try:
            item = announcements.create(
                title=str(body.get("title") or ""),
                body=str(body.get("body") or ""),
                level=str(body.get("level") or "info"),
                enabled=bool(body.get("enabled", True)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return item

    @router.put("/announcements/{item_id}")
    async def update_announcement(item_id: str, request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            return announcements.update(item_id, await _json_body(request))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/announcements/{item_id}")
    def delete_announcement(item_id: str, request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            announcements.delete(item_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="公告不存在") from exc
        return {"ok": True}

    @router.get("/users")
    def list_users(request: Request, limit: int = 30) -> Dict[str, Any]:
        _guard(request)
        return {"items": users.list_users(min(max(1, limit), 200))}

    @router.put("/users/{openid}/balance")
    async def set_user_balance(openid: str, request: Request) -> Dict[str, Any]:
        """管理员调整用户光子：body 传 {balance: 绝对值} 或 {delta: 增减量}。"""
        _guard(request)
        if users.get_user(openid) is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        body = await _json_body(request)
        try:
            if body.get("delta") is not None:
                balance = users.add_balance(openid, int(body["delta"]))
            else:
                balance = users.set_balance(openid, int(body.get("balance") or 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="金额必须是整数") from exc
        users.audit(openid, "admin_adjust_balance",
                    "delta=%s balance=%s" % (body.get("delta"), balance))
        return {"ok": True, "balance": balance}

    @router.put("/users/{openid}/ban")
    async def set_user_ban(openid: str, request: Request) -> Dict[str, Any]:
        """封禁/解封用户：封禁后无法提交任务，登录与历史查看不受影响。"""
        _guard(request)
        if users.get_user(openid) is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        body = await _json_body(request)
        banned = bool(body.get("banned"))
        users.set_banned(openid, banned)
        users.audit(openid, "banned_by_admin" if banned else "unbanned_by_admin")
        return {"ok": True, "banned": banned}

    @router.get("/audit")
    def list_audit(request: Request, limit: int = 50) -> Dict[str, Any]:
        _guard(request)
        return {"items": users.recent_audit(min(max(1, limit), 200))}

    @router.get("/jobs")
    def list_jobs(request: Request, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        _guard(request)
        offset = max(0, offset)
        limit = min(max(1, limit), 200)
        items, total = jobs.list_recent(offset=offset, limit=limit)
        return {"items": items, "total": total}

    # ------------------------------------------------------------------ #
    # 模板与分组 CRUD（后台热调，小程序 GET /api/templates 现读）
    # ------------------------------------------------------------------ #
    @router.get("/template-groups")
    def list_template_groups(request: Request) -> Dict[str, Any]:
        _guard(request)
        groups = templates.list_groups()
        counts: Dict[str, int] = {}
        for t in templates.list_templates():
            counts[t["group_id"]] = counts.get(t["group_id"], 0) + 1
        for g in groups:
            g["template_count"] = counts.get(g["id"], 0)
        return {"items": groups}

    @router.post("/template-groups")
    async def create_template_group(request: Request) -> Dict[str, Any]:
        _guard(request)
        body = await _json_body(request)
        try:
            return templates.create_group(
                name=str(body.get("name") or ""),
                sort=int(body.get("sort") or 99),
                enabled=bool(body.get("enabled", True)))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/template-groups/{group_id}")
    async def update_template_group(group_id: str, request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            return templates.update_group(group_id, await _json_body(request))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/template-groups/{group_id}")
    def delete_template_group(group_id: str, request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            templates.delete_group(group_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="分组不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @router.get("/templates")
    def list_templates(request: Request) -> Dict[str, Any]:
        _guard(request)
        items = templates.list_templates()
        # 附带解析后的封面与 3 张示例图 URL，后台预览用
        for t in items:
            t["cover_url"] = resolve_cover(t, settings)
            t["covers_urls"] = resolve_covers(t, settings)
        return {"items": items}

    @router.post("/templates")
    async def create_template(request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            return templates.create_template(await _json_body(request))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/templates/{tpl_id}")
    async def update_template(tpl_id: str, request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            return templates.update_template(tpl_id, await _json_body(request))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/templates/{tpl_id}")
    def delete_template(tpl_id: str, request: Request) -> Dict[str, Any]:
        _guard(request)
        try:
            templates.delete_template(tpl_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="模板不存在") from exc
        return {"ok": True}

    @router.post("/templates/{tpl_id}/cover")
    @router.post("/templates/{tpl_id}/covers")
    def upload_template_cover(tpl_id: str, request: Request,
                              slot: int = Query(0),
                              file: UploadFile = File(...)) -> Dict[str, Any]:
        """上传模板示例图（支持 3 张，slot=0, 1, 2）。压到长边 720 的 JPEG。

        配了 COS 传到 cos:covers/（/api/templates 读时现场签名，流量走 COS）；
        否则落盘 data/covers/ 走后端本地服务（开发期用）。

        同步处理且按块限长读取：超限直接断，不把大文件整个读进内存。
        """
        _guard(request)
        slot = max(0, min(int(slot), 2))
        limit = 10 * 1024 * 1024
        chunks = []
        received = 0
        try:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > limit:
                    raise HTTPException(status_code=413, detail="封面不能超过 10 MB")
                chunks.append(chunk)
        finally:
            try:
                file.file.close()
            except Exception:  # noqa: BLE001
                pass
        data = b"".join(chunks)
        try:
            jpg = _compress_cover(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        current = templates.get_template(tpl_id)
        if current is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        next_v = int(current.get("cover_v", 0) or 0) + 1

        # COS 用版本化对象键（covers/{id}_s{slot}_v{n}.jpg），免缓存刷新
        cover_key = "covers/%s_s%d_v%d.jpg" % (tpl_id, slot, next_v)
        cover_ref = "local:%s_s%d_v%d.jpg" % (tpl_id, slot, next_v)
        local_name = "%s_s%d_v%d.jpg" % (tpl_id, slot, next_v)
        if settings.cos_ready():
            try:
                from cos_store import CosError, put_object as cos_put
                cos_put(settings, cover_key, jpg)
                cover_ref = "cos:%s" % cover_key
            except CosError as exc:
                log.warning("封面传 COS 失败，退本地存储: %s", exc)
                cover_ref = "local:%s_s%d.jpg" % (tpl_id, slot)
                local_name = "%s_s%d.jpg" % (tpl_id, slot)
        with open(os.path.join(covers_dir(), local_name), "wb") as fh:
            fh.write(jpg)   # 本地永远留一份降级副本
        updated = templates.set_cover_slot(tpl_id, slot, cover_ref)
        updated["cover_url"] = resolve_cover(updated, settings)
        updated["covers_urls"] = resolve_covers(updated, settings)
        return updated

    @router.delete("/templates/{tpl_id}/covers/{slot}")
    def delete_template_cover(tpl_id: str, slot: int, request: Request) -> Dict[str, Any]:
        """删除指定槽位的示例图。"""
        _guard(request)
        updated = templates.delete_cover_slot(tpl_id, slot)
        updated["cover_url"] = resolve_cover(updated, settings)
        updated["covers_urls"] = resolve_covers(updated, settings)
        return updated

    return router


def _compress_cover(data: bytes, max_side: int = 720,
                    quality: int = 85) -> bytes:
    """封面统一压成长边 720 的 JPEG：单张 60~150KB，列表页流量可控。"""
    import io

    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法识别的图片格式") from exc
    w, h = img.size
    long_side = max(w, h)
    if long_side > max_side:
        scale = max_side / float(long_side)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return body
