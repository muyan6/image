# -*- coding: utf-8 -*-
"""OpenAI Images 兼容网关客户端(用于 worldcodes.online / Sub2API 类中转站)。

接口与 fal_ai.FalImageEnhance 同构,方便插入 main.py 的降级链:

    client = OpenAIImagesEnhance(config)   # config = providers.worldcodes
    client.configured                      # 密钥/地址齐了才算
    client.enhance(inp, out, quality="fine", style=None, prompt=None)
    client.last_cost_cny / last_model / last_notice

调用的是网关的 OpenAI Images 风格接口:

    POST {base_url}{endpoint}        默认 /v1/images/edits
    multipart: model / prompt / image
    响应: data[0].b64_json 或 data[0].url(含 data: URI 也兼容)

网关实现五花八门:有的只认 multipart,有的只认 JSON+dataURI。
先按标准 multipart 发,遇到 404/422 再用 JSON+dataURI 兜底试一次,
两种都失败才算这家失败。
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

log = logging.getLogger("rescue.gateway")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class GatewayError(RuntimeError):
    """错误码与 FalError 对齐,方便上层统一处理。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: str = "GATEWAY_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OpenAIImagesEnhance:
    """按次计费的图像编辑模型(修图提示词驱动)。"""

    def __init__(self, config: Dict[str, Any],
                 session: Optional[requests.Session] = None) -> None:
        self._config = dict(config or {})
        self._session = session or requests.Session()
        self.last_cost_cny: Optional[float] = None
        self.last_model: Optional[str] = None
        self.last_notice: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return str(self._config.get("base_url") or "").strip().rstrip("/")

    @property
    def api_key(self) -> str:
        return str(self._config.get("api_key") or "").strip()

    @property
    def endpoint(self) -> str:
        endpoint = str(self._config.get("endpoint") or "").strip() or "/v1/images/edits"
        return endpoint if endpoint.startswith("/") else "/" + endpoint

    @property
    def timeout(self) -> int:
        try:
            return max(10, int(self._config.get("timeout", 180)))
        except (TypeError, ValueError):
            return 180

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _model_for(self, quality: str) -> str:
        key = "model_light" if quality == "light" else "model_fine"
        model = str(self._config.get(key) or "").strip()
        if not model:
            raise GatewayError("网关 %s 未配置模型" % key, code="NOT_CONFIGURED")
        return model

    def _price_for(self, quality: str) -> float:
        key = "price_light_cny" if quality == "light" else "price_fine_cny"
        try:
            return float(self._config.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    def enhance(self, input_path: str, output_path: str,
                quality: str = "fine", style: Optional[str] = None,
                prompt: Optional[str] = None,
                size: Optional[str] = None,
                model: Optional[str] = None,
                image_url: Optional[str] = None) -> str:
        """把 input_path 的图交给网关模型处理,结果写到 output_path。

        prompt 由调用方(main.py)从运行时设置里取,后台改完立即生效;
        不传时用通用兜底文案。style 仅作记录,网关提示词不按风格分叉。

        size:    OpenAI Images 标准尺寸参数(如 "2048x2048"),模板可覆盖;
                 网关不支持该参数时(400/404/415/422)自动去掉重试一次。
        model:   模板级模型覆盖(如供应商的原生 2K/4K 模型名),
                 不传则按 quality 取 model_light / model_fine。
        """
        self.last_cost_cny = None
        self.last_model = None
        self.last_notice = None

        if not self.configured:
            raise GatewayError("网关未配置 base_url 或 api_key", code="NOT_CONFIGURED")
        if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
            raise GatewayError("输入图片不存在", code="NO_INPUT")

        model = (model or "").strip() or self._model_for(quality)
        text = (prompt or "").strip() or (
            "修复并增强这张照片，提升清晰度与质感，保持画面内容与构图不变。")
        image_bytes = self._maybe_compress(input_path)
        use_size = (size or "").strip() or None

        last_error: Optional[GatewayError] = None
        for attempt in range(3):
            try:
                try:
                    if image_url:
                        try:
                            payload = self._request_image_by_url(
                                model, text, image_url, size=use_size)
                        except GatewayError as url_error:
                            if url_error.status not in (400, 404, 415, 422):
                                raise
                            # URL transport and size compatibility are independent.
                            self.last_notice = "网关不支持 URL 输入，已切换文件上传"
                            image_url = None
                            payload = self._request_image(model, text, image_bytes, size=use_size)
                    else:
                        payload = self._request_image(model, text, image_bytes,
                                                      size=use_size)
                except GatewayError as exc:
                    # 网关不认识 size 参数:去掉再试一次,不算这家失败
                    if (use_size and exc.status in (400, 404, 415, 422)
                            and exc.code not in ("NETWORK", "TIMEOUT", "RATE_LIMIT")):
                        self.last_notice = (
                            "网关不支持 size=%s，已按默认尺寸生成" % use_size)
                        log.warning("网关拒绝 size 参数(%s)，去掉后重试", exc.status)
                        use_size = None
                        payload = self._request_image(model, text, image_bytes,
                                                      size=None)
                    else:
                        raise
                break
            except GatewayError as exc:
                last_error = exc
                if exc.code not in ("NETWORK", "RATE_LIMIT", "TIMEOUT") \
                        and not (exc.status in RETRYABLE_STATUS):
                    raise
                if attempt == 2:
                    raise
                wait = 2 ** attempt * 1.5
                log.warning("网关第 %d 次尝试失败(%s),%.1fs 后重试",
                            attempt + 1, exc.code, wait)
                time.sleep(wait)

        self.last_model = model
        self.last_cost_cny = self._price_for(quality)
        self._write_output(payload, output_path)
        return output_path

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def generate(self,output_path: str,prompt: str,model: str,size: str,endpoint: str="/v1/images/generations") -> str:
        """Text-to-image has no input image; timeout is not retried blindly."""
        if not self.configured or not model:raise GatewayError('文生图模型未配置',code='NOT_CONFIGURED')
        try:
            response=self._session.post(self.base_url+endpoint,
                headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json'},
                json={'model':model,'prompt':prompt,'size':size,'n':1},timeout=(10,self.timeout))
        except requests.RequestException as exc:raise GatewayError('文生图请求未完成，请稍后查看作品状态',code='NETWORK') from exc
        self._raise_for_status(response)
        self._write_output(self._extract_image(response),output_path)
        self.last_model=model
        return output_path

    def _maybe_compress(self, path: str, limit_mb: float = 18.0) -> bytes:
        """网关普遍有 20MB 左右的请求上限,超了就压成 JPEG。"""
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) <= limit_mb * 1024 * 1024:
            return data
        try:
            import cv2
            import numpy as np
            img = cv2.imdecode(
                np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise GatewayError("无法解析待压缩图片", code="BAD_IMAGE")
            ok, buf = cv2.imencode(
                ".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not ok:
                raise GatewayError("压缩编码失败", code="BAD_IMAGE")
            log.info("输入 %.1fMB 超限,已压缩为 %.1fMB JPEG",
                     len(data) / 1048576, len(buf) / 1048576)
            return buf.tobytes()
        except ImportError:
            raise GatewayError(
                "输入超过 %.0fMB 且本地无 OpenCV 可压缩" % limit_mb,
                code="IMAGE_TOO_LARGE")

    def _request_image_by_url(self, model: str, prompt: str, image_url: str,
                              size: Optional[str] = None) -> bytes:
        """URL 直连:网关服务端自己拉取图片(如 COS 签名直链)。

        本服务器不出公网流量。网关侧格式为 images[].image_url;
        若网关拒绝该参数(400/404/415/422),由调用方回退 multipart。
        """
        url = self.base_url + self.endpoint
        body = {"model": model, "prompt": prompt,
                "images": [{"image_url": image_url}]}
        if size:
            body["size"] = size
        try:
            resp = self._session.post(url,
                headers={"Authorization": "Bearer %s" % self.api_key,
                         "Content-Type": "application/json"},
                json=body, timeout=(15, self.timeout))
        except requests.Timeout as exc:
            raise GatewayError("网关请求超时(%ds)" % self.timeout,
                               code="TIMEOUT") from exc
        except requests.RequestException as exc:
            raise GatewayError("网络错误: %s" % exc.__class__.__name__,
                               code="NETWORK") from exc
        self._raise_for_status(resp)
        return self._extract_image(resp)

    def _request_image(self, model: str, prompt: str, image_bytes: bytes,
                       size: Optional[str] = None) -> bytes:
        """先 multipart,404/415/422 时退 JSON+dataURI。返回图片原始字节。"""
        url = self.base_url + self.endpoint
        headers = {"Authorization": "Bearer %s" % self.api_key}
        mime = "image/jpeg" if image_bytes[:3] == b"\xff\xd8\xff" else "image/png"
        extra = {"size": size} if size else {}

        try:
            resp = self._session.post(
                url, headers=headers,
                files={"image": ("image.jpg", image_bytes, mime)},
                data={"model": model, "prompt": prompt, "n": "1", **extra},
                timeout=(15, self.timeout))
        except requests.Timeout as exc:
            raise GatewayError("网关请求超时(%ds)" % self.timeout,
                               code="TIMEOUT") from exc
        except requests.RequestException as exc:
            raise GatewayError("网络错误: %s" % exc.__class__.__name__,
                               code="NETWORK") from exc

        if resp.status_code in (404, 405, 415, 422):
            return self._request_image_json(
                url, headers, model, prompt, image_bytes, mime, size)
        self._raise_for_status(resp)
        return self._extract_image(resp)

    def _request_image_json(self, url: str, headers: Dict[str, str],
                            model: str, prompt: str,
                            image_bytes: bytes, mime: str,
                            size: Optional[str] = None) -> bytes:
        data_uri = "data:%s;base64,%s" % (
            mime, base64.b64encode(image_bytes).decode("ascii"))
        body: Dict[str, Any] = {"model": model, "prompt": prompt, "n": 1,
                                "image": data_uri}
        if size:
            body["size"] = size
        try:
            resp = self._session.post(
                url, headers={**headers, "Content-Type": "application/json"},
                json=body,
                timeout=(15, self.timeout))
        except requests.Timeout as exc:
            raise GatewayError("网关请求超时(%ds)" % self.timeout,
                               code="TIMEOUT") from exc
        except requests.RequestException as exc:
            raise GatewayError("网络错误: %s" % exc.__class__.__name__,
                               code="NETWORK") from exc
        self._raise_for_status(resp)
        return self._extract_image(resp)

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            body = resp.json()
            err = body.get("error")
            detail = (err.get("message") if isinstance(err, dict) else err) \
                or body.get("message") or str(body)[:200]
        except ValueError:
            detail = resp.text[:200]
        if resp.status_code in (401, 403):
            raise GatewayError("网关鉴权失败(%s): %s" % (resp.status_code, detail),
                               status=resp.status_code, code="AUTH")
        if resp.status_code == 429:
            raise GatewayError("网关限流: %s" % detail,
                               status=429, code="RATE_LIMIT")
        raise GatewayError("网关 HTTP %s: %s" % (resp.status_code, detail),
                           status=resp.status_code,
                           code="HTTP_%s" % resp.status_code)

    def _extract_image(self, resp: requests.Response) -> bytes:
        # 部分网关成功时直接回二进制图片,不走 JSON
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype.startswith("image/"):
            if resp.content:
                return resp.content
            raise GatewayError("网关返回空图片流", code="EMPTY_RESULT")

        try:
            body = resp.json()
        except ValueError as exc:
            raise GatewayError(
                "网关返回不是 JSON(status=%s, type=%s): %s"
                % (resp.status_code, ctype or "unknown", resp.text[:150]),
                code="BAD_RESPONSE") from exc

        items = body.get("data") if isinstance(body, dict) else None
        if not items or not isinstance(items, list):
            raise GatewayError(
                "网关响应缺少 data 数组: %s" % str(body)[:150],
                code="BAD_RESPONSE")

        item = items[0] if isinstance(items[0], dict) else {}
        if item.get("b64_json"):
            try:
                return base64.b64decode(item["b64_json"])
            except Exception as exc:  # noqa: BLE001
                raise GatewayError("b64_json 解码失败", code="BAD_RESPONSE") from exc

        url = item.get("url") or ""
        if url.startswith("data:"):
            _, _, b64 = url.partition("base64,")
            try:
                return base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001
                raise GatewayError("data URI 解码失败", code="BAD_RESPONSE") from exc
        if url:
            return self._download(url)

        raise GatewayError("网关响应里没有图片(b64_json/url 均为空)",
                           code="EMPTY_RESULT")

    def _download(self, url: str) -> bytes:
        """Public/signed URLs never receive gateway credentials across origins."""
        def origin(value):
            try:
                u = urlsplit(value)
                if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.port == 0:
                    return None
                return u.scheme, u.hostname.lower(), u.port if u.port is not None else (443 if u.scheme == 'https' else 80)
            except ValueError:
                return None
        target_origin = origin(url)
        if target_origin is None:
            raise GatewayError("结果图地址无效", code="BAD_RESULT_URL")
        try:
            resp = self._session.get(url, timeout=(15, self.timeout))
            effective_origin = origin(getattr(resp, 'url', None) or url)
            if (resp.status_code in (401, 403) and target_origin == origin(self.base_url)
                    and effective_origin == target_origin):
                resp = self._session.get(
                    url, headers={"Authorization": "Bearer %s" % self.api_key},
                    timeout=(15, self.timeout), allow_redirects=False)
            if 300 <= resp.status_code < 400:
                raise GatewayError("结果图鉴权地址发生重定向", code="RESULT_REDIRECT")
            if resp.status_code in (401, 403):
                raise GatewayError("结果图访问凭据失效", code="RESULT_ACCESS", status=resp.status_code)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise GatewayError("结果图下载失败: %s" % exc.__class__.__name__,
                               code="NETWORK") from exc
        if not resp.content:
            raise GatewayError("结果图为空", code="EMPTY_RESULT")
        return resp.content

    @staticmethod
    def _write_output(payload: bytes, output_path: str) -> None:
        with open(output_path, "wb") as fh:
            fh.write(payload)
