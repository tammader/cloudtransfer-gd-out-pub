# -*- coding: utf-8 -*-
"""给 PATHS_JSON(或 PATHS_JSON_TD) 加/改 key —— 值不出 CI, 日志只打 key 名与长度

为什么在 CI 里改: 盘名/路径真值只存在 Secret 里, 本机没有副本。这个脚本在 runner 上
读同一份 Secret, 合并后写回, 公开仓库的日志里不出现任何值。

用法:
  python3 tools/paths_set.py GY_SRC=gdrive6:out3 GY_DST_DIR=/ydy2/ycl          # 演练
  python3 tools/paths_set.py GY_SRC=gdrive6:out3 --apply                        # 真写
  python3 tools/paths_set.py --delete OLD_KEY --apply
  python3 tools/paths_set.py --dict PATHS_JSON_TD K=V ...                       # 改第二个字典
环境: GH_TOKEN(写 Secret) / REPO(默认 tammader/cloudtransfer-gd-out-pub)
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("REPO", "tammader/cloudtransfer-gd-out-pub")
API = "https://api.github.com"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def gh(method, path, body=None, tok=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "token " + tok, "User-Agent": "paths-set",
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
        print("!! 取公钥失败 %s %s" % (st, str(d)[:120]))
        return False
    from nacl import encoding, public as npub
    pk = npub.PublicKey(d["key"].encode(), encoding.Base64Encoder())
    enc = base64.b64encode(npub.SealedBox(pk).encrypt(value.encode())).decode()
    st, _ = gh("PUT", "/repos/%s/actions/secrets/%s" % (REPO, name),
               {"encrypted_value": enc, "key_id": d["key_id"]}, tok=tok)
    print("Secret %-16s -> %s (新值 %d 字节)" % (name, st, len(value)))
    return st in (201, 204)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="*", help="K=V")
    ap.add_argument("--dict", default="PATHS_JSON", choices=["PATHS_JSON", "PATHS_JSON_TD"])
    ap.add_argument("--delete", default="", help="要删的 key(逗号分隔)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    raw = os.environ.get(a.dict, "")
    if not raw.strip():
        print("!! 环境里没有 %s(这个 workflow 得注入该 Secret)" % a.dict)
        return 1
    d = json.loads(raw)
    print("%s: 现有 %d 个 key" % (a.dict, len(d)))

    changed = []
    for kv in a.pairs:
        if "=" not in kv:
            print("!! 跳过非法参数: %s" % kv)
            continue
        k, v = kv.split("=", 1)
        old = d.get(k)
        d[k] = v
        print("   %-18s %s -> %d 字节" % (k, ("(新加)" if old is None else "旧 %d 字节" % len(str(old))), len(v)))
        changed.append(k)
    for k in [x.strip() for x in a.delete.split(",") if x.strip()]:
        if k in d:
            del d[k]
            print("   %-18s 删除" % k)
            changed.append(k)
        else:
            print("   %-18s (本来就没有)" % k)

    if not changed:
        print("没有要改的 key")
        return 0
    if not a.apply:
        print("(演练: 上面这些改动没写回; 加 --apply 才写)")
        return 0
    tok = os.environ.get("GH_TOKEN", "")
    if not tok:
        print("!! 缺 GH_TOKEN")
        return 1
    ok = set_secret(a.dict, json.dumps(d, ensure_ascii=False), tok)
    print("写回: %s" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
