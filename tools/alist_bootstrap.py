# -*- coding: utf-8 -*-
"""在 CI 里给跑起来的 alist/OpenList 灌存储配置, 并取一个管理 token

背景: /归档挂载 是本机 alist 上的 Crypt 盘(底层 上游网盘), GitHub runner 连不到本机,
      所以改成"在 runner 上装一份 OpenList, 用它挂同样的上游网盘 + Crypt, 再上传"。
      存储定义(含 cookie/密码/salt)从本机 alist 的 x_storages 导出, 通过 Secret 带过去。

用法(在 alist 数据目录初始化之后跑):
  python3 alist_bootstrap.py --data-dir ./data --password xxx --storages-file storages.json
      [--emit-env]        # 把 ALIST_TOKEN 写进 $GITHUB_ENV
      [--force-db]        # 直接写 sqlite(默认先试管理 API, 失败再写库)

注意: 直接写 sqlite 要趁 alist 没跑(或写完重启), 否则缓存不刷新。
"""
import argparse
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
URL = DEFAULT_URL
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(method, path, body=None, token="", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = token
    req = urllib.request.Request(URL + path, data=data, method=method, headers=h)
    with OP.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def wait_up(secs=60):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            api("GET", "/api/public/settings", timeout=10)
            return True
        except Exception:
            time.sleep(2)
    return False


def login(username, password):
    d = api("POST", "/api/auth/login", {"username": username, "password": password})
    if d.get("code") != 200:
        raise SystemExit("!! 登录失败: %s" % d.get("message"))
    token = d["data"]["token"]
    # 安全: 立刻让 GitHub Actions 把后续日志里出现的这串值替换成 ***
    # (仓库现已公开, 日志任何人可读 -> 绝不能把 token 明文留在日志里)
    print("::add-mask::%s" % token)
    return token


def add_via_api(st, token):
    """先试管理 API: 存储已存在就先删掉再建"""
    body = {k: st.get(k) for k in (
        "mount_path", "order", "driver", "cache_expiration", "status", "addition",
        "remark", "order_by", "order_direction", "extract_folder",
        "webdav_policy", "down_proxy_url")}
    # v4 这几个字段是严格 bool, 传 0/1 会被拒 ("ReadBool: expect t or f, but found 0")
    body["web_proxy"] = bool(st.get("web_proxy"))
    body["proxy_range"] = bool(st.get("proxy_range"))
    if not body.get("webdav_policy"):
        body.pop("webdav_policy")
    body["addition"] = st["addition"] if isinstance(st["addition"], str) else \
        json.dumps(st["addition"], ensure_ascii=False)
    body["status"] = "work"
    lst = api("GET", "/api/admin/storage/list", token=token)
    for x in (lst.get("data") or {}).get("content") or []:
        if x.get("mount_path") == st["mount_path"]:
            api("POST", "/api/admin/storage/delete", {"id": x["id"]}, token=token)
            print("   删掉旧存储 id=%s %s" % (x["id"], x["mount_path"]))
    r = api("POST", "/api/admin/storage/create", body, token=token)
    if r.get("code") != 200:
        raise RuntimeError("create 失败: %s" % r.get("message"))
    return True


def add_via_db(db, st):
    """直接写 sqlite: 只填两边都有的列(order 是保留字, 列名必须加双引号)"""
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(x_storages)")]
    conn.execute("delete from x_storages where mount_path=?", (st["mount_path"],))
    use = {k: v for k, v in st.items() if k in cols and k != "id"}
    if "addition" in use and not isinstance(use["addition"], str):
        use["addition"] = json.dumps(use["addition"], ensure_ascii=False)
    use["status"] = "work"
    use["disabled"] = 0
    use["web_proxy"] = 1 if st.get("web_proxy") else 0
    use["proxy_range"] = 1 if st.get("proxy_range") else 0
    ks = list(use.keys())
    conn.execute('insert into x_storages (%s) values (%s)'
                 % (",".join('"%s"' % k for k in ks), ",".join("?" * len(ks))),
                 [use[k] for k in ks])
    conn.commit()
    conn.close()
    return ks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="./data")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--storages-file", default="storages.json")
    ap.add_argument("--emit-env", action="store_true")
    ap.add_argument("--force-db", action="store_true")
    ap.add_argument("--login-only", action="store_true", help="只登录拿 token, 不碰存储")
    ap.add_argument("--verify-mount", default="", help="验证用的挂载点; 留空=自动取第一个存储的挂载点(不把路径写进命令行/日志)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--override", nargs="*", default=[],
                    help="覆盖 addition 字段, 如 upload_thread=8 (可多个)")
    a = ap.parse_args()
    global URL
    URL = a.url

    over = {}
    for kv in a.override:
        k, _, v = kv.partition("=")
        if k:
            over[k.strip()] = v.strip()

    if a.login_only:
        if not wait_up(90):
            raise SystemExit("!! alist 没起来")
        token = login(a.user, a.password)
        if a.emit_env:
            gh = os.environ.get("GITHUB_ENV")
            if gh:
                with io.open(gh, "a", encoding="utf-8") as f:
                    f.write("ALIST_TOKEN=%s\n" % token)
                print("已写入 GITHUB_ENV: ALIST_TOKEN")
                return 0
        print("ALIST_TOKEN=%s...(已打码, 共 %d 字符)" % (token[:6], len(token)))
        return 0

    storages = json.loads(io.open(a.storages_file, encoding="utf-8").read())
    if isinstance(storages, dict):
        storages = storages.get("storages") or []
    if over:
        for st in storages:                       # 把 addition 统一成 dict 再覆盖
            if isinstance(st.get("addition"), str):
                st["addition"] = json.loads(st["addition"])
            st.setdefault("addition", {}).update(over)
        print("覆盖 addition 字段: %s" % over)
    print("待灌入 %d 个存储: %s" % (len(storages), [s["mount_path"] for s in storages]))

    token = ""
    if not a.force_db and wait_up(60):
        try:
            token = login(a.user, a.password)
            print("管理 API 可用, 逐个建存储")
            for st in storages:
                add_via_api(st, token)
                print("   OK %s (%s)" % (st["mount_path"], st["driver"]))
        except Exception as e:
            print("  !! API 建存储失败: %s -> 改用直接写库" % str(e)[:120])
            token = ""

    if not token:
        db = os.path.join(a.data_dir, "data.db")
        if not os.path.exists(db):
            raise SystemExit("!! %s 不存在(先跑 openlist admin set)" % db)
        print("直接写 sqlite: %s" % db)
        for st in storages:
            ks = add_via_db(db, st)
            print("   OK %s (%s) 列 %s" % (st["mount_path"], st["driver"], ks))
        return 0                      # 改了库需要重启 alist, 由调用方处理

    for st in storages:
        try:
            d = api("POST", "/api/fs/list",
                    {"path": st["mount_path"], "page": 1, "per_page": 0, "refresh": True})
            n = len((d.get("data") or {}).get("content") or [])
            print("   挂载点 %s: %d 个条目 %s"
                  % (st["mount_path"], n, "OK" if d.get("code") == 200 else d.get("message")))
        except Exception as e:
            print("   !! 列 %s 失败: %s" % (st["mount_path"], str(e)[:150]))

    # 验证挂载点: 路径不写死(日志会公开), 留空则取第一个存储的挂载点; 只报数量不报条目名
    vm = a.verify_mount or (storages[0].get("mount_path", "") if storages else "")
    if vm:
        try:
            d = api("POST", "/api/fs/list",
                    {"path": vm, "page": 1, "per_page": 0, "refresh": True})
            got = (d.get("data") or {}).get("content") or []
            print("[验证] 挂载点 -> %d 个条目 (code=%s)" % (len(got), d.get("code")))
        except Exception as e:
            print("[验证] 列目录失败: %s" % str(e)[:120])

    if a.emit_env and token:
        gh = os.environ.get("GITHUB_ENV")
        if gh:
            with io.open(gh, "a", encoding="utf-8") as f:
                f.write("ALIST_TOKEN=%s\n" % token)
            print("已写入 GITHUB_ENV: ALIST_TOKEN")
        else:
            print("ALIST_TOKEN=%s...(已打码, 共 %d 字符)" % (token[:6], len(token)))
    print("完成")
    return 0


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
