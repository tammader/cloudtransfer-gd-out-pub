# -*- coding: utf-8 -*-
"""上游网盘 OpenAPI 客户端 —— 复刻官方 SDK 的 HL6-HMAC-SHA256 签名
只用标准库, 不依赖 alist。用于: 列目录 / 移动 / 删除 / 回收站
凭证走环境变量(HALAL_CLIENT_ID / HALAL_CLIENT_SECRET / HALAL_REFRESH_TOKEN), 不落盘、不打日志。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

HOST = "openapi.2dland.cn"
ALGO = "HL6-HMAC-SHA256"
SUFFIX = "hl6_request"
PREFIX = "HL6"


def sha256hex(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode()).hexdigest()


def hmac_sha256(key, data):
    return hmac.new(key if isinstance(key, bytes) else key.encode(),
                    data if isinstance(data, bytes) else data.encode(),
                    hashlib.sha256).digest()


def base36(n):
    digs = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while n > 0:
        s = digs[n % 36] + s
        n //= 36
    return s or "0"


def rfc3986(s):
    return urllib.parse.quote(s, safe="").replace("+", "%20")


class Halal6:
    def __init__(self, client_id, client_secret, refresh_token=""):
        self.cid = client_id
        self.secret = client_secret
        self.token = ""
        self.refresh_token = refresh_token

    def _req(self, method, path, body_obj=None, params=None, tries=4):
        """带重试的请求(网络抖动/SSL 握手超时/5xx)。

        每次重试都**重新签名**(nonce + 时间戳会变), 避免服务端重放校验拒绝。
        """
        last = ""
        for i in range(tries):
            r = self._req_once(method, path, body_obj, params)
            if "_net_error" not in r:
                return r
            last = str(r["_net_error"])
            if i < tries - 1:
                time.sleep(2 + i * 3)
        return {"_error": "网络失败(已重试 %d 次): %s" % (tries, last[:160])}

    def _req_once(self, method, path, body_obj, params=None):
        body = json.dumps(body_obj, separators=(",", ":"),
                          ensure_ascii=False).encode() if body_obj is not None else b""
        now = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        date_s = time.strftime("%Y-%m-%d", time.gmtime(now))
        nonce = base36(int(now * 1e9))
        hdrs = {"host": HOST, "x-hl-nonce": nonce, "x-hl-timestamp": ts,
                "other-header": "other-value",
                "content-type": "application/json"}
        to_sign = ["content-type", "host", "other-header",
                   "x-hl-nonce", "x-hl-timestamp"]
        canon_h = "".join("%s:%s\n" % (k, hdrs[k]) for k in sorted(to_sign))
        signed_h = ";".join(sorted(to_sign))
        q = ""
        if params:
            keys = sorted(params)
            q = "&".join("%s=%s" % (rfc3986(k), rfc3986(str(params[k]))) for k in keys)
        canon_req = "\n".join([method, path, q, canon_h, signed_h, sha256hex(body)])
        scope = "%s/%s/%s" % (date_s, self.token, SUFFIX)
        sts = "\n".join([ALGO, ts, scope, sha256hex(canon_req.encode())])
        k = (PREFIX + self.secret).encode()
        k1 = hmac_sha256(k, date_s)
        k2 = hmac_sha256(k1, self.token)
        k3 = hmac_sha256(k2, SUFFIX)
        sig = hmac.new(k3, sts.encode(), hashlib.sha256).hexdigest()
        hdrs["authorization"] = ("%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                                 % (ALGO, self.cid, scope, signed_h, sig))
        url = "https://%s%s%s" % (HOST, path, ("?" + q) if q else "")
        req = urllib.request.Request(url, method=method, data=body or None,
                                     headers=hdrs)
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with op.open(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:          # 限流/服务端错误 -> 交给上层重试
                return {"_net_error": "HTTP %s" % e.code}
            return {"_error": "HTTP %s" % e.code,
                    "_body": e.read().decode("utf-8", "replace")[:300]}
        except Exception as e:                           # URLError / timeout / 连接重置
            return {"_net_error": "%s: %s" % (type(e).__name__, e)}

    def login(self):
        """用 refresh_token 换 access_token; 失败再试 client_credentials"""
        if self.refresh_token:
            r = self._req("POST", "/v6/oauth/refresh_token",
                          {"refresh_token": self.refresh_token,
                           "grant_type": "refresh_token",
                           "client_id": self.cid})
            if r.get("access_token"):
                self.token = r["access_token"]
                self.refresh_token = r.get("refresh_token", self.refresh_token)
                return "refresh_token 方式成功"
            return "refresh_token 失败: %s" % json.dumps(r, ensure_ascii=False)[:200]
        r = self._req("POST", "/v6/oauth/refresh_token",
                      {"grant_type": "client_credentials",
                       "client_id": self.cid, "client_secret": self.secret})
        if r.get("access_token"):
            self.token = r["access_token"]
            return "client_credentials 方式成功"
        return "client_credentials 失败: %s" % json.dumps(r, ensure_ascii=False)[:200]

    def ls(self, path="/"):
        return self._req("POST", "/v6/userfile/list",
                         {"parent": {"path": path}})

    @staticmethod
    def is_dir_entry(f):
        """判断列表项是不是目录。

        6盘2 API 返回的字段是 **dir: true**(不是 is_dir!),
        目录同时带 mime_type=inode/directory、type="20"、files=<子项数>。
        曾经只读 is_dir 导致 **所有子文件夹都被当成文件**, 这里做多字段兜底。
        """
        if f.get("dir") is True or f.get("is_dir") is True:
            return True
        if str(f.get("mime_type") or "") == "inode/directory":
            return True
        return str(f.get("type") or "") == "20"

    def ls_all(self, path="/", per_page=200):
        """全量列目录 -> [(path, name, size, is_dir)]
        注意: 分页游标字段是 token; limit 是 int64, protobuf JSON 里要传字符串"""
        out, cursor = [], ""
        for _ in range(80):
            body = {"parent": {"path": path},
                    "list_info": {"limit": str(per_page)}}
            if cursor:
                body["list_info"]["token"] = cursor
            r = self._req("POST", "/v6/userfile/list", body)
            if "_error" in r:
                break
            files = r.get("files") or []
            for f in files:
                out.append((f.get("path") or "", f.get("name") or "",
                            int(f.get("size") or 0), self.is_dir_entry(f)))
            info = r.get("list_info") or {}
            cursor = info.get("token") or ""
            if not cursor or len(files) < per_page:
                break
        return out

    def move(self, src_paths, dst_dir):
        """移动(批量)到目标目录"""
        return self._req("POST", "/v6/userfile/move",
                         {"source": [{"path": p} for p in src_paths],
                          "dest": {"path": dst_dir}})

    def trash(self, paths):
        """移入回收站(可恢复, 比 delete 安全)"""
        return self._req("POST", "/v6/userfile/trash",
                         {"source": [{"path": p} for p in paths]})

    def rename(self, path, new_name):
        """改名: body 用 {path, name}"""
        return self._req("POST", "/v6/userfile/rename",
                         {"path": path, "name": new_name})

    def mkdir(self, parent, name):
        return self._req("POST", "/v6/userfile/create",
                         {"parent": {"path": parent}, "name": name})


def main():
    """自测: 凭证走环境变量 —— 不硬编码路径, 也不打印任何凭证内容"""
    cid = os.environ.get("HALAL_CLIENT_ID", "")
    sec = os.environ.get("HALAL_CLIENT_SECRET", "")
    rt = os.environ.get("HALAL_REFRESH_TOKEN", "")
    if not (cid and sec and rt):
        print("!! 需要环境变量 HALAL_CLIENT_ID / HALAL_CLIENT_SECRET / HALAL_REFRESH_TOKEN")
        return 1
    c = Halal6(cid, sec, rt)
    print("登录:", c.login())
    if not getattr(c, "token", ""):
        print("!! 拿不到 access_token")
        return 1
    print("access_token 已获取(长度 %d, 不打印内容)" % len(c.token))
    for p in ("/", "/in"):
        items = c.ls_all(p)
        print("%s -> %d 项(%d 个文件)"
              % (p, len(items), len([x for x in items if not x[3]])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
