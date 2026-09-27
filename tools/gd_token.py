# -*- coding: utf-8 -*-
"""刷新 源远端 的 access_token 并写回 rclone 配置

为什么需要: rclone 不认"access_token 为空"的 token 配置(会报
  "token expired and there's no refresh token"), 所以跑 rclone 前先用
  refresh_token 换一个新 token 写回配置, rclone 就能正常用了。

用法(在跑 rclone 之前执行一次):
    python3 tools/gd_token.py
"""
import configparser
import datetime
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import pathcfg          # 段名也不硬编码(CI=Secret / 本机=paths.local.json)
SEC = pathcfg.require("GD_SEC")


def _proxies():
    """本地(国内)调试时走代理; GitHub runner 没有这些环境变量 -> 直连"""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return {"http": v, "https": v}
    return {}


def conf_path():
    cands = [os.environ.get("RCLONE_CONF_PATH", ""),
             os.path.join(os.environ.get("HOME", ""), ".config/rclone/rclone.conf"),
             os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf"),
             os.path.expanduser("~/AppData/Roaming/rclone/rclone.conf")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return ""


def main():
    p = conf_path()
    if not p:
        print("!! 找不到 rclone.conf")
        return 1
    c = configparser.ConfigParser()
    c.read(p, encoding="utf-8")
    if not c.has_section(SEC):
        print("!! 配置里没有 [%s]" % SEC)
        return 1
    sec = c[SEC]
    cid = sec.get("client_id", "")
    csec = sec.get("client_secret", "")
    try:
        old = json.loads(sec.get("token", "{}"))
    except Exception:
        old = {}
    rt = old.get("refresh_token", "")
    if not (cid and csec and rt):
        print("!! 凭证不全: client_id=%s client_secret=%s refresh_token(len)=%d"
              % (bool(cid), bool(csec), len(rt)))
        return 1
    print("client_id: %s | refresh_token len=%d" % (cid[:36], len(rt)))
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": csec,
                                   "refresh_token": rt,
                                   "grant_type": "refresh_token"}).encode()
    op = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies()))
    try:
        d = json.loads(op.open(urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=90).read())
    except urllib.error.HTTPError as e:
        print("!! 换 token 失败 HTTP %s: %s"
              % (e.code, e.read().decode("utf-8", "replace")[:250]))
        return 1
    exp = (datetime.datetime.utcnow()
           + datetime.timedelta(seconds=int(d.get("expires_in", 3600)) - 300)
           ).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    tok = json.dumps({"access_token": d["access_token"], "token_type": "Bearer",
                      "refresh_token": rt, "expiry": exp}, separators=(",", ":"))
    txt = io.open(p, encoding="utf-8").read()
    pat = re.compile(r"(\[%s\][\s\S]*?token\s*=\s*)[^\n]*" % re.escape(SEC))
    if not pat.search(txt):
        print("!! 配置里找不到 [%s] 的 token 行" % SEC)
        return 1
    txt = pat.sub(lambda m: m.group(1) + tok, txt, count=1)
    io.open(p, "w", encoding="utf-8").write(txt)
    print("已刷新并写回: access_token=%s... expiry=%s"
          % (d["access_token"][:18], exp))
    return 0


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
