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
import json
from urllib.parse import quote
from typing import Any, Dict, Optional

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
        # 独立内网域名只存在于 tencentcos.cn，myqcloud.com 没有这个形式。
        return "%s.cos-internal.%s.tencentcos.cn" % (conf["cos_bucket"], conf["cos_region"])
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
            ttl_seconds: int = 3600, internal: bool = False,
            params: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> str:
    """生成预签名 URL。method: put / get / head / delete（小写）。"""
    conf = _conf(settings)
    now = int(time.time())
    key_time = "%d;%d" % (now, now + ttl_seconds)
    sign_key = _sign_key(conf, key_time)

    uri = "/" + key.lstrip("/")
    def canonical(values):
        pairs=sorted((quote(str(k),safe='').lower(),quote(str(v),safe='')) for k,v in (values or {}).items())
        return '&'.join(k+'='+v for k,v in pairs),';'.join(k for k,v in pairs)
    query_values,query_keys=canonical(params)
    header_values,header_keys=canonical({k.lower():v for k,v in (headers or {}).items()})
    http_string = "%s\n%s\n%s\n%s\n" % (method.lower(), uri, query_values, header_values)
    string_to_sign = "sha1\n%s\n%s\n" % (
        key_time, hashlib.sha1(http_string.encode("utf-8")).hexdigest())
    signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"),
                         hashlib.sha1).hexdigest()

    query = (
        "q-sign-algorithm=sha1&q-ak={ak}&q-sign-time={kt}&q-key-time={kt}"
        "&q-header-list={hk}&q-url-param-list={qk}&q-signature={sig}".format(
            ak=conf["secret_id"], kt=key_time, sig=signature,
            hk=quote(header_keys,safe=''),qk=quote(query_keys,safe='')))
    if params:
        query+='&'+'&'.join(quote(str(k),safe='')+'='+quote(str(v),safe='') for k,v in params.items())
    return "https://%s%s?%s" % (_host(conf, internal=internal), uri, query)


def process_image(settings: SettingsStore, source: str, target: str, rule: str) -> None:
    """CI basic processing persists a separate object; source is never overwritten."""
    if source == target:
        raise ValueError("Processed object must have a separate key")
    operations=json.dumps({'is_pic_info':1,'rules':[{'fileid':quote(target,safe='/'),'rule':rule}]},separators=(',',':'))
    headers={'Pic-Operations':operations}
    for internal in (True,False):
        try:
            response=requests.post(presign(settings,'post',source,internal=internal,
                params={'image_process':''},headers=headers),headers=headers,data=b'',timeout=(5,30))
            if response.status_code==200 and head_exists(settings,target):return
            if not internal:raise CosError('CI 基础图片处理失败 HTTP %s'%response.status_code,code='CI_PROCESSING_FAILED')
        except requests.RequestException as exc:
            if not internal:raise CosError('CI 基础图片处理请求失败',code='CI_PROCESSING_FAILED') from exc


def put_object(settings: SettingsStore, key: str, data: bytes,
               content_type: str = "image/jpeg", *, internal_only: bool = False) -> None:
    """服务端直接写入一个对象。优先走腾讯云同地域内网专线（0 流量费，不占公网带宽）。"""
    # 优先尝试走内网，若非云端环境（如本地调试）自动回退公网
    for internal in ((True,) if internal_only else (True, False)):
        try:
            url = presign(settings, "put", key, internal=internal)
            resp = requests.put(url, data=data,
                                headers={"Content-Type": content_type},
                                timeout=(8, 45))
            if resp.status_code == 200:
                return
            if not internal or internal_only:
                raise CosError("COS 上传失败 HTTP %s: %s" % (resp.status_code, resp.text[:120]),
                               status=resp.status_code)
        except requests.RequestException as exc:
            if not internal or internal_only:
                raise CosError("COS 上传网络异常", code="NETWORK") from exc


def get_object(settings: SettingsStore, key: str, max_bytes: Optional[int] = None) -> bytes:
    """服务端直接读取对象。优先走腾讯云同地域内网专线（0 流量费，不占公网带宽）。"""
    for internal in (True, False):
        try:
            url = presign(settings, "get", key, internal=internal)
            with requests.get(url, timeout=(8, 45), stream=True) as resp:
                if resp.status_code == 200:
                    length = resp.headers.get("Content-Length", "")
                    if max_bytes is not None and length.isdigit() and int(length) > max_bytes:
                        raise CosError("COS 对象超过上传大小上限", status=413, code="TOO_LARGE")
                    chunks = []
                    total = 0
                    for chunk in resp.iter_content(1024 * 1024):
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            raise CosError("COS 对象超过上传大小上限", status=413, code="TOO_LARGE")
                        chunks.append(chunk)
                    return b"".join(chunks)
                if not internal:
                    raise CosError("COS 读取失败 HTTP %s" % resp.status_code,
                                   status=resp.status_code)
        except requests.RequestException as exc:
            if not internal:
                raise CosError("COS 读取网络异常", code="NETWORK") from exc
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


def delete_object(settings: SettingsStore, key: str,
                  timeout: tuple = (6, 15)) -> None:
    """Idempotent delete: a missing object is already cleaned up."""
    for internal in (True, False):
        try:
            response = requests.delete(presign(settings, "delete", key, internal=internal), timeout=timeout)
            if response.status_code in (200, 204, 404):
                return
            if not internal:
                raise CosError("COS 删除失败 HTTP %s" % response.status_code, status=response.status_code)
        except requests.RequestException as exc:
            if not internal:
                raise CosError("COS 删除网络异常", code="NETWORK") from exc


def check_internal(settings: SettingsStore) -> Dict[str, Any]:
    """探测当前服务器与 COS 桶之间的内网连通性与 DNS 解析状态。"""
    try:
        conf = _conf(settings)
        host = _host(conf, internal=True)
        import socket
        import ipaddress
        ip = socket.gethostbyname(host)
        address = ipaddress.ip_address(ip)
        ranges = ("10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
        if not any(address in ipaddress.ip_network(network) for network in ranges):
            raise RuntimeError("内网域名未解析至腾讯云内网地址")
        return {
            "ok": True,
            "mode": "VPC_INTERNAL",
            "host": host,
            "ip": ip,
            "desc": "已连接腾讯云同地域内网专线，图片在服务器与桶之间流转 0 流量费，不占用 4M 公网带宽"
        }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "PUBLIC_FALLBACK",
            "error": str(exc),
            "desc": "内网未连通，自动降级为公网（本地开发机正常现象）"
        }
