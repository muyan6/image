"""Per-post community management, preserving the existing settings-backed data."""
from __future__ import annotations
import copy
import logging
import os
import re
import time
import uuid
from urllib.parse import urlsplit

CATEGORIES = {"all": "其它", "portrait": "冷白人像", "film": "复古胶片",
              "old_photo": "老照片复苏", "anime": "动漫重绘"}
MEDIA_PREFIX = "/api/community/media/"


class CommunityStore:
    def __init__(self, settings):
        self.settings = settings

    @property
    def media_dir(self):
        return os.path.join(os.path.dirname(self.settings._path), "community_media")

    def _normalize(self):
        conf = self.settings.snapshot().get("community") or {}
        if conf.get("posts_version") == 1:
            return conf
        def migrate(doc):
            # Recheck under the write lock; preserve an intentionally hidden legacy list.
            if doc.get("posts_version") == 1:
                return
            old_visible = bool(doc.get("enabled"))
            items = []
            seen = set()
            for raw in (doc.get("items") if isinstance(doc.get("items"), list) else []):
                if not isinstance(raw, dict):
                    continue
                item = copy.deepcopy(raw)
                key = str(item.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or key in seen:
                    key = "p_" + uuid.uuid4().hex[:16]
                seen.add(key)
                item["id"] = key
                item["status"] = item.get("status") if item.get("status") in ("published", "paused") else ("published" if old_visible else "paused")
                item["pinned"] = bool(item.get("pinned"))
                for field, default in (("sort", 100), ("likes", 0)):
                    try: item[field] = max(0, min(99999 if field == "sort" else 1_000_000_000, int(item.get(field, default))))
                    except (TypeError, ValueError, OverflowError): item[field] = default
                for field in ("title", "story", "template_name", "result_url", "orig_url"):
                    item[field] = str(item.get(field) or "")
                item["category"] = item.get("category") if item.get("category") in CATEGORIES else "all"
                item["quality"] = "light" if item.get("quality") == "light" else "fine"
                item.setdefault("created_at", time.time())
                item.setdefault("updated_at", item["created_at"])
                items.append(item)
            doc.update(enabled=True, items=items, posts_version=1)
        return self.settings.mutate_community(migrate)

    def list(self, status="all", query="", offset=0, limit=30):
        if status not in ("all", "published", "paused"):
            raise ValueError("帖子状态只能是 published、paused 或 all")
        all_items = self._normalize()["items"]
        stats = {"total": len(all_items), "published": sum(p["status"] == "published" for p in all_items),
                 "paused": sum(p["status"] == "paused" for p in all_items), "pinned": sum(bool(p["pinned"]) for p in all_items)}
        needle = query.strip().lower()[:200]
        items = [p for p in all_items if (status == "all" or p["status"] == status) and
                 (not needle or needle in " ".join(str(p.get(k) or "") for k in ("title", "story", "author_name", "template_name")).lower())]
        items.sort(key=lambda p: (not p["pinned"], p["sort"], -float(p.get("created_at") or 0), p["id"]))
        return {"items": copy.deepcopy(items[max(0, offset):max(0, offset) + max(1, min(200, limit))]),
                "total": len(items), "stats": stats}

    def _validated(self, data, existing=None):
        allowed = ("title", "story", "author_name", "author_avatar", "date", "category", "category_name",
                   "template_id", "template_name", "quality", "result_url", "orig_url", "likes", "status", "pinned", "sort")
        item = copy.deepcopy(existing or {"title": "", "story": "", "status": "paused", "pinned": False,
                    "sort": 100, "likes": 0, "category": "all", "quality": "fine", "result_url": "", "orig_url": ""})
        for key in allowed:
            if key in data: item[key] = data[key]
        for key, maximum in (("title", 100), ("story", 5000), ("author_name", 60), ("author_avatar", 1000),
                             ("date", 30), ("category_name", 30), ("template_id", 64), ("template_name", 100),
                             ("result_url", 1000), ("orig_url", 1000)):
            value = item.get(key, "")
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError("%s 必须是文本，最多 %d 字" % (key, maximum))
            item[key] = value.strip()
        if not item["title"]: raise ValueError("请填写帖子标题")
        if item["status"] not in ("published", "paused"): raise ValueError("帖子状态只能是发布或暂停展示")
        if item["quality"] not in ("light", "fine"): raise ValueError("画质档位无效")
        if item["category"] not in CATEGORIES: raise ValueError("请选择有效的帖子分类")
        item["category_name"] = item["category_name"] or CATEGORIES[item["category"]]
        if type(item["pinned"]) is not bool: raise ValueError("置顶标记必须是布尔值")
        for key, maximum in (("likes", 1_000_000_000), ("sort", 99999)):
            if type(item[key]) is not int or not 0 <= item[key] <= maximum:
                raise ValueError("%s 必须是非负整数" % key)
        for key in ("result_url", "orig_url", "author_avatar"):
            value = item.get(key, "")
            if value and not ((urlsplit(value).scheme in ("http", "https") and urlsplit(value).netloc)
                              or value.startswith((MEDIA_PREFIX, "/api/covers/"))):
                raise ValueError("图片请上传或填写 HTTP(S) 地址")
        if item["status"] == "published" and not item["result_url"]:
            raise ValueError("发布前请上传或填写结果图")
        item["updated_at"] = time.time()
        return item

    def create(self, data):
        self._normalize()
        item = self._validated(data)
        item.update(id="p_" + uuid.uuid4().hex[:16], created_at=time.time())
        def add(doc):
            if len(doc["items"]) >= 200: raise ValueError("最多保存 200 条社区帖子")
            doc["items"].append(item)
        self.settings.mutate_community(add)
        return copy.deepcopy(item)

    def update(self, post_id, data):
        self._normalize()
        result = {}
        def edit(doc):
            for index, current in enumerate(doc["items"]):
                if current["id"] == post_id:
                    result.update(self._validated(data, current))
                    doc["items"][index] = result
                    return
            raise KeyError("帖子不存在")
        self.settings.mutate_community(edit)
        return result

    def batch(self, ids, action):
        if action not in ("pause", "resume", "delete"): raise ValueError("批量操作无效")
        if not isinstance(ids, list) or not ids or len(ids) > 200 or any(not isinstance(k, str) for k in ids):
            raise ValueError("请选择 1~200 条帖子")
        self._normalize()
        selected = set(ids)
        removed = []
        def apply(doc):
            mapping = {p["id"]: p for p in doc["items"]}
            if not selected <= mapping.keys(): raise KeyError("部分帖子已不存在，请刷新列表")
            if action == "delete":
                removed.extend(p for p in doc["items"] if p["id"] in selected)
                doc["items"] = [p for p in doc["items"] if p["id"] not in selected]
            else:
                for key in selected:
                    mapping[key].update(self._validated({"status": "paused" if action == "pause" else "published"}, mapping[key]))
        self.settings.mutate_community(apply)
        if removed: self._cleanup_deleted_media(removed)
        return {"ok": True, "affected": len(selected)}

    def delete(self, post_id):
        return self.batch([post_id], "delete")

    def _cleanup_deleted_media(self, removed):
        with self.settings._lock:
            kept = {p.get(key) for p in self.settings.community()["items"] for key in ("result_url", "orig_url", "author_avatar")}
            for post in removed:
                for key in ("result_url", "orig_url", "author_avatar"):
                    url = post.get(key) or ""
                    if url in kept or not url.startswith(MEDIA_PREFIX): continue
                    name = url[len(MEDIA_PREFIX):]
                    if re.fullmatch(r"[0-9a-f]{32}\.jpg", name):
                        try: os.remove(os.path.join(self.media_dir, name))
                        except FileNotFoundError: pass
                        except OSError:
                            logging.getLogger("rescue.community").exception("帖子已删除，社区图片清理异常")
