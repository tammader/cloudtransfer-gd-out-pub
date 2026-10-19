# -*- coding: utf-8 -*-
"""139 移动云盘 - 个人云(personal_new) OpenAPI 客户端

复刻 alist `drivers/139` 的个人云实现(签名与请求头照抄), 只用标准库, 不依赖 alist。
用于: 列目录 / 取直链 / 移动文件 / 建目录  (ydy2 流水线)

协议要点:
  host    : POST https://user-njs.yun.139.com/user/route/qryRoutePolicy
            -> data.routePolicyList[] 里 modName=="personal" 的 httpsUrl
  列目录  : POST {host}/file/list           {parentFileId, orderBy, orderDirection, pageInfo}
  取直链  : POST {host}/file/getDownloadUrl {fileId} -> data.cdnUrl | data.url
  移动    : POST {host}/file/batchMove      {fileIds:[...], toParentFileId}
  建目录  : POST {host}/file/create         {parentFileId,name,type:"folder",fileRenameMode:"force_rename"}
  鉴权    : Authorization: Basic <存储 addition 里的 authorization>
  签名    : Mcloud-Sign: "{ts},{rand},{calSign(body,ts,rand)}"

账号安全: account 就是手机号, **绝不能进日志**(仓库是公开的)。
"""
import base64
import hashlib
import json
import random
import re
import string
import time
import urllib.parse
import urllib.request

ROUTE_BASE = "https://user-njs.yun.139.com"
REFRESH_URL = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
PERSONAL_MODULE = "personal"


def _enc(s):
    """复刻 Go 的 encodeURIComponent(url.QueryEscape + 5 处还原)"""
    r = urllib.parse.quote(s, safe="")
    for a, b in (("+", "%20"), ("%21", "!"), ("%27", "'"),
                 ("%28", "("), ("%29", ")"), ("%2A", "*")):
        r = r.replace(a, b)
    return r


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def cal_sign(body, ts, rand):
    """复刻 alist calSign: enc -> 逐字符排序 -> base64 -> md5(md5(b64)+md5(ts:rand)) 转大写"""
    b = _enc(body)
    b = "".join(sorted(b))
    b64 = base64.b64encode(b.encode("utf-8")).decode()
    return _md5(_md5(b64) + _md5(ts + ":" + rand)).upper()


def parse_account(authorization):
    """解 authorization(base64) -> (account, token段)"""
    try:
        raw = base64.b64decode(authorization).decode("utf-8", "replace")
        sp = raw.split(":")
        if len(sp) >= 3:
            return sp[1], sp[2]
    except Exception:
        pass
    return "", ""


class TyPersonal(object):
    def __init__(self, authorization, host=""):
        self.authorization = (authorization or "").strip()
        self.account, self.token_seg = parse_account(self.authorization)
        self.host = (host or "").rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------- 底层 ----------
    def _post(self, pathname, data, base=None, svc_type="1"):
        url = (base if base is not None else self.host) + pathname
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        rand = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        sign = cal_sign(body, ts, rand)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Basic " + self.authorization,
            "Caller": "web",
            "Cms-Device": "default",
            "Content-Type": "application/json",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Sign": "%s,%s,%s" % (ts, rand, sign),
            "Mcloud-Version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": svc_type,
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": svc_type,
        }
        req = urllib.request.Request(url, data=body.encode("utf-8"),
                                     headers=headers, method="POST")
        with self.op.open(req, timeout=180) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            return json.loads(txt)
        except Exception:
            return {"success": False, "message": txt[:200]}

    # ---------- 对外 ----------
    def login(self):
        """取个人云 host。返回 True/False"""
        if self.host:
            return True
        d = self._post("/user/route/qryRoutePolicy",
                       {"userInfo": {"userType": 1, "accountType": 1,
                                     "accountName": self.account},
                        "modAddrType": 1}, base=ROUTE_BASE)
        if not d.get("success"):
            print("   !! qryRoutePolicy 失败: %s" % str(d.get("message"))[:100])
            return False
        for it in ((d.get("data") or {}).get("routePolicyList") or []):
            if it.get("modName") == PERSONAL_MODULE:
                self.host = (it.get("httpsUrl") or "").rstrip("/")
                break
        return bool(self.host)

    def list_dir(self, folder_id="/"):
        """列目录, 返回 [{'id','name','size','is_dir','updated'}]"""
        out, cursor = [], ""
        for _ in range(500):
            d = self._post("/file/list", {
                "imageThumbnailStyleList": ["Small", "Large"],
                "orderBy": "updated_at", "orderDirection": "DESC",
                "pageInfo": {"pageCursor": cursor, "pageSize": 100},
                "parentFileId": folder_id})
            if not d.get("success"):
                raise RuntimeError("列目录失败: %s" % str(d.get("message"))[:150])
            data = d.get("data") or {}
            for it in (data.get("items") or []):
                out.append({
                    "id": it.get("fileId") or "",
                    "name": it.get("name") or "",
                    "size": int(it.get("size") or 0),
                    "is_dir": (it.get("type") == "folder"),
                    "updated": it.get("updatedAt") or "",
                })
            cursor = data.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    def find_dir(self, name, parent="/"):
        """在 parent 下按名字找目录, 找到返回该项, 否则 None"""
        for x in self.list_dir(parent):
            if x["is_dir"] and x["name"] == name:
                return x
        return None

    def mkdir(self, name, parent="/"):
        return self._post("/file/create", {
            "parentFileId": parent, "name": name, "description": "",
            "type": "folder", "fileRenameMode": "force_rename"})

    def get_link(self, file_id):
        """取直链, 返回 (url, 原始响应)"""
        d = self._post("/file/getDownloadUrl", {"fileId": file_id})
        if not d.get("success"):
            return "", d
        data = d.get("data") or {}
        return (data.get("cdnUrl") or data.get("url") or ""), d

    def move(self, file_ids, to_parent):
        """移动文件/目录到 to_parent(目录 id)。file_ids 可以是 str 或 list"""
        if isinstance(file_ids, str):
            file_ids = [file_ids]
        return self._post("/file/batchMove",
                          {"fileIds": list(file_ids), "toParentFileId": to_parent})

    # ---------- token 时效 ----------
    def expires_in_days(self):
        """authorization 里带了过期毫秒时间戳(token 段按 | 分割, 第 4 个)"""
        try:
            return (int(self.token_seg.split("|")[3]) - int(time.time() * 1000)) / 86400000.0
        except Exception:
            return 999.0

    def refresh(self):
        """刷新 token, 成功返回新的 authorization(否则返回空串)"""
        if not self.token_seg or not self.account:
            return ""
        body = ("<root><token>%s</token><account>%s</account>"
                "<clienttype>656</clienttype></root>" % (self.token_seg, self.account))
        try:
            req = urllib.request.Request(
                REFRESH_URL, data=body.encode("utf-8"),
                headers={"Content-Type": "application/xml"}, method="POST")
            with self.op.open(req, timeout=90) as r:
                txt = r.read().decode("utf-8", "replace")
        except Exception as e:
            print("   !! 刷新 token 异常: %s" % str(e)[:100])
            return ""
        m = re.search(r"<return>(-?\d+)</return>", txt)
        t = re.search(r"<token>([^<]+)</token>", txt)
        if not m or m.group(1) != "0" or not t:
            d = re.search(r"<desc>([^<]*)</desc>", txt)
            print("   !! 刷新 token 失败: return=%s desc=%s"
                  % (m.group(1) if m else "?", (d.group(1) if d else "")[:80]))
            return ""
        head = base64.b64decode(self.authorization).decode("utf-8", "replace").split(":")
        return base64.b64encode(("%s:%s:%s" % (head[0], head[1], t.group(1))).encode()).decode()
