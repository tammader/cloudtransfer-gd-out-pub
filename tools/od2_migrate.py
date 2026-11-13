# -*- coding: utf-8 -*-
"""OneDrive2 退役迁移: 把 PATHS_JSON / PATHS_JSON_TD 里所有指向 OneDrive2 的路径改到
GoogleDrive2 的 onedrive 目录下, 顺带把容量卡片里的 OneDrive2 行摘掉。

为什么在 CI 里改而不是在本机:
  盘名/目录名的真值只存在 Secret(PATHS_JSON / PATHS_JSON_TD) 里, 本机没有副本。
  这个脚本跑在 runner 上, 用同一份 Secret 算出新字典, 再写回 Secret —— 值不出 CI,
  公开仓库的日志里只出现 key 名与脱敏形状(<r:hash8>)。

三种模式
  --hashmap   只打印"探针": CI 的 rclone 远端名 / alist 挂载名 / 每个 key 的脱敏形状。
              远端名与挂载名只给 sha1[:8], 本机拿候选名对一下就能反查是哪一个, 值不外泄。
  (默认)       演练: 打印哪些 key 会被改写、改成什么形状(带新远端占位), 不写任何东西。
  --apply      真改: 把新字典加密写回 Secret。

参数(可覆盖)
  OLD_REMOTE=onedrive2 OLD_MOUNT=/OneDrive2
  NEW_REMOTE=gdrive2   NEW_MOUNT=/GoogleDrive2  NEW_PREFIX=onedrive
  GH_TOKEN / REPO (写 Secret 用; 默认 tammader/cloudtransfer-gd-out-pub)
"""
import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pathcfg                                     # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.environ.get("REPO", "tammader/cloudtransfer-gd-out-pub")
API = "https://api.github.com"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def h8(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8]


def conf_path():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


def conf_remotes():
    out = []
    try:
        for l in io.open(conf_path(), encoding="utf-8").read().splitlines():
            s = l.strip()
            if s.startswith("[") and s.endswith("]"):
                out.append(s[1:-1])
    except Exception:
        pass
    return out


def storages(env_key):
    raw = os.environ.get(env_key, "")
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except Exception:
        return []
    out = []
    for it in (arr if isinstance(arr, list) else []):
        if isinstance(it, dict):
            out.append((it.get("mount_path") or "", it.get("driver") or ""))
    return out


def is_list_key(v):
    return isinstance(v, str) and "," in v


def is_json_str(v):
    """PATHS_JSON 里有些 key 的值本身是 JSON 字符串(FS_QUOTA/FS_DESC/_WORDS 这类)"""
    if not isinstance(v, str):
        return False
    s = v.strip()
    return (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}"))


def deep_transform(v, a):
    """递归: 字符串叶子按规则改写; 值是 JSON 字符串的 -> 解析-改-再按原形态写回。返回 (新值, 命中数)"""
    if isinstance(v, dict):
        out, n = {}, 0
        for k, x in v.items():
            nv, m = deep_transform(x, a)
            out[k] = nv
            n += m
        return out, n
    if isinstance(v, list):
        out, n = [], 0
        for x in v:
            nv, m = deep_transform(x, a)
            out.append(nv)
            n += m
        return out, n
    if is_json_str(v):
        try:
            inner = json.loads(v)
        except Exception:
            return transform_value(v, a)
        ni, m = deep_transform(inner, a)
        if not m:
            return v, 0
        return json.dumps(ni, ensure_ascii=False), m
    if isinstance(v, str):
        return transform_value(v, a)
    return v, 0


def is_old_ref(s, a):
    """这个字符串是不是指向旧盘(远端名/挂载名两种形态都算)"""
    t = (s or "").strip()
    return t in (a.old_remote, a.old_remote + ":", a.old_mount, a.old_mount.rstrip("/"))


def transform_value(v, a):
    """把一个配置值里的 OneDrive2 引用改到新盘。返回 (新值, 命中数)"""
    if not isinstance(v, str):
        return v, 0
    n = 0
    parts = []
    for one in (v.split(",") if is_list_key(v) else [v]):
        raw = one
        s = one.strip()
        if not s:
            parts.append(one)
            continue
        # rclone 形:  old_remote:dir[/...]
        if s == a.old_remote or s.startswith(a.old_remote + ":"):
            rest = s[len(a.old_remote):].lstrip(":/")
            new = "%s:%s" % (a.new_remote, (a.new_prefix + "/" + rest).strip("/"))
            parts.append(new)
            n += 1
            continue
        # alist 挂载形: /OneDrive2[/dir...]
        if s == a.old_mount or s.startswith(a.old_mount.rstrip("/") + "/"):
            rest = s[len(a.old_mount.rstrip("/")):].strip("/")
            new = "%s/%s" % (a.new_mount.rstrip("/"), (a.new_prefix + "/" + rest).strip("/"))
            parts.append(new)
            n += 1
            continue
        parts.append(raw)
    return (",".join(parts) if is_list_key(v) else parts[0]), n


def shape(v, a, hashed=False):
    """日志用形状: 远端名/挂载名脱敏, 目录名保留(目录名本来就在源码注释里)"""
    if not isinstance(v, str):
        return repr(v)[:80]
    out = []
    for one in v.split(","):
        s = one.strip()
        if ":" in s and not s.startswith("/"):
            r, p = s.split(":", 1)
            rr = "<r:%s>" % h8(r) if hashed else ("<NEW>" if r == a.new_remote else r)
            out.append("%s:%s" % (rr, p))
        elif s.startswith("/"):
            seg = s.strip("/").split("/")
            mp = "/" + seg[0]
            rest = "/".join(seg[1:])
            mm = "<m:%s>" % h8(mp) if hashed else ("<NEWMNT>" if mp == a.new_mount.rstrip("/") else mp)
            out.append(mm + ("/" + rest if rest else ""))
        else:
            out.append(s)
    return ",".join(out)


def gh(method, path, body=None, tok=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "token " + tok, "User-Agent": "od2-migrate",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with OP.open(req, timeout=90) as x:
            t = x.read().decode()
            return x.status, (json.loads(t) if t.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def set_secret(name, value, tok):
    st, d = gh("GET", "/repos/%s/actions/secrets/public-key" % REPO, tok=tok)
    if st != 200:
        print("  !! 取公钥失败 %s %s" % (st, str(d)[:120]))
        return False
    from nacl import encoding, public as npub
    pk = npub.PublicKey(d["key"].encode(), encoding.Base64Encoder())
    enc = base64.b64encode(npub.SealedBox(pk).encrypt(value.encode())).decode()
    st, d = gh("PUT", "/repos/%s/actions/secrets/%s" % (REPO, name),
               {"encrypted_value": enc, "key_id": d["key_id"]}, tok=tok)
    print("  Secret %-16s -> %s (新值 %d 字节)" % (name, st, len(value)))
    return st in (201, 204)


def drop_quota_rows(v, a):
    """FS_QUOTA(可能是 list, 也可能是 JSON 字符串): 把 'a' 指向旧盘的行摘掉"""
    was_str = is_json_str(v)
    try:
        arr = json.loads(v) if was_str else v
    except Exception:
        return v, []
    if not isinstance(arr, list):
        return v, []
    kept, dropped = [], []
    for q in arr:
        if isinstance(q, dict) and is_old_ref(str(q.get("a", "")), a):
            dropped.append(str(q.get("n") or "?"))
            continue
        kept.append(q)
    if not dropped:
        return v, []
    return (json.dumps(kept, ensure_ascii=False) if was_str else kept), dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把新字典写回 Secret")
    ap.add_argument("--hashmap", action="store_true", help="只打印脱敏探针(远端名/挂载名/各 key 形状)")
    a = ap.parse_args()
    a.old_remote = os.environ.get("OLD_REMOTE", "onedrive2")
    a.old_mount = os.environ.get("OLD_MOUNT", "/OneDrive2")
    a.new_remote = os.environ.get("NEW_REMOTE", "gdrive2")
    a.new_mount = os.environ.get("NEW_MOUNT", "/GoogleDrive2")
    a.new_prefix = os.environ.get("NEW_PREFIX", "onedrive")
    tok = os.environ.get("GH_TOKEN", "")

    print("[1] 迁移目标: %s:* -> %s:%s/*   |  alist %s/* -> %s/%s/*"
          % (a.old_remote, a.new_remote, a.new_prefix, a.old_mount, a.new_mount, a.new_prefix))
    remotes = conf_remotes()
    print("CI rclone 远端 %d 个(名字脱敏):" % len(remotes))
    for r in remotes:
        print("   <r:%s>  %s" % (h8(r), "** = OLD_REMOTE" if r == a.old_remote else
                                 ("** = NEW_REMOTE" if r == a.new_remote else "")))
    for envk in ("ALIST_STORAGES_TY", "ALIST_STORAGES"):
        st = storages(envk)
        if not st:
            continue
        print("%s 挂载 %d 个(名字脱敏):" % (envk, len(st)))
        for mp, drv in st:
            print("   <m:%s>  %s%s" % (h8("/" + mp.strip("/")), "** = OLD_MOUNT " if "/" + mp.strip("/") == a.old_mount else "",
                                       "** = NEW_MOUNT" if "/" + mp.strip("/") == a.new_mount.rstrip("/") else ""))

    # ---- 逐 key 检查 ----
    d = dict(pathcfg._load())                      # PATHS_JSON(+本机文件); PATHS_JSON_TD 单独处理
    td_raw = os.environ.get("PATHS_JSON_TD", "")
    td = {}
    if td_raw.strip():
        try:
            td = json.loads(td_raw)
        except Exception as e:
            print("!! PATHS_JSON_TD 解析失败: %s" % e)

    print("\n---- 命中 OneDrive2 的 key ----")
    hits, text_notes = [], []
    for src, dic in (("PATHS_JSON", d), ("PATHS_JSON_TD", td)):
        for k in sorted(dic):
            v = dic[k]
            if k == "_WORDS":
                w = v if isinstance(v, dict) else {}
                if a.old_remote in w or a.old_mount in w:
                    print("   %-22s(_WORDS 里映射了旧盘名: %s)"
                          % (src, ", ".join(x for x in (a.old_remote, a.old_mount) if x in w)))
                    hits.append((src, k, None))
                continue
            if k == "FS_QUOTA":
                arr = json.loads(v) if is_json_str(v) else (v if isinstance(v, list) else [])
                for i, q in enumerate(arr):
                    if not isinstance(q, dict):
                        continue
                    aa = str(q.get("a", ""))
                    print("   FS_QUOTA[%d] k=%-8s a=<r:%s>%s" % (i, q.get("k"), h8(aa),
                                                                 "  ** 旧盘(将摘掉)" if is_old_ref(aa, a) else ""))
                nv, dropped = drop_quota_rows(v, a)
                if dropped:
                    print("   %-22s FS_QUOTA 摘掉 %d 行" % (src, len(dropped)))
                    hits.append((src, k, len(dropped)))
                continue
            nv, n = deep_transform(v, a)
            if n:
                print("   %-22s %-18s %s  ->  %s" % (src, k, shape(v, a), shape(nv, a)))
                hits.append((src, k, n))
            elif isinstance(v, str) and not is_json_str(v) and a.old_remote in v:
                text_notes.append("%s.%s" % (src, k))          # 只是文案里提到旧盘名, 路径没变
    if text_notes:
        print("   (文案里提到旧盘名、路径未变的 key: %s)" % ", ".join(text_notes))
    if not hits:
        print("   (没有命中: 该盘在配置里已经没有引用了)")

    # ---- 生成新字典 ----
    def rebuild(dic):
        out = {}
        for k, v in dic.items():
            if k == "FS_QUOTA":
                nv, _ = drop_quota_rows(v, a)
                out[k] = nv
            elif k == "_WORDS" and isinstance(v, dict):
                w = dict(v)
                # 新目录名在公开日志里也要遮住
                w.setdefault(a.new_prefix, "OD目录")
                out[k] = w
            else:
                out[k], _ = deep_transform(v, a)
        return out

    new_d = rebuild(d)
    new_td = rebuild(td)
    print("\n---- 结果 ----")
    print("PATHS_JSON      key 数 %d -> %d" % (len(d), len(new_d)))
    if td:
        print("PATHS_JSON_TD   key 数 %d -> %d" % (len(td), len(new_td)))

    if not a.apply:
        print("(演练完; 加 --apply 才写回 Secret)")
        return 0
    if not tok:
        print("!! 缺 GH_TOKEN, 无法写 Secret")
        return 1
    ok = set_secret("PATHS_JSON", json.dumps(new_d, ensure_ascii=False), tok)
    if td:
        ok = set_secret("PATHS_JSON_TD", json.dumps(new_td, ensure_ascii=False), tok) and ok
    print("写回: %s" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import time
    sys.exit(main())
