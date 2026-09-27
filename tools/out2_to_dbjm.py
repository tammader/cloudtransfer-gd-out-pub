# -*- coding: utf-8 -*-
"""中转远端/out2  ->  alist /归档挂载 根目录      单向同步 + 到位后删源

语义:
  - 只下载源里有、目标里没有的;
  - 目标已有同名同大小 -> 跳过上传, 这就是"避免重复上传"的依据;
  - 同名但大小不同 -> 记为冲突, 默认不覆盖(要覆盖加 --overwrite);
  - **目标里多出来的文件不删**(只报告), 所以目标不会被"反噬";
  - **源文件删除(2026-09-25 加)**: 只要目标 /归档挂载 里确认有**同名且同大小**的文件
    (刚上传校验通过的, 或本来就在的), 就把 中转远端/out2 里的源文件删掉。
    只有 name+size 都一致才会删; 对不上(大小不同/目标没有/目标是目录)一律不删。
    中转远端 侧删除走 rclone deletefile -> 进回收站(可捞), 且原文件在 源远端:out2 归档区
    一直都有, 所以即使误删也能拿回来。想保留源文件加 --keep-src。

为什么必须在本机跑: /归档挂载 是本机 alist 上的 Crypt 加密盘(底层 /上游网盘/jm),
GitHub runner 访问不到它(跟 目标网盘/out2 一样只挂在本地)。

上传走 alist 原生接口 PUT /api/fs/put (File-Path 头 + 原始 body), 下载走 rclone。

用法:
  python out2_to_dbjm.py                      # 演练(只看计划)
  python out2_to_dbjm.py --apply              # 真跑(上传 + 校验 + 删源)
  python out2_to_dbjm.py --apply --keep-src   # 真跑但保留源文件
  python out2_to_dbjm.py --apply --only xxx.mp4
"""
import argparse
import configparser
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg          # 路径真值来自配置: CI=Secret PATHS_JSON, 本机=paths.local.json
SRC = pathcfg.require("DBJM_SRC")
DST = pathcfg.require("DBJM_DST")                     # alist 里的挂载路径(根)
ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
# 临时目录: 注意别放 C:/cb 下面, 会被本机 worker 当成待处理文件
TMP = os.environ.get("DBJM_TMP") or ("/tmp/od2dbjm" if os.name != "nt" else r"C:/od2dbjm")
LOGDIR = os.path.join(HERE, "logs")
STATE = os.path.join(HERE, "state")
LOG = os.path.join(LOGDIR, "dbjm_sync.log")
REPORT = os.path.join(LOGDIR, "dbjm_sync_report.txt")
REPORT_REMOTE = pathcfg.require("DBJM_REPORT")
FAILED = os.path.join(STATE, "dbjm_failed.json")
MAX_FAIL = 3                     # 同一文件连续失败这么多次就不再重试
RCLONE = os.environ.get("RCLONE_BIN") or \
    (r"C:/Users/Administrator.DESKTOP-78D2CT4/Tools/rclone/rclone.exe"
     if os.name == "nt" else (shutil.which("rclone") or "rclone"))


def _find_conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


RCLONE_CONF = _find_conf()

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本地 alist: 绕开系统代理


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def human(b):
    return "%.0f MB" % (b / 1048576) if b < 1024 ** 3 else "%.2f GB" % (b / 1024 ** 3)


# ---------------- alist ----------------
def alist_token():
    """优先用环境变量(CI 里由 alist_bootstrap.py 生成的 ALIST_TOKEN), 否则读本机 rclone.conf"""
    t = os.environ.get("ALIST_TOKEN", "").strip()
    if t:
        return t if t.lower().startswith("bearer ") else t
    c = configparser.ConfigParser()
    c.read(RCLONE_CONF, encoding="utf-8")
    if not c.has_section("alist"):
        raise SystemExit("!! 既没有 ALIST_TOKEN, rclone.conf 里也没有 [alist] 段")
    return c["alist"].get("bearer_token", "")


TOKEN = alist_token()


def alist_post(path, payload):
    req = urllib.request.Request(ALIST + path, data=json.dumps(payload).encode(),
                                 headers={"Authorization": TOKEN,
                                          "Content-Type": "application/json"})
    d = json.loads(OP.open(req, timeout=180).read().decode("utf-8", "replace"))
    if d.get("code") != 200:
        raise RuntimeError("alist %s -> %s %s" % (path, d.get("code"), d.get("message")))
    return d.get("data")


def alist_list(path, refresh=True):
    d = alist_post("/api/fs/list", {"path": path, "page": 1, "per_page": 0,
                                    "refresh": refresh})
    out = {}
    for x in (d or {}).get("content") or []:
        out[x["name"]] = (int(x.get("size") or 0), bool(x.get("is_dir")))
    return out


def alist_get(path, refresh=True):
    """单个文件信息(不存在返回 None)。refresh=True 强制穿透缓存, 删源前必须用"""
    try:
        d = alist_post("/api/fs/get", {"path": path, "refresh": refresh})
    except Exception:
        return None
    if not d:
        return None
    return int(d.get("size") or 0), bool(d.get("is_dir"))


class _Progress(object):
    """给 http.client 当 body 用: 边发边报进度"""
    def __init__(self, fh, total, label):
        self.fh, self.total, self.label = fh, total, label
        self.n = 0
        self.t0 = time.time()
        self.last = 0.0

    def read(self, n=-1):
        b = self.fh.read(n)
        if b:
            self.n += len(b)
            now = time.time()
            if now - self.last >= 8:
                self.last = now
                el = max(now - self.t0, 0.001)
                sys.stdout.write("\r    ⬆ %s  %5.1f%%  %s/%s  %.1f MB/s   "
                                 % (self.label, 100.0 * self.n / max(self.total, 1),
                                    human(self.n), human(self.total),
                                    self.n / el / 1048576))
                sys.stdout.flush()
        return b


def alist_put(remote_path, local_file, size):
    url_path = urllib.parse.quote(remote_path)
    with open(local_file, "rb") as fh:
        body = _Progress(fh, size, os.path.basename(remote_path)[:28])
        req = urllib.request.Request(ALIST + "/api/fs/put", data=body, method="PUT",
                                     headers={"Authorization": TOKEN,
                                              "File-Path": url_path,
                                              "Content-Type": "application/octet-stream",
                                              "Content-Length": str(size)})
        d = json.loads(OP.open(req, timeout=3600).read().decode("utf-8", "replace"))
    sys.stdout.write("\n")
    if d.get("code") != 200:
        raise RuntimeError("上传失败: %s %s" % (d.get("code"), d.get("message")))
    return True


# ---------------- rclone ----------------
def rclone(args, timeout=7200, quiet_proxy=True):
    env = dict(os.environ)
    if quiet_proxy:                    # 跟 run_server.bat 一样: 本机 rclone 不走代理
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env.pop(k, None)
    return subprocess.run([RCLONE] + args + ["--config", RCLONE_CONF],
                          capture_output=True, text=True, errors="replace",
                          timeout=timeout, env=env)


def lsjson(remote):
    r = rclone(["lsjson", remote, "--files-only"])
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:160]))
    return {x["Name"]: int(x.get("Size") or 0) for x in json.loads(r.stdout or "[]")}


# ---------------- 到位后删源 ----------------
def _src_delete(remote, name):
    """删源文件(中转远端: 进回收站)。返回 (ok, msg)"""
    r = rclone(["deletefile", "%s/%s" % (remote, name),
                "--retries", "3", "--low-level-retries", "10"], timeout=900)
    if r.returncode != 0:
        return False, "deletefile rc=%d %s" % (r.returncode, (r.stderr or "")[:140])
    chk = rclone(["lsjson", "--stat", "%s/%s" % (remote, name)], timeout=600)
    if chk.returncode == 0:
        return False, "删除命令返回成功但源文件仍能读到"
    return True, ""


def del_src_if_present(src, name, dst_path, size):
    """目标侧再确认一次"同名同大小"后删源。
    返回 (deleted:int, note:str)。任何一项对不上都不删。"""
    got = alist_get(dst_path, refresh=True)          # 穿透缓存重新读, 避免拿陈旧列表做删除决策
    if not got:
        return 0, "目标侧复查不到该文件, 不删源"
    if got[1]:
        return 0, "目标侧同名的是目录, 不删源"
    if got[0] != size:
        return 0, "目标侧大小 %s ≠ 源 %s, 不删源" % (human(got[0]), human(size))
    ok, msg = _src_delete(src, name)
    return (1, "源已删除(进回收站)") if ok else (0, "删源失败: " + msg)


def load_json(p, d):
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


def save_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真动; 不加只演练")
    ap.add_argument("--max-ops", type=int, default=20, help="本轮最多上传几个")
    ap.add_argument("--budget-min", type=int, default=240, help="本轮时间预算(分钟)")
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst", default=DST)
    ap.add_argument("--tmp", default=TMP)
    ap.add_argument("--overwrite", action="store_true", help="同名不同大小时覆盖目标")
    ap.add_argument("--keep-src", action="store_true",
                    help="不删源文件(默认: 目标确认同名同大小后删掉 中转远端/out2 里的源)")
    ap.add_argument("--max-delete", type=int, default=200,
                    help="本轮最多为多少个【本来就在目标】的文件删源(无需重新上传的那批)")
    ap.add_argument("--only", default="", help="只同步这一个文件名(调试用)")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(a.tmp, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    fails = load_json(FAILED, {})

    lines = []
    lines.append("# out2 -> %s 单向同步报告  %s  [%s]"
                 % (a.dst, time.strftime("%Y-%m-%d %H:%M:%S"),
                    "执行模式" if a.apply else "演练模式(dry-run)"))
    log("源 %s | 目标 alist:%s | 临时 %s | 模式 %s"
        % (a.src, a.dst, a.tmp, "执行" if a.apply else "演练"))

    # 1) 两边清单
    try:
        src = lsjson(a.src)
    except Exception as e:
        log("!! 读源失败: %s" % str(e)[:180])
        lines.append("!! 读源失败: %s" % str(e)[:180])
        finish(lines, a)
        return 1
    try:
        dst = alist_list(a.dst)
    except Exception as e:
        log("!! 读目标失败(/归档挂载 挂载是否正常?): %s" % str(e)[:180])
        lines.append("!! 读目标失败: %s" % str(e)[:180])
        finish(lines, a)
        return 1

    todo, already, conflict, deeper = [], [], [], []
    for n, s in sorted(src.items(), key=lambda kv: kv[1]):
        if a.only and n != a.only:
            continue
        if n in dst:
            ds, isdir = dst[n]
            if isdir:
                deeper.append((n, s))
            elif ds == s:
                already.append((n, s))
            else:
                conflict.append((n, s, ds))
        else:
            todo.append((n, s))

    log("源 %d 个 | 目标 %d 个 | 已在目标 %d | 待同步 %d | 冲突 %d | 目标是目录 %d"
        % (len(src), len(dst), len(already), len(todo), len(conflict), len(deeper)))
    lines.append("源 %s: %d 个文件 | 目标 %s: %d 个文件"
                 % (a.src, len(src), a.dst, len(dst)))
    lines.append("已在目标(跳过) %d | 待同步 %d | 同名不同大小 %d | 目标是同名目录 %d"
                 % (len(already), len(todo), len(conflict), len(deeper)))
    only_dst = [n for n in dst if n not in src]
    if only_dst:
        lines.append("目标里多出来、本轮不动的 %d 个: %s"
                     % (len(only_dst), ", ".join(only_dst[:6])))
    for n, s, ds in conflict[:10]:
        lines.append("  ! 冲突(不覆盖): %s 源 %s / 目标 %s" % (n, human(s), human(ds)))

    # 1.5) 本来就在目标里(同名同大小) -> 不用重传, 直接删源
    do_del = not a.keep_src
    n_del_already = n_del_new = n_del_fail = 0
    if already:
        lines.append("---")
        if not do_del:
            mode = "保留源(--keep-src)"
        else:
            mode = "演练: 将删源" if not a.apply else "真跑: 删源"
        lines.append("已在目标(同名同大小) %d 个 | %s | 本轮最多处理 %d 个"
                     % (len(already), mode, a.max_delete))
        for n, s in already[:a.max_delete]:
            if time.time() - t0 > a.budget_min * 60:
                lines.append("⏱ 到时间预算, 剩余留到下次")
                break
            if not do_del:
                lines.append("  [%s] %s | 源保留" % (human(s), n))
                continue
            if not a.apply:
                lines.append("  [%s] %s | (演练) 将删除源" % (human(s), n))
                continue
            c, msg = del_src_if_present(a.src, n, a.dst.rstrip("/") + "/" + n, s)
            if c:
                n_del_already += 1
                log("  🗑 %s | %s" % (n, msg))
                lines.append("  [%s] %s | 已删源" % (human(s), n))
            else:
                n_del_fail += 1
                log("  !! %s | %s" % (n, msg))
                lines.append("  [%s] %s | !! %s" % (human(s), n, msg))

    # 2) 逐个: 下载 -> 上传 -> 校验(-> 删源)
    ok_n = fail_n = 0
    plan = todo[:a.max_ops]
    if a.overwrite:
        plan += [(n, s) for n, s, _ in conflict][:max(0, a.max_ops - len(plan))]
    lines.append("---")
    for i, (name, size) in enumerate(plan, 1):
        if time.time() - t0 > a.budget_min * 60:
            lines.append("⏱ 到时间预算, 剩余留到下次")
            log("⏱ 到时间预算, 收工")
            break
        if fails.get(name, {}).get("count", 0) >= MAX_FAIL:
            lines.append("%s | 已连续失败 %d 次, 跳过" % (name, fails[name]["count"]))
            log("⏭ %s 连续失败 %d 次, 跳过" % (name, fails[name]["count"]))
            continue
        head = "[%d/%d][%s] %s" % (i, len(plan), human(size), name)
        if not a.apply:
            lines.append("%s | (演练) 将下载并上传到 %s%s"
                         % (head, a.dst,
                            (", 校验通过后删除源文件" if do_del else " (--keep-src: 保留源)")))
            continue
        lp = os.path.join(a.tmp, name)
        try:
            log("%s ⬇ 下载" % head)
            r = rclone(["copyto", "%s/%s" % (a.src, name), lp,
                        "--retries", "3", "--low-level-retries", "10",
                        "--stats", "20s", "--stats-one-line"])
            if r.returncode != 0 or os.path.getsize(lp) != size:
                got = os.path.getsize(lp) if os.path.exists(lp) else 0
                raise RuntimeError("下载失败/大小不符 得 %s 应 %s :: %s"
                                   % (human(got), human(size), (r.stderr or "")[:120]))
            log("%s ⬆ 上传 -> %s%s" % (head, a.dst, name))
            alist_put(a.dst.rstrip("/") + "/" + name, lp, size)
            got = alist_get(a.dst.rstrip("/") + "/" + name)
            if not got or got[0] != size:
                raise RuntimeError("上传后校验不过: 目标大小 %s 期望 %s"
                                   % (human(got[0]) if got else "无", human(size)))
            ok_n += 1
            fails.pop(name, None)
            log("%s ✅ 完成 (%s)" % (head, human(size)))
            lines.append("%s | ✅ 已上传并校验 (%s)" % (head, human(size)))
            if do_del:
                c, msg = del_src_if_present(a.src, name, a.dst.rstrip("/") + "/" + name, size)
                if c:
                    n_del_new += 1
                    log("%s 🗑 %s" % (head, msg))
                    lines.append("%s | 🗑 %s" % (head, msg))
                else:
                    n_del_fail += 1
                    log("%s !! 删源未成: %s" % (head, msg))
                    lines.append("%s | !! 删源未成: %s (源文件保留, 下轮重试)" % (head, msg))
        except Exception as e:
            fail_n += 1
            rec = fails.get(name, {"count": 0})
            rec.update({"count": rec.get("count", 0) + 1, "note": str(e)[:200],
                        "size_mb": round(size / 1048576, 1),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            fails[name] = rec
            log("%s ❌ %s" % (head, str(e)[:180]))
            lines.append("%s | ❌ %s" % (head, str(e)[:180]))
        finally:
            try:
                if os.path.exists(lp):
                    os.remove(lp)
            except OSError:
                pass

    save_json(FAILED, {k: v for k, v in fails.items() if v.get("count")})
    if not a.apply:
        lines.append("(演练模式: 未下载、未上传、未删除; 加 --apply 生效)")

    lines.append("---")
    lines.append("本轮: 上传成功 %d | 失败 %d | 已在目标 %d | 冲突 %d | 目标多出(不动) %d"
                 % (ok_n, fail_n, len(already), len(conflict), len(only_dst)))
    lines.append("删源: 新上传的 %d + 本来就在目标的 %d = %d 个 | 删源失败 %d %s"
                 % (n_del_new, n_del_already, n_del_new + n_del_already, n_del_fail,
                    "(--keep-src: 源文件全部保留)" if a.keep_src else ""))
    lines.append("用时 %.1f 分钟" % ((time.time() - t0) / 60))
    log("合计: 上传 %d | 失败 %d | 删源 %d (新 %d / 已在 %d) | 删源失败 %d"
        % (ok_n, fail_n, n_del_new + n_del_already, n_del_new, n_del_already, n_del_fail))
    finish(lines, a)
    return 0


def finish(lines, a):
    text = "\n".join(lines) + "\n"
    try:
        with io.open(REPORT, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log("!! 报告写入失败: %s" % str(e)[:80])
    try:
        r = rclone(["copyto", REPORT, REPORT_REMOTE, "--retries", "2"], timeout=600)
        log("报告 -> %s: %s" % (REPORT_REMOTE,
                               "OK" if r.returncode == 0 else (r.stderr or "")[:80]))
    except Exception as e:
        log("报告上传异常(不影响): %s" % str(e)[:80])


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
