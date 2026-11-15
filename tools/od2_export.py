# -*- coding: utf-8 -*-
"""把本仓库的 Secret 原值搬进新仓库(默认只演练)。

为什么在 CI 里搬: 盘名/目录名/凭证的真值只存在 Secret 里, 本机没有副本。
  本脚本跑在「持有旧 Secret 的仓库」的 runner 上, 读环境变量 → 直接写目标仓库,
  值既不出 CI, 也不落盘。日志里只有 Secret 名 / HTTP 状态 / 字节数 / 脱敏形状。

PATHS_JSON 的反向换算(--reverse-paths):
  2026-11-13 的 od2_migrate 把 OneDrive2 的目录整体挪到了源盘的 <prefix>/ 下
  (onedrive2:X -> gdrive2:<prefix>/X, /OneDrive2/X -> /GoogleDrive2/<prefix>/X),
  这里做逆运算, 并把容量卡片里被摘掉的 OneDrive2 行加回来。
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SRC_REMOTE = "gdrive2"          # 当前这套用的源侧远端
SRC_MOUNT = "/GoogleDrive2"
DST_REMOTE = "onedrive2"
DST_MOUNT = "/OneDrive2"
QUOTA_ROW = {"k": "rclone", "a": "onedrive2", "n": "OneDrive2"}


def h8(s):
    return hashlib.sha1((s or "").encode()).hexdigest()[:8]


def gh(method, path, body=None, tok=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "token " + tok,
                                          "User-Agent": "od2-export",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with OP.open(req, timeout=90) as x:
            t = x.read().decode()
            return x.status, (json.loads(t) if t.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def put_secret(repo, name, value, tok):
    st, d = gh("GET", "/repos/%s/actions/secrets/public-key" % repo, tok=tok)
    if st != 200:
        print("   !! %-22s 取公钥失败 %s %s" % (name, st, str(d)[:120]))
        return False
    from nacl import encoding, public as npub
    pk = npub.PublicKey(d["key"].encode(), encoding.Base64Encoder())
    enc = base64.b64encode(npub.SealedBox(pk).encrypt(value.encode())).decode()
    st, d = gh("PUT", "/repos/%s/actions/secrets/%s" % (repo, name),
               {"encrypted_value": enc, "key_id": d["key_id"]}, tok=tok)
    print("   %-22s -> %s  %d B" % (name, st, len(value)))
    return st in (201, 204)


# ---------------- 反向换算 ----------------
def is_json_str(v):
    return isinstance(v, str) and v.strip()[:1] in "[{" and v.strip()[-1:] in "]}"


def rev_value(s, prefix):
    """gdrive2:<prefix>/X -> onedrive2:X ; /GoogleDrive2/<prefix>/X -> /OneDrive2/X"""
    t = s.strip()
    rp = SRC_REMOTE + ":" + prefix
    if t == rp:
        return DST_REMOTE
    if t.startswith(rp + "/"):
        return "%s:%s" % (DST_REMOTE, t[len(rp) + 1:])
    mp = SRC_MOUNT.rstrip("/") + "/" + prefix
    if t == mp:
        return DST_MOUNT
    if t.startswith(mp + "/"):
        return "%s/%s" % (DST_MOUNT, t[len(mp) + 1:])
    return s


def rev(v, prefix):
    """递归改; 返回 (新值, 命中数)"""
    if isinstance(v, dict):
        out, n = {}, 0
        for k, x in v.items():
            nv, m = rev(x, prefix)
            out[k] = nv
            n += m
        return out, n
    if isinstance(v, list):
        out, n = [], 0
        for x in v:
            nv, m = rev(x, prefix)
            out.append(nv)
            n += m
        return out, n
    if is_json_str(v):
        try:
            inner = json.loads(v)
        except Exception:
            return v, 0
        ni, m = rev(inner, prefix)
        if not m:
            return v, 0
        return json.dumps(ni, ensure_ascii=False), m
    if isinstance(v, str):
        parts, n = [], 0
        for one in v.split(","):
            raw, s = one, one.strip()
            if not s:
                parts.append(one)
                continue
            nv = rev_value(s, prefix)
            if nv != s:
                n += 1
            parts.append(nv)
        return ",".join(parts), n
    return v, 0


def shape(v):
    if not isinstance(v, str):
        return repr(v)[:60]
    out = []
    for one in v.split(","):
        s = one.strip()
        if not s:
            continue
        if ":" in s and not s.startswith("/"):
            r, p = s.split(":", 1)
            out.append("<r:%s>:%s" % (h8(r), p))
        elif s.startswith("/"):
            seg = s.strip("/").split("/")
            out.append("<m:%s>/%s" % (h8("/" + seg[0]), "/".join(seg[1:])))
        else:
            out.append(s)
    return ",".join(out)


def reverse_paths(raw, prefix):
    d = json.loads(raw)
    segs = set()
    for k, v in d.items():
        for one in (str(v).split(",") if isinstance(v, str) else []):
            s = one.strip()
            if s.startswith(SRC_REMOTE + ":") and "/" in s:
                segs.add(s.split(":", 1)[1].split("/")[0])
    print("   源盘上出现的目录层: %s" % sorted(segs))

    hits = []
    new = {}
    for k, v in d.items():
        if k == "FS_QUOTA":
            arr = json.loads(v) if is_json_str(v) else (v if isinstance(v, list) else [])
            if not any(isinstance(q, dict) and q.get("a") == DST_REMOTE for q in arr):
                idx = next((i for i, q in enumerate(arr)
                            if isinstance(q, dict) and q.get("k") == "rclone"), 0)
                arr = list(arr[:idx + 1]) + [QUOTA_ROW] + list(arr[idx + 1:])
                print("   %-18s 容量行 加回 OneDrive2 (共 %d 行)" % (k, len(arr)))
                hits.append(k)
            new[k] = json.dumps(arr, ensure_ascii=False) if is_json_str(v) else arr
            continue
        if k == "_WORDS" and isinstance(v, dict):
            w = {a: b for a, b in v.items() if a != prefix}
            if len(w) != len(v):
                print("   %-18s _WORDS 去掉 %s 一项" % (k, prefix))
                hits.append(k)
            w.setdefault(DST_REMOTE, "远端二")
            new[k] = w
            continue
        nv, n = rev(v, prefix)
        if n:
            print("   %-18s %s  ->  %s   (%d 处)" % (k, shape(v)[:70], shape(nv)[:70], n))
            hits.append(k)
        new[k] = nv
    print("   命中 %d 个 key: %s" % (len(hits), ", ".join(hits)))
    return json.dumps(new, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--names", default="")
    ap.add_argument("--prefix", default="onedrive")
    ap.add_argument("--reverse-paths", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    tok = os.environ.get("OD2_TOKEN", "")
    if not tok:
        print("!! 缺 OD2_TOKEN(目标仓库的写权限 token)")
        return 1
    names = [x.strip() for x in a.names.split(",") if x.strip()]
    print("目标仓库: %s   要搬 %d 个: %s" % (a.repo, len(names), ", ".join(names)))
    st, d = gh("GET", "/repos/%s" % a.repo, tok=tok)
    print("目标仓库可写: %s (%s)" % (st == 200 and bool(d.get("permissions", {}).get("push")), st))
    if st != 200:
        return 1

    todo = []
    for n in names:
        v = os.environ.get(n, "")
        if not v:
            print("   %-22s 本仓库没有这个 Secret, 跳过" % n)
            continue
        if n == "PATHS_JSON" and a.reverse_paths:
            v2 = reverse_paths(v, a.prefix)
            print("   %-22s 反向换算完成 %d -> %d 字节" % (n, len(v), len(v2)))
            v = v2
        todo.append((n, v))
    print("要写入 %d 个" % len(todo))
    if not a.apply:
        print("(演练完; 加 --apply 才真写)")
        return 0
    ok = 0
    for n, v in todo:
        ok += put_secret(a.repo, n, v, tok)
    st, d = gh("GET", "/repos/%s/actions/secrets?per_page=100" % a.repo, tok=tok)
    got = sorted(x["name"] for x in (d.get("secrets") or []))
    print("写入成功 %d / %d" % (ok, len(todo)))
    print("目标仓库现有 Secret(名字): %s" % ", ".join(got))
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
