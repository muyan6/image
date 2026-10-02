"""Per-post community management, preserving the existing settings-backed data."""
from __future__ import annotations
import copy
import logging
import os
import re
import time
import uuid
import io
import threading
from collections import OrderedDict
from urllib.parse import urlsplit, unquote

CATEGORIES = {"all": "其它", "portrait": "冷白人像", "film": "复古胶片",
              "old_photo": "老照片复苏", "anime": "动漫重绘"}
MEDIA_PREFIX = "/api/community/media/"


def editorial_thumbnail(settings, url):
    """Small media for a published editorial row; third-party/signed URLs stay intact."""
    url=str(url or '')
    if re.fullmatch(re.escape(MEDIA_PREFIX)+r'[0-9a-f]{32}\.jpg',url):
        filename=url[len(MEDIA_PREFIX):]
        try:
            source=os.stat(os.path.join(CommunityStore(settings).media_dir,filename))
            return url+'/thumbnail?v='+str(source.st_mtime_ns)
        except OSError:return url
    try:
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return url
        conf=settings.tencent()
        from cos_store import thumbnail_url, _host
        hosts={urlsplit('https://'+host).hostname for host in
               (_host(conf), '%s.cos.%s.myqcloud.com'%(conf.get('cos_bucket',''),conf.get('cos_region','')))}
        if parsed.hostname not in hosts:return url
        key=unquote(parsed.path).lstrip('/')
        if not key or '%' in key or '\\' in key or any(x in ('.','..') for x in key.split('/')) or any(ord(x)<32 for x in key):return url
        return thumbnail_url(settings,key,ttl_seconds=300)
    except (ValueError,KeyError,RuntimeError):return url


class CommunityStore:
    def __init__(self, settings):
        self.settings = settings

    @property
    def media_dir(self):
        return os.path.join(os.path.dirname(self.settings._path), "community_media")

    def _normalize(self):
        conf = (self.settings.community_snapshot() if hasattr(self.settings, "community_snapshot")
                else self.settings.snapshot().get("community")) or {}
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
                # Keep historical category IDs; display taxonomy is a read-only
                # catalog projection, not a destructive category migration.
                category = item.get('category')
                item["category"] = category if isinstance(category,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',category) else 'all'
                item["quality"] = "light" if item.get("quality") == "light" else "fine"
                item.setdefault("created_at", time.time())
                item.setdefault("updated_at", item["created_at"])
                items.append(item)
            doc.update(enabled=True, items=items, posts_version=1)
        return self.settings.mutate_community(migrate)

    def public_items(self):
        """Bounded, non-personalized editorial projection; no media signatures/users.

        The source revision is checked under the settings write lock, so pause,
        deletion and edits invalidate before the next read. Submission visibility,
        account bans, likes and comments are deliberately never cached here.
        """
        def published():
            return self.list(status='published',limit=None)['items']
        if not hasattr(self.settings, "section_revision"):
            return copy.deepcopy(published())
        with self.settings._lock:
            revision = self.settings.section_revision("community")
            cached = getattr(self.settings, "_community_public_cache", None)
            now = time.time()
            if not cached or cached[0] != revision or not 0 <= now - cached[1] < 30:
                rows = published()
                # Normalization can atomically replace the legacy source.
                revision = self.settings.section_revision("community")
                cached = (revision, now, rows)
                self.settings._community_public_cache = cached
            return copy.deepcopy(cached[2])

    def thumbnail_bytes(self, filename):
        """Read-only legacy media variant, gated on current published references.

        This does not write a thumbnail file or alter the 1600px editorial source.
        Source mtime+size invalidates a 32-image/8MiB in-memory cache. Visibility
        is checked before every cache hit and after decoding; only image work
        is serialized by the separate cache lock, never the settings write lock.
        """
        if not re.fullmatch(r'[0-9a-f]{32}\.jpg',filename):raise FileNotFoundError(filename)
        ref=MEDIA_PREFIX+filename
        with self.settings._lock:
            if not self._public_media_visible(ref):raise FileNotFoundError(filename)
            lock=getattr(self.settings,'_community_thumbnail_lock',None)
            if lock is None:lock=self.settings._community_thumbnail_lock=threading.RLock()
        with lock:
            path=os.path.join(self.media_dir,filename);stat=os.stat(path)
            key=(filename,stat.st_mtime_ns,stat.st_size)
            cache=getattr(self.settings,'_community_thumbnail_cache',None)
            if cache is None:cache=self.settings._community_thumbnail_cache=OrderedDict()
            if key in cache:
                cache.move_to_end(key);data=cache[key]
            else:
                from PIL import Image, ImageOps
                with Image.open(path) as source:
                    if source.width*source.height>40_000_000:raise ValueError('Editorial image is too large')
                    image=ImageOps.exif_transpose(source).convert('RGB');image.thumbnail((480,480))
                    output=io.BytesIO();image.save(output,'JPEG',quality=70);data=output.getvalue()
                for old in list(cache):
                    if old[0]==filename:del cache[old]
                if len(data)<=8*1024*1024:
                    while cache and (len(cache)>=32 or sum(len(v) for v in cache.values())+len(data)>8*1024*1024):cache.popitem(last=False)
                    cache[key]=data
        with self.settings._lock:
            if not self._public_media_visible(ref):raise FileNotFoundError(filename)
        return data

    def _public_media_visible(self, ref):
        """Caller holds settings._lock; no whole-section clone for a byte request."""
        conf=self.settings._data.get('community') or {}
        if not conf.get('enabled'):return False
        if conf.get('posts_version')!=1:conf=self._normalize()
        return any(p.get('status')=='published' and any(p.get(field)==ref for field in
            ('result_url','orig_url','author_avatar')) for p in conf.get('items',[]) if isinstance(p,dict))

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
        end = None if limit is None else max(0,offset) + max(1,min(200,limit))
        return {"items": copy.deepcopy(items[max(0, offset):end]),
                "total": len(items), "stats": stats}

    def _category_labels(self):
        provider = getattr(self.settings,'_community_category_provider',None)
        return {**CATEGORIES, **(provider() if provider else {})}

    def _validated(self, data, existing=None, category_labels=None):
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
        labels = category_labels if category_labels is not None else (CATEGORIES if existing else self._category_labels())
        unchanged = bool(existing and item.get('category') == existing.get('category'))
        category = item.get('category')
        if not isinstance(category,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',category) or (category not in labels and not unchanged):
            raise ValueError("请选择有效的帖子分类")
        item["category_name"] = item["category_name"] or labels.get(category,'未分组')
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
        labels = self._category_labels()
        result = {}
        def edit(doc):
            for index, current in enumerate(doc["items"]):
                if current["id"] == post_id:
                    result.update(self._validated(data, current, labels))
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
            conf = (self.settings.community_snapshot() if hasattr(self.settings,'community_snapshot') else self.settings.snapshot().get('community',{}))
            kept = {p.get(key) for p in conf.get('items',[]) for key in ("result_url", "orig_url", "author_avatar") if isinstance(p,dict)}
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
