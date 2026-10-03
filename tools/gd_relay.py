# -*- coding: utf-8 -*-
"""跨网盘中转搬运: 把 SRC 的文件搬到 DST, 搬成功后再处理源文件。

跨网盘没有 server-side copy, rclone 会自动
"下载到 runner -> 上传到目标" —— 这一步不用我们管, 我们要管的是**只在确认送达后才动源文件**。

三种模式(共用同一套逻辑, 只有源/目标/收尾动作不同):
  --mode in2-ydout : 源 = GR_A_SRC, 只挑名字含 GR_A_SUFFIX 的文件(默认 "_D");
                     目标 = GR_A_DST; 成功后 **删除** 源文件(进回收站可捞)
  --mode ydout-in3 : 源 = GR_B_SRC; 目标 = GR_B_DST;
                     成功后把源 **同盘移动** 到 GR_B_ARCH(服务端 move, 秒完成)
  --mode tg-gd2    : 源 = GR_TG_SRC(TG 目录), 全量不带后缀过滤;
                     目标 = GR_TG_DST; 成功后 **删除** 源文件

安全设计(这是全脚本的重点):
  * **只有"目标里存在同名且大小一致"的文件才算成功, 也才动源文件** —— 绝不留一半
  * 分批(按累计大小), 每批搬完立即校验+收尾, 再下一批
  * 到时间预算就优雅收工(已搬完的都收尾了), 剩下的下一轮继续
  * 默认演练(dry-run); --apply 才真搬
  * 同一文件连续失败 FAIL_MAX 次就不再重试(记云端失败清单)
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

WORK = "/tmp/gdrelay"
GB = 1024 ** 3
FAIL_MAX = 3
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
REPORT = pathcfg.require("GR_REPORT")
FAILED = pathcfg.require("GR_FAILED")

MODES = {
    "in2-ydout": {
        "src": lambda: pathcfg.require("GR_A_SRC"),
        "dst": lambda: pathcfg.require("GR_A_DST"),
        "suffix": lambda: pathcfg.get("GR_A_SUFFIX", "_D"),
        "arch": None,
        "on_success": "delete",          # 成功后删源(进回收站)
        "title": "已标记 _D 的文件 -> 目标远端",
    },
    "ydout-in3": {
        "src": lambda: pathcfg.require("GR_B_SRC"),
        "dst": lambda: pathcfg.require("GR_B_DST"),
        "suffix": lambda: "",
        "arch": lambda: pathcfg.require("GR_B_ARCH"),
        "on_success": "move",            # 成功后把源移到归档区
        "title": "目标远端 -> in3(并归档源)",
    },
    "tg-gd2": {
        # 源盘 TG 目录 -> 目标盘同名目录 (全量, 不筛后缀)
        "src": lambda: pathcfg.require("GR_TG_SRC"),
        "dst": lambda: pathcfg.require("GR_TG_DST"),
        "suffix": lambda: "",
        "arch": None,
        "on_success": "delete",          # 送达后删源(进回收站可捞)
        "title": "TG 目录 -> 目标远端(删源)",
    },
}


def log(msg):
    print("%s %s" % (time.strftime("%m-%d %H:%M:%S"), msg), flush=True)


def human(b):
    return "%.0f MB" % (b / 1048576) if b < GB else "%.2f GB" % (b / GB)


def rclone(args, timeout=14400):
    full = list(args) + ["--config", CONF]
    return subprocess.run([RCLONE] + full, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def lsjson(remote):
    """列目录 -> {名字: 字节数}(只取文件)"""
    r = rclone(["lsjson", remote], timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:180]))
    out = {}
    for x in (json.loads(r.stdout or "[]") or []):
        if not x.get("IsDir") and x.get("Name"):
            out[x["Name"]] = int(x.get("Size") or 0)
    return out


def load_failed():
    tmp = os.path.join(WORK, "failed.json")
    r = rclone(["copyto", FAILED, tmp, "--retries", "2"], timeout=600)
    if r.returncode != 0 or not os.path.exists(tmp):
        return {}
    try:
        d = json.load(io.open(tmp, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_failed(d):
    tmp = os.path.join(WORK, "failed.json")
    try:
        if not d:
            rclone(["deletefile", FAILED, "--retries", "2"], timeout=600)
            return
        io.open(tmp, "w", encoding="utf-8").write(
            json.dumps(d, ensure_ascii=False, indent=1))
        rclone(["copyto", tmp, FAILED, "--retries", "3"], timeout=600)
    except Exception as e:
        log("   !! 失败清单写入异常: %s" % str(e)[:80])


def write_list(names, tag):
    p = os.path.join(WORK, "_%s.txt" % tag)
    io.open(p, "w", encoding="utf-8", newline="\n").write("\n".join(names) + "\n")
    return p


def batches_of(names, sizes, batch_bytes):
    """按累计大小切批(单个文件超过 batch_bytes 也单独成一批)"""
    out, cur, acc = [], [], 0
    for n in names:
        s = sizes.get(n, 0)
        if cur and acc + s > batch_bytes:
            out.append(cur)
            cur, acc = [], 0
        cur.append(n)
        acc += s
    if cur:
        out.append(cur)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=sorted(MODES))
    ap.add_argument("--apply", action="store_true", help="真搬; 不加则只演练")
    ap.add_argument("--max-ops", type=int, default=4000, help="本轮最多处理多少个文件")
    ap.add_argument("--max-total-gb", type=float, default=200.0, help="本轮总量上限(GB)")
    ap.add_argument("--batch-gb", type=float, default=8.0, help="每批搬运量上限(GB)")
    ap.add_argument("--budget-min", type=int, default=300, help="本轮时间预算(分钟)")
    ap.add_argument("--only", default=os.environ.get("ONLY_NAME", ""),
                    help="只处理名字含该子串的文件(调试用)")
    a = ap.parse_args()

    m = MODES[a.mode]
    SRC, DST = m["src"](), m["dst"]()
    SUFFIX = m["suffix"]()
    ARCH = m["arch"]() if m["arch"] else ""
    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()

    lines = ["# 跨网盘中转报告 %s UTC  [%s]" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                "执行模式" if a.apply else "演练模式(dry-run)"),
             "# 模式: %s (%s)" % (a.mode, m["title"]),
             "# 规则: 目标端「同名同大小」才算成功; 送达后才处理源文件(%s)" %
             ("删除" if m["on_success"] == "delete" else "移到 %s" % ARCH)]
    log("模式 %s | %s -> %s" % (a.mode, SRC, DST))
    if SUFFIX:
        log("只处理名字含 %r 的文件" % SUFFIX)

    # ---- 列两端 ----
    try:
        src = lsjson(SRC)
        dst = lsjson(DST)
    except Exception as e:
        lines.append("\n!! %s" % str(e)[:200])
        log("!! 列目录失败: %s" % str(e)[:200])
        finish(lines, "\n".join(lines))
        return 1
    log("源 %s: %d 个 | 目标 %s: %d 个" % (SRC, len(src), DST, len(dst)))
    lines.append("\n源 %s %d 个 | 目标 %s %d 个" % (SRC, len(src), DST, len(dst)))

    # ---- 挑候选 ----
    cand = sorted(n for n in src if (not SUFFIX or SUFFIX in n))
    if a.only:
        cand = [n for n in cand if a.only in n]
        log("按 --only 过滤后: %d 个" % len(cand))
    if not cand:
        lines.append("\n没有符合条件的文件(带 %r 的 %d 个)" % (SUFFIX, len(src)))
        log("没有符合条件的文件")
        finish(lines, "\n".join(lines))
        return 0

    failed = load_failed()
    skipped_fail = [n for n in cand if failed.get(n, 0) >= FAIL_MAX]
    cand = [n for n in cand if n not in skipped_fail]
    # 目标端已有同名同大小的: 上次搬成功了但源还没处理掉 -> 直接收尾
    already = [n for n in cand if n in dst and dst[n] == src[n]]
    todo = [n for n in cand if n not in already]
    # 本轮上限
    picked, tot = [], 0
    for n in todo:
        if len(picked) >= a.max_ops or tot + src[n] > a.max_total_gb * GB:
            break
        picked.append(n)
        tot += src[n]
    log("候选 %d 个 | 目标已有(待收尾) %d | 本轮搬 %d (%.2f GB) | 失败跳过 %d | 留到下轮 %d"
        % (len(cand), len(already), len(picked), tot / GB, len(skipped_fail),
           max(0, len(todo) - len(picked))))
    lines.append("候选 %d 个 | 目标已有(待收尾) %d | 本轮搬 %d(%.2f GB) | 失败跳过 %d | 留到下轮 %d"
                 % (len(cand), len(already), len(picked), tot / GB, len(skipped_fail),
                    max(0, len(todo) - len(picked))))

    if a.only and not a.apply:
        log("[dry] 将搬: %s" % ", ".join(picked[:10]))
        finish(lines, "\n".join(lines))
        return 0
    if not a.apply:
        log("[dry] 演练结束(加 --apply 才真搬)")
        lines.append("\n(演练: 未搬运、未动任何源文件)")
        finish(lines, "\n".join(lines))
        return 0

    moved_ok, moved_fail = [], []

    def settle(names, why):
        """收尾: 把 names 从源文件里清掉(删除 或 移到归档区)"""
        if not names:
            return
        ff = write_list(names, "settle")
        if m["on_success"] == "delete":
            r = rclone(["delete", SRC, "--files-from", ff, "--retries", "3",
                        "--stats", "0"], timeout=3600)
        else:
            r = rclone(["move", SRC, ARCH, "--files-from", ff, "--retries", "3",
                        "--stats", "0"], timeout=3600)
        act = "删除" if m["on_success"] == "delete" else "移到 %s" % ARCH
        if r.returncode == 0:
            log("   收尾: %s %d 个源文件 (%s)" % (act, len(names), why))
            lines.append("   收尾: %s %d 个源文件 (%s)" % (act, len(names), why))
        else:
            log("   !! 收尾失败(源文件保持原样, 下轮重试): %s" % (r.stderr or "")[:150])
            lines.append("   !! 收尾失败: %s" % (r.stderr or "")[:150])

    # ---- 目标已有的: 直接收尾 ----
    if already:
        settle(already, "目标端已存在, 无需再搬")

    # ---- 分批搬 ----
    bs = batches_of(picked, src, a.batch_gb * GB)
    log("分 %d 批搬运" % len(bs))
    for bi, batch in enumerate(bs, 1):
        if (time.time() - t0) / 60 > a.budget_min:
            log("!! 到达时间预算 %d 分钟, 停止(剩下的下轮继续)" % a.budget_min)
            lines.append("\n!! 到达时间预算, 已搬完的都收尾了, 剩余下轮继续")
            break
        bsz = sum(src[n] for n in batch)
        log("第 %d/%d 批: %d 个 / %s ..." % (bi, len(bs), len(batch), human(bsz)))
        ff = write_list(batch, "batch")
        t = time.time()
        r = rclone(["copy", SRC, DST, "--files-from", ff,
                    "--size-only",                     # 跨网盘 modtime 不可比, 只比大小
                    "--transfers", "4", "--retries", "3", "--retries-sleep", "10s",
                    "--stats", "60s", "--stats-one-line"], timeout=14400)
        el = (time.time() - t) / 60
        log("   搬运完成: 退出码=%d 用时 %.1f 分钟" % (r.returncode, el))
        if r.returncode != 0:
            log("   (rclone 非零: %s)" % (r.stderr or "").strip()[:160])

        # 校验: 只有目标端"同名同大小"才算成功
        try:
            dst2 = lsjson(DST)
        except Exception as e:
            log("   !! 校验时列目标失败, 本批不做收尾: %s" % str(e)[:120])
            lines.append("   !! 第 %d 批校验失败, 未收尾" % bi)
            continue
        ok = [n for n in batch if n in dst2 and dst2[n] == src[n]]
        bad = [n for n in batch if n not in ok]
        log("   校验: 成功 %d | 未送达 %d" % (len(ok), len(bad)))
        settle(ok, "第 %d 批已送达" % bi)
        moved_ok += ok
        for n in bad:
            failed[n] = failed.get(n, 0) + 1
        moved_fail += bad

    # ---- 收尾统计 ----
    try:
        left = len([n for n in lsjson(SRC) if (not SUFFIX or SUFFIX in n)])
    except Exception:
        left = -1
    lines.append("\n---")
    lines.append("本轮: 送达并收尾 %d 个 | 未送达 %d 个 | 源端剩余(含 %r) %s 个"
                 % (len(moved_ok), len(moved_fail), SUFFIX, left))
    if moved_fail:
        lines.append("未送达明细(下轮重试, 连续 %d 次后不再试):" % FAIL_MAX)
        for n in moved_fail[:15]:
            lines.append("  - %s (已失败 %d 次)" % (n, failed.get(n, 0)))
    lines.append("用时 %.1f 分钟" % ((time.time() - t0) / 60))
    log("合计: 送达 %d | 未送达 %d | 源端剩余 %s" % (len(moved_ok), len(moved_fail), left))

    if failed:
        save_failed(failed)
    finish(lines, "\n".join(lines))
    return 0


def finish(lines, text):
    """报告落盘 + 上传到 报告区"""
    rp = os.path.join(WORK, "gd_relay_report.txt")
    try:
        io.open(rp, "w", encoding="utf-8", newline="\n").write(text + "\n")
    except Exception as e:
        log("!! 本地报告写入失败: %s" % str(e)[:100])
    r = rclone(["copyto", rp, REPORT, "--retries", "3"], timeout=900)
    log("报告 -> %s: %s" % (REPORT, "OK" if r.returncode == 0 else
                            "失败 " + (r.stderr or "")[:100]))


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
