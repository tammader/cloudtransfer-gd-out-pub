# -*- coding: utf-8 -*-
"""测速: OneDrive2/out2 -> 天翼个人/out2

流程(在 runner 上跑, 用 runner 本地那台 OpenList 的天翼个人挂载做上传):
  1) rclone 从 onedrive2:out2 下载一个文件到本地, 计时
  2) 通过本地 alist 的 PUT /api/fs/put 上传到 /天翼个人/out2, 计时
  3) 校验目标大小 == 本地大小
  4) 默认把测试文件从目标删掉(进回收站), 不污染 out2

用法:
  python3 ty_upload_speedtest.py --file 已处理_xxx.mp4            # 指定文件
  python3 ty_upload_speedtest.py --pick-largest --max-mb 400      # 自动挑
  python3 ty_upload_speedtest.py --keep                          # 传完不删(留在 out2)
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SRC = os.environ.get("TY_SRC", "onedrive2:out2")
DST = os.environ.get("TY_DST", "/天翼个人/out2")
ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
TMP = os.environ.get("TY_TMP", "/tmp/tytest")
RCLONE = os.environ.get("RCLONE_BIN") or (shutil.which("rclone") or "rclone")
CONF = os.environ.get("RCLONE_CONF_PATH") or os.path.expanduser("~/.config/rclone/rclone.conf")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def token():
    t = os.environ.get("ALIST_TOKEN", "").strip()
    if not t:
        raise SystemExit("!! 需要 ALIST_TOKEN")
    return t


def alist(method, path, payload):
    req = urllib.request.Request(ALIST + path, data=json.dumps(payload).encode(), method=method,
                                 headers={"Authorization": token(),
                                          "Content-Type": "application/json"})
    d = json.loads(OP.open(req, timeout=180).read().decode("utf-8", "replace"))
    if d.get("code") != 200:
        raise RuntimeError("alist %s -> %s %s" % (path, d.get("code"), d.get("message")))
    return d.get("data")


def rclone(args, timeout=3600):
    env = dict(os.environ)
    return subprocess.run([RCLONE] + args + ["--config", CONF], capture_output=True,
                          text=True, errors="replace", timeout=timeout, env=env)


def human(b):
    return "%.1f MB" % (b / 1048576) if b < 1024 ** 3 else "%.2f GB" % (b / 1024 ** 3)


class Progress(object):
    """给 http.client 当 body, 边发边算速度"""
    def __init__(self, fh, total, every=10):
        self.fh, self.total, self.n, self.t0, self.last, self.every = fh, total, 0, time.time(), 0.0, every

    def read(self, n=-1):
        b = self.fh.read(n)
        if b:
            self.n += len(b)
            now = time.time()
            if now - self.last >= self.every:
                self.last = now
                el = max(now - self.t0, 1e-6)
                sys.stdout.write("\r    ⬆ %5.1f%%  %s/%s  %.1f MB/s   "
                                 % (100.0 * self.n / max(self.total, 1), human(self.n),
                                    human(self.total), self.n / el / 1048576))
                sys.stdout.flush()
        return b

    @property
    def secs(self):
        return time.time() - self.t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    ap.add_argument("--pick-largest", action="store_true")
    ap.add_argument("--max-mb", type=int, default=400, help="自动挑选时的上限")
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst", default=DST)
    ap.add_argument("--tmp", default=TMP)
    ap.add_argument("--keep", action="store_true", help="传完不删测试文件")
    a = ap.parse_args()
    os.makedirs(a.tmp, exist_ok=True)

    # 1) 挑文件
    r = rclone(["lsjson", a.src, "--files-only"])
    if r.returncode != 0:
        print("!! 列源失败: %s" % (r.stderr or "")[:200])
        return 1
    items = json.loads(r.stdout or "[]")
    if not items:
        print("!! %s 里没有文件" % a.src)
        return 1
    if a.file:
        hit = [x for x in items if x["Name"] == a.file]
        if not hit:
            print("!! 源里没有 %s" % a.file)
            return 1
        pick = hit[0]
    else:
        cand = [x for x in items if int(x["Size"]) <= a.max_mb * 1048576]
        cand = cand or items
        pick = max(cand, key=lambda x: int(x["Size"])) if a.pick_largest else min(cand, key=lambda x: int(x["Size"]))
    name, size = pick["Name"], int(pick["Size"])
    print("测试文件: %s (%s)" % (name, human(size)))
    print("源 %s -> 目标 %s" % (a.src, a.dst))

    # 2) 下载
    lp = os.path.join(a.tmp, name)
    t0 = time.time()
    r = rclone(["copyto", "%s/%s" % (a.src, name), lp, "--retries", "3",
                "--low-level-retries", "10", "--stats", "0"])
    dl_s = time.time() - t0
    if r.returncode != 0 or os.path.getsize(lp) != size:
        print("!! 下载失败/大小不符: %s" % (r.stderr or "")[:150])
        return 1
    dl_sp = size / max(dl_s, 1e-6) / 1048576
    print("⬇ 下载完成: %s 用时 %.1fs -> %.1f MB/s" % (human(size), dl_s, dl_sp))

    # 3) 上传
    dst_path = a.dst.rstrip("/") + "/" + name
    with open(lp, "rb") as fh:
        body = Progress(fh, size)
        req = urllib.request.Request(ALIST + "/api/fs/put", data=body, method="PUT",
                                     headers={"Authorization": token(),
                                              "File-Path": urllib.parse.quote(dst_path),
                                              "Content-Type": "application/octet-stream",
                                              "Content-Length": str(size)})
        up_s = None
        try:
            d = json.loads(OP.open(req, timeout=7200).read().decode("utf-8", "replace"))
        except Exception as e:
            print("\n!! 上传异常: %s" % str(e)[:200])
            return 1
        up_s = body.secs
    sys.stdout.write("\n")
    if d.get("code") != 200:
        print("!! 上传失败: %s %s" % (d.get("code"), d.get("message")))
        return 1
    up_sp = size / max(up_s, 1e-6) / 1048576
    print("⬆ 上传完成: %s 用时 %.1fs -> %.1f MB/s" % (human(size), up_s, up_sp))

    # 4) 校验
    got = alist("POST", "/api/fs/get", {"path": dst_path}) or {}
    ok = int(got.get("size") or 0) == size
    print("校验 %s: 目标大小 %s (期望 %s) %s"
          % (dst_path, human(int(got.get("size") or 0)), human(size), "OK" if ok else "!! 不一致"))

    # 5) 清理测试文件(进回收站)
    cleaned = ""
    if not a.keep:
        try:
            alist("POST", "/api/fs/remove", {"dir": a.dst.rstrip("/"), "names": [name]})
        except Exception as e:
            cleaned = "删除请求失败: %s" % str(e)[:80]
        # 校验: 取不到就算删干净(500 object not found 也是"已不在")
        try:
            left = alist("POST", "/api/fs/get", {"path": dst_path})
            cleaned = cleaned or ("删除后仍在?" if left else "已删除(回收站)")
        except Exception:
            cleaned = cleaned or "已删除(回收站)"
        print("清理: %s" % cleaned)
    else:
        print("清理: 未删(--keep), 测试文件留在 %s" % a.dst)

    print("\n===== 测速结果 =====")
    print("文件大小        : %s" % human(size))
    print("下载(OneDrive2) : %.1f MB/s  (%.1fs)" % (dl_sp, dl_s))
    print("上传(天翼个人)  : %.1f MB/s  (%.1fs)" % (up_sp, up_s))
    print("端到端          : %.1f MB/s  (%.1fs)" % (size / max(dl_s + up_s, 1e-6) / 1048576, dl_s + up_s))
    print("校验/清理       : %s / %s" % ("OK" if ok else "失败", cleaned or "n/a"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
