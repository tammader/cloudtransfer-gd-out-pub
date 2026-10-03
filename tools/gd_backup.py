# -*- coding: utf-8 -*-
"""网盘目录级备份: 把 SRC_ROOT 下的若干目录 **拷贝** 到 DST_ROOT 的同名目录(源保留)。

与 gd_relay.py 的区别: 那边是"搬运"(成功即删/移源), 这边是"备份/镜像"(源一个都不动),
所以用 rclone 原生的目录级 copy 更合适 —— 自带并发、断点重试、同名同大小自动跳过,
不用逐文件校验收尾。每轮跑一遍即可增量补齐(新文件追加上去)。

配置(走 pathcfg: CI=Secret PATHS_JSON, 本机=paths.local.json):
  GB_SRC_ROOT  源远端名(真值在配置里)
  GB_DST_ROOT  目标远端名(真值在配置里)
  GB_DIRS      要备份的目录名, 逗号分隔(真值在配置里)
  GB_REPORT    报告落点(真值在配置里)

安全: 默认演练(--dry); --apply 才真拷。源端**只读**。
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

import pathcfg          # 路径真值: CI=Secret PATHS_JSON, 本机=paths.local.json

WORK = "/tmp/gdbackup"
GB = 1024 ** 3
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"


def _find_conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf"),
              os.path.expanduser("~/AppData/Roaming/rclone/rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = _find_conf()
SRC_ROOT = pathcfg.require("GB_SRC_ROOT")
DST_ROOT = pathcfg.require("GB_DST_ROOT")
DIRS = [x.strip() for x in pathcfg.require("GB_DIRS").split(",") if x.strip()]
REPORT = pathcfg.require("GB_REPORT")


def log(msg):
    print("%s %s" % (time.strftime("%m-%d %H:%M:%S"), msg), flush=True)


def human(b):
    return "%.0f MB" % (b / 1048576) if b < GB else "%.2f GB" % (b / GB)


def rclone(args, timeout=21600):
    return subprocess.run([RCLONE] + list(args) + ["--config", CONF],
                          capture_output=True, text=True, errors="replace", timeout=timeout)


def lsjson(remote, timeout=3600):
    """列目录 -> {名字: 字节数}(只取文件)"""
    r = rclone(["lsjson", remote], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:180]))
    out = {}
    for x in (json.loads(r.stdout or "[]") or []):
        if not x.get("IsDir") and x.get("Name"):
            out[x["Name"]] = int(x.get("Size") or 0)
    return out


def finish(lines, text):
    """报告落盘 + 上传到报告区"""
    rp = os.path.join(WORK, "gd_backup_report.txt")
    try:
        io.open(rp, "w", encoding="utf-8", newline="\n").write(text + "\n")
    except Exception as e:
        log("!! 本地报告写入失败: %s" % str(e)[:100])
    r = rclone(["copyto", rp, REPORT, "--retries", "3"], timeout=900)
    log("报告 -> %s: %s" % (REPORT, "OK" if r.returncode == 0 else
                            "失败 " + (r.stderr or "")[:100]))
    for ln in lines:
        print(ln, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真拷; 不加则只演练")
    ap.add_argument("--only", default=os.environ.get("ONLY_NAME", ""),
                    help="只处理该目录(调试用), 例: --only out2")
    ap.add_argument("--transfers", type=int, default=4, help="并发传输数")
    ap.add_argument("--checkers", type=int, default=8)
    ap.add_argument("--budget-min", type=int, default=320, help="本轮时间预算(分钟)")
    a = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()
    dirs = [d for d in DIRS if (not a.only or d == a.only)]
    lines = ["# 网盘目录备份报告 %s UTC  [%s]" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                              "执行模式" if a.apply else "演练模式(dry-run)"),
             "# %s -> %s, 目录: %s" % (SRC_ROOT, DST_ROOT, ",".join(dirs)),
             "# 规则: 目录级 copy, 同名同大小跳过; **源端只读, 不动任何文件**"]
    log("备份 %s -> %s | 目录 %s" % (SRC_ROOT, DST_ROOT, ",".join(dirs)))

    # ---- 1) 先逐目录算差异(不写), 好按工作量排序 ----
    plan = []
    for d in dirs:
        s_rem, t_rem = "%s:%s" % (SRC_ROOT, d), "%s:%s" % (DST_ROOT, d)
        try:
            s = lsjson(s_rem)
        except Exception as e:
            log("!! 列源失败(跳过): %s -> %s" % (s_rem, str(e)[:150]))
            lines.append("\n!! %s 列源失败: %s" % (d, str(e)[:150]))
            continue
        try:
            t = lsjson(t_rem)
        except Exception as e:
            # 目标目录不存在=全新备份, 当空处理
            log("   目标 %s 列不到(当空): %s" % (t_rem, str(e)[:100]))
            t = {}
        miss = {n: sz for n, sz in s.items() if t.get(n) != sz}
        plan.append({"d": d, "s": s, "t": t, "miss": miss,
                     "bytes": sum(miss.values())})
        log("   %-16s 源 %4d 个 / %-11s | 目标已有 %4d 个 | 待拷 %4d 个 / %s"
            % (d, len(s), human(sum(s.values())), len(t), len(miss), human(sum(miss.values()))))

    tot_miss = sum(p["bytes"] for p in plan)
    lines.append("\n待拷合计: %d 个 / %s" % (sum(len(p["miss"]) for p in plan), human(tot_miss)))
    log("待拷合计: %d 个 / %s" % (sum(len(p["miss"]) for p in plan), human(tot_miss)))

    if not a.apply:
        lines.append("(演练: 未拷贝任何文件)")
        finish(lines, "\n".join(lines))
        return 0

    # ---- 2) 按工作量从小到大执行(快的先完成) ----
    order = sorted([p for p in plan if p["miss"]], key=lambda x: x["bytes"])
    if not order:
        lines.append("\n所有目录都已是最新, 无需拷贝")
        log("所有目录都已是最新")
        finish(lines, "\n".join(lines))
        return 0

    lines.append("")
    for p in order:
        d = p["d"]
        left = a.budget_min - (time.time() - t0) / 60
        if left <= 1:
            lines.append("!! 到达时间预算, 剩余目录下轮继续: %s"
                         % ",".join(x["d"] for x in order[order.index(p):]))
            log("!! 到达时间预算, 停止")
            break
        s_rem, t_rem = "%s:%s" % (SRC_ROOT, d), "%s:%s" % (DST_ROOT, d)
        mins = int(min(left, 300))
        log(">>> 拷贝 %s: 待拷 %d 个 / %s (预算 %d 分钟)"
            % (d, len(p["miss"]), human(p["bytes"]), mins))
        t = time.time()
        # --max-duration: 到点优雅收工, 避免超过 runner 上限; 退出码 10 = 超时(正常)
        r = rclone(["copy", s_rem, t_rem,
                    "--size-only",                     # 跨网盘 modtime 不可比, 只比大小
                    "--transfers", str(a.transfers),
                    "--checkers", str(a.checkers),
                    "--retries", "3", "--retries-sleep", "10s",
                    "--max-duration", "%dm" % mins,
                    "--stats", "60s", "--stats-one-line"])
        el = (time.time() - t) / 60
        ok_code = r.returncode in (0, 10)
        log("   退出码=%d%s 用时 %.1f 分钟"
            % (r.returncode, " (到达 max-duration, 正常)" if r.returncode == 10 else "", el))
        if not ok_code:
            log("   !! rclone 报错: %s" % (r.stderr or "").strip()[:200])
            lines.append("!! %s 拷贝报错(退出码 %d): %s"
                         % (d, r.returncode, (r.stderr or "").strip()[:160]))

        # ---- 3) 复核: 只认"同名同大小" ----
        try:
            t2 = lsjson(t_rem)
            left_names = [n for n, sz in p["s"].items() if t2.get(n) != sz]
            done = len(p["s"]) - len(left_names)
            lines.append("%-16s 源 %d 个 | 现存 %d 个 | 本轮后仍缺 %d 个%s"
                         % (d, len(p["s"]), done, len(left_names),
                            "" if not left_names else "  (下轮继续)"))
            log("   复核: 现存 %d/%d | 仍缺 %d" % (done, len(p["s"]), len(left_names)))
        except Exception as e:
            lines.append("!! %s 复核失败: %s" % (d, str(e)[:120]))
            log("   !! 复核失败: %s" % str(e)[:120])

    lines.append("")
    lines.append("用时 %.1f 分钟" % ((time.time() - t0) / 60))
    finish(lines, "\n".join(lines))
    return 0


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
