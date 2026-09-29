"""Trusted ad-verification bridge, not a client 'isEnded' validator.

The bridge must independently validate an advertising platform's server event.
The application only credits a unique, user-bound verified event ID. Credentials
remain on the backend and are never sent to the mini-program.
"""
import hashlib

import requests


def verify_video(settings, openid: str, receipt: str) -> str:
    config = settings.ads()
    if not config["rewarded_video_ready"]:
        raise ValueError("激励视频验证服务尚未配置")
    if not receipt or len(receipt) > 4096:
        raise ValueError("缺少广告平台观看凭据")
    try:
        response = requests.post(config["rewarded_video_verifier_url"],
                                 headers={"Authorization": "Bearer " + config["rewarded_video_verifier_key"]},
                                 json={"openid": openid, "ad_unit_id": config["rewarded_video_unit_id"],
                                       "receipt": receipt}, timeout=(5, 10), allow_redirects=False)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError("广告观看验证暂未完成，请稍后重试") from exc
    event = data.get("event_id") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("verified") is not True or data.get("openid") != openid \
            or data.get("ad_unit_id") != config["rewarded_video_unit_id"] \
            or not isinstance(event, str) or not event or len(event) > 200:
        raise ValueError("广告观看凭据未通过服务端验证")
    return hashlib.sha256((config["rewarded_video_verifier_url"] + "|" +
                           config["rewarded_video_unit_id"] + "|" + event).encode("utf-8")).hexdigest()
