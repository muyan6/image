# -*- coding: utf-8 -*-
"""腾讯云 COS 对象存储：预签名直传 + 服务端读写。

签名是 COS XML API 的 q-sign-algorithm=sha1 规范（与 TC3 不同），纯手写：

    KeyTime    = "start;end"
    SignKey    = HmacSHA1(SecretKey, KeyTime)
    HttpString = "{method}\n{uri}\n{params}\n{headers}\n"
    StringToSign = "sha1\n{KeyTime}\n{sha1(HttpString)}\n"
    Signature  = HmacSHA1(SignKey, StringToSign)

预签名 URL 把签名放在 query（q-header-list / q-url-param-list 留空），
客户端不需要带任何自定义头 —— 小程序 wx.request PUT 与 wx.downloadFile 都能用。

地域注意：服务器与桶同地域最省流量；不同地域也能用，只是拉取走公网计费。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Dict, Optional

import requests

from settings_store import SettingsStore

log = logging.getLogger("rescue.cos")


class CosError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "COS_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _conf(settings: SettingsStore) -> Dict[str, str]:
    tc = settings.tencent()
    if not (tc.get("secret_id") and tc.get("secret_key")
            and tc.get("cos_bucket") and tc.get("cos_region")):
        raise CosError("COS 未配置完整（密钥 + 桶名 + 地域）", code="NOT_CONFIGURED")
    return tc


def _host(conf: Dict[str, str], internal: bool = False) -> str:
    """内网请求优先走 cos-internal 保证 0 公网流量与千兆速度；外部请求走自定义域名或公网域名。"""
    if internal:
        return "%s.cos-internal.%s.myqcloud.com" % (conf["cos_bucket"], conf["cos_region"])
    custom = str(conf.get("cos_custom_domain") or "").strip()
    if custom:
        custom = custom.replace("https://", "").replace("http://", "").rstrip("/")
        if custom:
            return custom
    return "%s.cos.%s.myqcloud.com" % (conf["cos_bucket"], conf["cos_region"])


def _sign_key(conf: Dict[str, str], key_time: str) -> str:
    """官方规范: SignKey = Hex(HmacSHA1(SecretKey, KeyTime))——十六进制字符串,
    第二次 HMAC 用它字符串本身做密钥,不能用原始字节。"""
    return hmac.new(conf["secret_key"].encode("utf-8"),
                     key_time.encode("utf-8"), hashlib.sha1).hexdigest()


def presign(settings: SettingsStore, method: str, key: str,
            ttl_seconds: int = 3600, internal: bool = False) -> str:
    """生成预签名 URL。method: put / get / head / delete（小写）。"""
    conf = _conf(settings)
    now = int(time.time())
    key_time = "%d;%d" % (now, now + ttl_seconds)
    sign_key = _sign_key(conf, key_time)

    uri = "/" + key.lstrip("/")
    http_string = "%s\n%s\n%s\n%s\n" % (method.lower(), uri, "", "")
    string_to_sign = "sha1\n%s\n%s\n" % (
        key_time, hashlib.sha1(http_string.encode("utf-8")).hexdigest())
    signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"),
                         hashlib.sha1).hexdigest()

    query = (
        "q-sign-algorithm=sha1&q-ak={ak}&q-sign-time={kt}&q-key-time={kt}"
        "&q-header-list=&q-url-param-list=&q-signature={sig}".format(
            ak=conf["secret_id"], kt=key_time, sig=signature))
    return "https://%s%s?%s" % (_host(conf, internal=internal), uri, query)


def put_object(settings: SettingsStore, key: str, data: bytes,
               content_type: str = "image/jpeg") -> None:
    """服务端直接写入一个对象。优先走腾讯云同地域内网专线（0 流量费，不占公网带宽）。"""
    # 优先尝试走内网，若非云端环境（如本地调试）自动回退公网
    for internal in (True, False):
        try:
            url = presign(settings, "put", key, internal=internal)
            resp = requests.put(url, data=data,
                                headers={"Content-Type": content_type},
                                timeout=(8, 45))
            if resp.status_code == 200:
                return
            if not internal:
                raise CosError("COS 上传失败 HTTP %s: %s" % (resp.status_code, resp.text[:120]),
                               status=resp.status_code)
        except requests.RequestException:
            if not internal:
                raise


def get_object(settings: SettingsStore, key: str) -> bytes:
    """服务端直接读取对象。优先走腾讯云同地域内网专线（0 流量费，不占公网带宽）。"""
    for internal in (True, False):
        try:
            url = presign(settings, "get", key, internal=internal)
            resp = requests.get(url, timeout=(8, 45))
            if resp.status_code == 200:
                return resp.content
            if not internal:
                raise CosError("COS 读取失败 HTTP %s" % resp.status_code,
                               status=resp.status_code)
        except requests.RequestException:
            if not internal:
                raise
    raise CosError("COS 读取异常")


def head_exists(settings: SettingsStore, key: str) -> bool:
    for internal in (True, False):
        try:
            url = presign(settings, "head", key, internal=internal)
            resp = requests.head(url, timeout=(6, 15))
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
    return False
