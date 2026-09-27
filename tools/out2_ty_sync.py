# -*- coding: utf-8 -*-
"""源远端:out2 -> 目标网盘/out2 单向同步 + 两处齐全后归档到 源远端:out3

规则:
  1) 单向同步: 把 源远端:out2 里的文件传到 目标网盘/out2 (同名同大小则跳过, 源文件不动)
  2) 完成判定: 同一个文件在 **目标网盘/out2** 和 **/归档挂载** 都齐了, 就把 源远端:out2 里的它
     `moveto` 到 **源远端:out3** (同盘服务端移动, 秒完成) —— 等于"这条文件走完了全流程"
  3) dbjm 里是 gd-out2 切过的分片, 所以判定要分两种:
       <= 阈值(默认300MB): dbjm 里有同名同大小
       >  阈值          : dbjm 里有 <名字去扩展>.part001.<ext> 和 .part002.<ext> 两片
     (若 dbjm 里直接有同名同大小, 也算齐 —— 兼容老数据)

跑法: runner 上现装 OpenList 挂「目标网盘」, 上传走 /api/fs/put; 源远端 走 rclone。
默认演练(--dry); --apply 才真动。报告 中转远端:报告区/out2_ty_report.txt
"""
import argparse
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg          # 路径真值来自配置: CI=Secret PATHS_JSON, 本机=paths.local.json
SRC = pathcfg.require("OT_SRC")
TY_DIR = pathcfg.require("OT_TY")
DBJM_DIR = pathcfg.require("OT_DBJM")
ARCH = pathcfg.require("OT_ARCH")
ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
WORK = os.environ.get("OT_WORK", "/tmp/out2ty")
REPORT = pathcfg.require("OT_REPORT")
DAILY = pathcfg.require("OT_DAILY")      # 云盘日上传配额账本(防超 200GB/日)
GB = 1024 ** 3
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def human(b):
    return "%.1f MB" % (b / 1048576) if b < GB else "%.2f GB" % (b / GB)


def alist_token():
    t = os.environ.get("ALIST_TOKEN", "").strip()
    if not t:
        raise SystemExit("!! 需要 ALIST_TOKEN")
    return t


def alist(method, path, payload, timeout=180):
    req = urllib.request.Request(ALIST + path, data=json.dumps(payload).encode(), method=method,
                                 headers={"Authorization": alist_token(),
                                          "Content-Type": "application/json"})
    d = json.loads(OP.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    if d.get("code") != 200:
        raise RuntimeError("alist %s -> %s %s" % (path, d.get("code"), d.get("message")))
    return d.get("data")


def alist_list(path):
    d = alist("POST", "/api/fs/list", {"path": path, "page": 1, "per_page": 0, "refresh": True})
    out = {}
    for x in (d or {}).get("content") or []:
        out[x["name"]] = (int(x.get("size") or 0), bool(x.get("is_dir")))
    return out


def alist_get(path):
    try:
        d = alist("POST", "/api/fs/get", {"path": path})
    except Exception:
        return None
    return int((d or {}).get("size") or 0)


def rclone(args, timeout=1500):
    env = dict(os.environ)
    return subprocess.run([RCLONE] + args + ["--config", CONF], capture_output=True,
                          text=True, errors="replace", timeout=timeout, env=env)


def _find_conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = _find_conf()


def lsjson(remote, timeout=1800):
    r = rclone(["lsjson", remote, "--files-only"], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:160]))
    return {x["Name"]: int(x.get("Size") or 0) for x in json.loads(r.stdout or "[]")}


STALL_SECS = 300          # 连续多久没有任何字节推进 -> 判定停滞, 主动断开(原来只能等 socket 超时 3600s)
PUT_MAX_SECS = 1800       # 单个文件上传总时长上限(慢但有进展的也不许无限拖)


class Progress(object):
    def __init__(self, fh, total, label, every=10):
        self.fh, self.total, self.label = fh, total, label
        self.n, self.t0, self.last, self.every = 0, time.time(), 0.0, every
        self.last_byte = time.time()

    def read(self, n=-1):
        b = self.fh.read(n)
        now = time.time()
        if b:
            self.n += len(b)
            self.last_byte = now
            if now - self.last >= self.every:
                self.last = now
                sys.stdout.write("\r    ⬆ %s %5.1f%%  %s/%s  %.1f MB/s   "
                                 % (self.label, 100.0 * self.n / max(self.total, 1), human(self.n),
                                    human(self.total), self.n / max(now - self.t0, 1e-6) / 1048576))
                sys.stdout.flush()
        # 停滞/超时硬闸(raise 会让 http 层直接中断本次 PUT, 不用等 socket 超时)
        if self.n < self.total and now - self.last_byte > STALL_SECS:
            raise RuntimeError("上传停滞 %.0f 分钟无进展(已传 %s / %s)"
                               % ((now - self.last_byte) / 60, human(self.n), human(self.total)))
        if now - self.t0 > PUT_MAX_SECS:
            raise RuntimeError("上传超过 %.0f 分钟上限(已传 %s / %s)"
                               % (PUT_MAX_SECS / 60.0, human(self.n), human(self.total)))
        return b

    @property
    def secs(self):
        return time.time() - self.t0


def alist_put(dst_path, local_file, size, label):
    with open(local_file, "rb") as fh:
        body = Progress(fh, size, label)
        req = urllib.request.Request(ALIST + "/api/fs/put", data=body, method="PUT",
                                     headers={"Authorization": alist_token(),
                                              "File-Path": urllib.parse.quote(dst_path),
                                              "Content-Type": "application/octet-stream",
                                              "Content-Length": str(size)})
        # socket 超时压到 STALL_SECS: 服务器收包卡住时 5 分钟就断开, 不再干等 1 小时
        d = json.loads(OP.open(req, timeout=STALL_SECS + 60).read().decode("utf-8", "replace"))
    sys.stdout.write("\n")
    if d.get("code") != 200:
        raise RuntimeError("上传失败: %s %s" % (d.get("code"), d.get("message")))
    return body.secs


def split_parts(name, thresh):
    """>阈值时 gd-out2 切出来的两个分片名"""
    stem, ext = os.path.splitext(name)
    return stem + ".part001" + ext, stem + ".part002" + ext


def dbjm_done(name, size, dbjm, thresh):
    """dbjm 里算不算齐了"""
    hit = dbjm.get(name)
    if hit and not hit[1] and hit[0] == size:
        return True
    if size > thresh:
        a, b = split_parts(name, thresh)
        x, y = dbjm.get(a), dbjm.get(b)
        return bool(x and y and not x[1] and not y[1])
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-ops", type=int, default=60, help="本轮最多处理几个文件")
    ap.add_argument("--max-total-gb", type=float, default=6.0, help="本轮上传总量上限(GB)")
    ap.add_argument("--thresh-mb", type=int, default=300, help="gd-out2 的切分阈值(判定分片用)")
    ap.add_argument("--budget-min", type=int, default=170)
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-local", action="store_true")
    ap.add_argument("--daily-limit-gb", type=float, default=180.0,
                    help="云盘当日上传总量上限(GB); 按北京自然日累计, 默认 180(留 20GB 余量)")
    a = ap.parse_args()
    thresh = a.thresh_mb * 1024 ** 2
    max_total = int(a.max_total_gb * GB)

    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)

    lines = ["# 源远端:out2 -> 目标网盘/out2 同步 + 归档 源远端:out3 报告  %s  [%s]"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练")]

    # --- 云盘日配额保护: 该网盘按北京自然日重置, 这里累计封顶, 免得撞日配额 ---
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    d_date, d_gb = load_daily()
    if d_date != today:
        d_date, d_gb = today, 0.0
    if d_gb >= a.daily_limit_gb:
        lines.append("今日已传 %.1fGB >= 日上限 %.1fGB -> 本轮不传, 等次日 0 点重置"
                     % (d_gb, a.daily_limit_gb))
        print(lines[-1])
        finish(lines, a)
        return 0
    eff_gb = min(a.max_total_gb, a.daily_limit_gb - d_gb)
    max_total = int(eff_gb * GB)
    lines.append("日配额: 今日已用 %.1fGB / 上限 %.1fGB -> 本轮最多再传 %.1fGB"
                 % (d_gb, a.daily_limit_gb, eff_gb))

    # 刷 源远端 token
    try:
        import gd_token
        log("刷 源远端 token: %s" % ("OK" if gd_token.main() == 0 else "失败"))
    except Exception as e:
        log("!! gd_token 异常: %s" % str(e)[:120])

    src = lsjson(SRC)
    ty = alist_list(TY_DIR)
    db = alist_list(DBJM_DIR)
    log("源 %s: %d 个文件 | 云盘 %s: %d | dbjm: %d" % (SRC, len(src), TY_DIR, len(ty), len(db)))
    lines.append("源 %s %d 个 | %s %d 个 | %s %d 个" % (SRC, len(src), TY_DIR, len(ty), DBJM_DIR, len(db)))

    if a.apply:
        r = rclone(["mkdir", ARCH])
        log("确保 %s 存在: %s" % (ARCH, "OK" if r.returncode == 0 else (r.stderr or "")[:80]))

    todo = sorted(src.items(), key=lambda kv: kv[1])
    if a.only:
        todo = [(n, s) for n, s in todo if n == a.only]
        if not todo:
            lines.append("源里没有 %s" % a.only)
            finish(lines, a)
            return 1

    up_n = arch_n = skip_n = fail_n = 0
    total_up = 0
    lines.append("---")

    def checkpoint():
        """增量落盘(报告 + 日账本): 运行被取消/超时/强杀也不丢已完成的账"""
        if not a.apply:
            return
        snap = list(lines)
        snap.append("---")
        snap.append("(进行中) 上传 %d 个 (%s) | 归档 %d | 已有 %d | 失败 %d | 已用 %.1f 分钟"
                    % (up_n, human(total_up), arch_n, skip_n, fail_n, (time.time() - t0) / 60))
        finish(snap, a, quiet=True)
        if total_up > 0:
            save_daily(today, d_gb + total_up / GB)

    for name, size in todo:
        if up_n + arch_n + fail_n >= a.max_ops:
            lines.append("达本轮个数上限 %d, 收工" % a.max_ops)
            break
        if total_up >= max_total:
            lines.append("达本轮总量上限 %.1f GB, 收工" % a.max_total_gb)
            break
        if time.time() - t0 > a.budget_min * 60:
            lines.append("到时间预算, 收工")
            break
        head = "[%s] %s" % (human(size), name)

        # --- 1) 云盘侧 ---
        ty_hit = ty.get(name)
        ty_ok = bool(ty_hit and not ty_hit[1] and ty_hit[0] == size)
        note = ""
        if ty_ok:
            skip_n += 1
            note = "云盘已有(同名同大小)"
        elif not a.apply:
            note = "(演练) 将上传到 %s" % TY_DIR
            ty_ok = True                      # 演练: 假装成功, 好把后面归档逻辑也演出来
        else:
            lp = os.path.join(WORK, name)
            try:
                ft0 = time.time()
                r = rclone(["copyto", "%s/%s" % (SRC, name), lp, "--retries", "3",
                            "--low-level-retries", "10", "--transfers", "1",
                            "--multi-thread-streams", "1", "--timeout", "5m",
                            "--contimeout", "1m", "--stats", "15s",
                            "--stats-one-line"], timeout=1500)
                if r.returncode != 0 or os.path.getsize(lp) != size:
                    raise RuntimeError("下载失败/大小不符 :: %s" % (r.stderr or "")[:120])
                secs = alist_put(TY_DIR.rstrip("/") + "/" + name, lp, size, os.path.basename(name)[:26])
                got = alist_get(TY_DIR.rstrip("/") + "/" + name)
                if got != size:
                    raise RuntimeError("上传后校验不过: 目标 %s 期望 %s" % (got, size))
                ty_ok = True
                up_n += 1
                total_up += size
                ty[name] = (size, False)
                note = "上传云盘 OK (%.1f MB/s)" % (size / max(secs, 1e-6) / 1048576)
            except Exception as e:
                fail_n += 1
                note = "上传失败(%.1fmin): %s" % ((time.time() - ft0) / 60, str(e)[:120])
                lines.append("%s | ❌ %s" % (head, note))
                print("%s ❌ %s" % (head, note))
            finally:
                if not a.keep_local and os.path.exists(lp):
                    try:
                        os.remove(lp)
                    except OSError:
                        pass

        # --- 2) 完成判定 -> 归档 out3 ---
        arch_note = ""
        if ty_ok:
            db_ok = dbjm_done(name, size, db, thresh)
            if db_ok:
                if a.apply:
                    r = rclone(["moveto", "%s/%s" % (SRC, name), "%s/%s" % (ARCH, name),
                                "--retries", "3", "--low-level-retries", "10"])
                    if r.returncode == 0:
                        arch_n += 1
                        arch_note = "| 云盘+dbjm 都有 -> 已归档到 %s" % ARCH
                    else:
                        arch_note = "| !! 归档失败: %s" % (r.stderr or "")[:90]
                else:
                    arch_note = "| (演练) 云盘+dbjm 都有 -> 将归档到 %s" % ARCH
            else:
                arch_note = "| dbjm 还没有(等 od2-dbjm)"
        print("%s %s %s" % (head, note, arch_note))
        lines.append("%s | %s %s" % (head, note, arch_note))
        checkpoint()

    lines.append("---")
    lines.append("本轮: 上传云盘 %d 个 (%s) | 归档 out3 %d 个 | 云盘已有 %d 个 | 失败 %d"
                 % (up_n, human(total_up), arch_n, skip_n, fail_n))
    lines.append("源目录剩余 %d 个" % max(len(src) - arch_n, 0))
    print("\n合计: 上传 %d (%s) | 归档 %d | 已在 %d | 失败 %d"
          % (up_n, human(total_up), arch_n, skip_n, fail_n))
    if a.apply and total_up > 0:
        save_daily(today, d_gb + total_up / GB)
    finish(lines, a)
    return 0


def load_daily():
    """读今日已上传量; 返回 (日期, GB)"""
    try:
        r = rclone(["cat", DAILY], timeout=90)
        if r.returncode != 0:
            return "", 0.0
        d = json.loads((r.stdout or "").strip() or "{}")
        return d.get("date", ""), float(d.get("gb", 0) or 0)
    except Exception:
        return "", 0.0


def save_daily(date, gb):
    """写回今日累计上传量(供下一轮继续累计)"""
    try:
        payload = json.dumps({"date": date, "gb": round(gb, 2),
                              "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        r = subprocess.run([RCLONE, "rcat", DAILY, "--config", CONF],
                           input=payload, capture_output=True, text=True,
                           errors="replace", timeout=120)
        log("日配额账本 %.1fGB -> %s: %s"
            % (gb, DAILY, "OK" if r.returncode == 0 else (r.stderr or "")[:80]))
    except Exception as e:
        log("!! 日配额账本写入失败: %s" % str(e)[:80])


def finish(lines, a, quiet=False):
    text = "\n".join(lines) + "\n"
    rp = os.path.join(WORK, "out2_ty_report.txt")
    try:
        with io.open(rp, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log("!! 报告落盘失败: %s" % str(e)[:80])
    r = rclone(["copyto", rp, REPORT, "--retries", "2"], timeout=600)
    log("报告 -> %s: %s" % (REPORT, "OK" if r.returncode == 0 else (r.stderr or "")[:80]))


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
