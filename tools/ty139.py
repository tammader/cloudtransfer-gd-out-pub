# -*- coding: utf-8 -*-
"""139 家庭云 API 客户端 (纯标准库, 从 alist drivers/139 复刻)
支持: 列表 / 取直链 / 改名
"""
import base64
import hashlib
import json
import random
import string
import time
import urllib.parse
import urllib.request

BASE = "https://yun.139.com"


def cal_sign(body, ts, rand):
    """复刻 alist calSign"""
    b = urllib.parse.quote(body, safe="").replace("+", "%20")
    b = "".join(sorted(b))
    b = base64.b64encode(b.encode()).decode()
    m1 = hashlib.md5(b.encode()).hexdigest()
    m2 = hashlib.md5(("%s:%s" % (ts, rand)).encode()).hexdigest()
    return hashlib.md5((m1 + m2).encode()).hexdigest().upper()


def parse_account(authorization):
    """authorization 形如 base64('pc:手机号:...') -> 手机号"""
    try:
        raw = base64.b64decode(authorization + "===").decode("utf-8", "replace")
        for sep in (":", "|"):
            parts = raw.split(sep)
            for p in parts:
                if p.isdigit() and len(p) >= 11:
                    return p
        return parts[1] if len(parts) > 1 else ""
    except Exception:
        return ""


def pick(d, *keys, default=None):
    """大小写不敏感取值 (139 接口返回小写开头, alist 源码里是 Go 风格大写)"""
    if not isinstance(d, dict):
        return default
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k in d:
            return d[k]
        if k.lower() in low:
            return low[k.lower()]
    return default


class Ty139:
    def __init__(self, authorization, cloud_id, account):
        self.auth = authorization
        self.cloud_id = cloud_id
        self.account = account or parse_account(authorization)
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path, data):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        rand = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        body = json.dumps(data, ensure_ascii=False)
        sign = cal_sign(body, ts, rand)
        req = urllib.request.Request(
            BASE + path, method="POST", data=body.encode("utf-8"),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Authorization": "Basic " + self.auth,
                "CMS-DEVICE": "default",
                "mcloud-channel": "1000101",
                "mcloud-client": "10701",
                "mcloud-sign": "%s,%s,%s" % (ts, rand, sign),
                "mcloud-version": "7.14.0",
                "Origin": "https://yun.139.com",
                "Referer": "https://yun.139.com/w/",
                "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
                "Content-Type": "application/json",
            })
        with self.op.open(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def new_json(self, data):
        d = dict(data)
        d.update({"catalogType": 3, "cloudID": self.cloud_id, "cloudType": 1,
                  "commonAccountInfo": {"account": self.account, "accountType": 1}})
        return d

    def list_dir(self, catalog_id, page_size=100):
        out, page = [], 1
        while page <= 50:
            data = self.new_json({
                "catalogID": catalog_id, "contentSortType": 0,
                "pageInfo": {"pageNum": page, "pageSize": page_size},
                "sortDirection": 1})
            r = self._post("/orchestration/familyCloud-rebuild/content/v1.2/queryContentList", data)
            if str(r.get("code")) not in ("0", "None") and r.get("success") is False:
                raise RuntimeError("列表失败: %s" % json.dumps(r, ensure_ascii=False)[:200])
            d = r.get("data") or {}
            dirs = pick(d, "catalogList", "CloudCatalogList", default=[]) or []
            files = pick(d, "contentList", "CloudContentList", default=[]) or []
            for c in dirs:
                out.append({"id": pick(c, "catalogID", "catalogId"),
                            "name": pick(c, "catalogName"),
                            "is_dir": True, "size": 0,
                            "path": pick(d, "path", default="/") or "/"})
            for c in files:
                out.append({"id": pick(c, "contentID", "contentId"),
                            "name": pick(c, "contentName"),
                            "is_dir": False,
                            "size": int(pick(c, "contentSize", default=0) or 0),
                            "path": pick(d, "path", default="/") or "/"})
            if not dirs and not files:
                break
            page += 1
        return out

    def get_link(self, content_id, path):
        data = self.new_json({"contentID": content_id, "path": path})
        r = self._post("/orchestration/familyCloud-rebuild/content/v1.0/getFileDownLoadURL", data)
        return (pick(r.get("data") or {}, "downloadURL", "downloadUrl", default="") or ""), r

    def rename(self, content_id, new_name, path):
        data = {"contentID": content_id, "contentName": new_name,
                "commonAccountInfo": {"account": self.account, "accountType": 1},
                "path": path}
        return self._post("/orchestration/familyCloud-rebuild/photoContent/v1.0/modifyContentInfo", data)
