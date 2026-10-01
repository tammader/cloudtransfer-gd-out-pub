# -*- coding: utf-8 -*-
"""上游盘 /in 的跨系统判重 -> 已归档的移出 /out, 其余写成"待下载清单"

为什么需要它(和 cleanup 那条流程的分工):
  * 清理流程只做 /in **内部**去重(同编号保最大、其余移出 /out), 它不读任何清单、不看目标盘;
  * 本流程做的是**跨系统**判重: 编号已经落在目标侧任何一个 out 区(实时列 + 云端编号清单),
    说明这份早就走完全流程了 -> 直接从 /in 移出 /out, **不必再下载/切分/上传**;
  * 剩下的才写进"待下载清单"(TSV), 交给手机端去下载 → 切分 → 上传。

跑法: runner 上装 rclone, 凭证走 Secrets:
  HALAL_CLIENT_ID / HALAL_CLIENT_SECRET / HALAL_REFRESH_TOKEN  (上游盘直连)
  RCLONE_CONF (中转远端) · PATHS_JSON (盘名/目录名)
默认演练(--dry), --apply 才真移动。产出(都在 中转远端:报告区/):
  <队列TSV>     名字<TAB>字节   —— 待下载清单(手机端读它)
  <报告TXT>     本轮结果
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg                      # 路径真值: CI=Secret PATHS_JSON, 本机=paths.local.json
try:
    from halal6 import Halal6       # 上游盘直连 OpenAPI
except Exception:                   # pragma: no cover
    Halal6 = None

HL_IN = pathcfg.require("HL_IN")                       # 上游盘 /in(端侧路径, 不带挂载名)
HL_OUT = pathcfg.require("HL_OUT")                     # 上游盘 /out
OD_DIRS = [x.strip() for x in pathcfg.require("ID_OD_DIRS").split(",") if x.strip()]
STEM_MANIFESTS = [x.strip() for x in pathcfg.require("ID_STEM_MANIFESTS").split(",") if x.strip()]
QUEUE = pathcfg.require("ID_QUEUE")
REPORT = pathcfg.require("ID_REPORT")
DONE_PREFIX = pathcfg.get("ID_DONE_PREFIX", "")
MAX_MOVE = int(pathcfg.get("ID_MAX_MOVE", "800"))
ID_RE = re.compile(r"[A-Za-z]{1,10}-[0-9]{1,6}")     # 编号识别: bat-123 / ANKK-041
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def stem(name):
    """复刻本机 worker: 去扩展名 / _D / 前缀 / .partNNN, 小写"""
    s = os.path.splitext(name)[0]
    s = re.sub(r"_D$", "", s)
    s = re.sub(r"^(已处理_|D_)", "", s)
    s = re.sub(r"\.part\d+$", "", s)
    return s.strip().lower()


def find_conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = find_conf()


def rclone(args, timeout=1800):
    return subprocess.run([RCLONE] + args + ["--config", CONF],
                          capture_output=True, text=True, errors="replace", timeout=timeout)


def lsjson(remote):
    r = rclone(["lsjson", remote, "--files-only"], timeout=900)
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:140]))
    return [x["Name"] for x in json.loads(r.stdout or "[]")]


def collect_have():
    """目标侧"已归档"的编号集合 = 实时列中转远端各目录 ∪ 云端编号清单

    返回 (stems, ids):
      stems = 精确名(去扩展名/前缀/分卷后) —— 命中即可确定"已归档", 可以直接移出
      ids   = 从 stem 里再抽出的**编号** —— /in 侧文件名往往还带杂质前缀(站点名+@+编号)
              或序号/标记不一致, 精确名对不上, 但编号能对上; 只用来**避免重复下载**, 不移出。
    """
    have, ids = set(), set()
    for d in OD_DIRS:
        try:
            names = lsjson(d)
        except Exception as e:
            log("   !! 实时列失败(跳过该目录): %s" % str(e)[:110])
            continue
        for n in names:
            have.add(stem(n))
        log("   实时 %-26s %d 个" % (d.split(":")[-1], len(names)))
    for mf in STEM_MANIFESTS:
        try:
            r = rclone(["cat", mf], timeout=300)
            if r.returncode != 0:
                log("   !! 清单读不到(跳过): %s" % (r.stderr or "")[:100])
                continue
            cnt = 0
            for line in (r.stdout or "").splitlines():
                x = line.strip()
                if x and not x.startswith("#"):
                    have.add(x.lower())
                    cnt += 1
            log("   清单 %-26s %d 个编号" % (mf.split("/")[-1], cnt))
        except Exception as e:
            log("   !! 清单异常(跳过): %s" % str(e)[:100])
    for s in have:
        m = ID_RE.search(s)
        if m:
            ids.add(m.group(0).lower())
    return have, ids


def upload(local, remote, label):
    r = rclone(["copyto", local, remote, "--retries", "3", "--retries-sleep", "5s"], timeout=600)
    log("   %s -> %s: %s" % (label, remote, "OK" if r.returncode == 0 else (r.stderr or "")[:100]))
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真移动; 默认只演练")
    ap.add_argument("--max-ops", type=int, default=MAX_MOVE, help="本轮最多移出几个")
    ap.add_argument("--budget-min", type=int, default=20, help="本轮时间预算(分钟)")
    a = ap.parse_args()

    t0 = time.time()
    lines = ["# 上游盘 %s 跨系统判重报告  %s  [%s]"
             % (HL_IN, time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练")]

    if Halal6 is None:
        lines.append("!! 缺 halal6 模块")
        print(lines[-1])
        return 1
    cid = os.environ.get("HALAL_CLIENT_ID", "")
    sec = os.environ.get("HALAL_CLIENT_SECRET", "")
    rt = os.environ.get("HALAL_REFRESH_TOKEN", "")
    if not (cid and sec and rt):
        lines.append("!! 缺 HALAL_CLIENT_ID / HALAL_CLIENT_SECRET / HALAL_REFRESH_TOKEN")
        print(lines[-1])
        return 1

    log("连上游盘 ...")
    c = Halal6(cid, sec, rt)
    msg = c.login()
    if not getattr(c, "token", ""):
        lines.append("!! 上游盘登录失败: %s" % msg)
        print(lines[-1])
        return 1
    log("   登录: %s" % msg)

    try:
        items = c.ls_all(HL_IN)
    except Exception as e:
        lines.append("!! 列 %s 失败: %s" % (HL_IN, str(e)[:160]))
        print(lines[-1])
        return 1
    files = [(p, n, s) for (p, n, s, isd) in items
             if not isd and s > 0 and not (DONE_PREFIX and n.startswith(DONE_PREFIX))]
    dirs = [n for (_, n, _, isd) in items if isd]
    log("   %s: 文件 %d 个 + 子目录 %d 个" % (HL_IN, len(files), len(dirs)))
    lines.append("%s: 文件 %d 个 + 子目录 %d 个" % (HL_IN, len(files), len(dirs)))

    log("收集目标侧已归档编号 ...")
    have, have_ids = collect_have()
    log("   精确名 %d 个 | 编号 %d 个" % (len(have), len(have_ids)))

    todo_move, suspect, queue = [], [], []
    for p, n, s in files:
        if stem(n) in have:
            todo_move.append((p, n, s))          # 精确命中: 确定已归档 -> 移出
            continue
        m = ID_RE.search(n)
        if m and m.group(0).lower() in have_ids:
            suspect.append((p, n, s))            # 仅编号命中: 不下载, 也先不动它
            continue
        queue.append((p, n, s))
    q_bytes = sum(s for _, _, s in queue)
    log("   已归档可移出 %d | 仅编号疑似已归档 %d | 待下载 %d (%.2f GB)"
        % (len(todo_move), len(suspect), len(queue), q_bytes / 1024.0 ** 3))
    lines.append("已归档可移出 %d | 仅编号疑似 %d | 待下载 %d (%.2f GB)"
                 % (len(todo_move), len(suspect), len(queue), q_bytes / 1024.0 ** 3))

    # 待下载清单(先行落盘, 手机端要用)
    tf = "/tmp/in_queue.tsv"
    rows = ["%s\t%d" % (n, s) for _, n, s in sorted(queue, key=lambda x: x[2])]
    io.open(tf, "w", encoding="utf-8").write(
        "# %s 待下载清单 (名字<TAB>字节) 更新: %s | %d 个 | %.2f GB\n"
        % (HL_IN, time.strftime("%Y-%m-%d %H:%M:%S"), len(rows), q_bytes / 1024.0 ** 3)
        + "\n".join(rows) + "\n")
    ok_q = upload(tf, QUEUE, "待下载清单 %d 条" % len(rows)) if a.apply else False
    if not a.apply:
        log("   (演练) 待下载清单 %d 条 未上传" % len(rows))

    # 移出已归档的
    moved = failed = 0
    if a.apply and todo_move:
        batch = a.max_ops
        for i in range(0, min(len(todo_move), a.max_ops), 50):
            if time.time() - t0 > a.budget_min * 60:
                lines.append("到时间预算, 收工(剩余下轮继续)")
                break
            chunk = todo_move[i:i + 50]
            try:
                r = c.move([p for p, _, _ in chunk], HL_OUT)
                ok = not (r.get("_error") or r.get("_net_error"))
            except Exception:
                ok = False
            if ok:
                moved += len(chunk)
            else:                                  # 整批失败 -> 逐个来, 免得好文件陪葬
                for p, n, s in chunk:
                    try:
                        r1 = c.move([p], HL_OUT)
                        if r1.get("_error") or r1.get("_net_error"):
                            raise RuntimeError(str(r1)[:80])
                        moved += 1
                    except Exception as e:
                        failed += 1
                        log("   !! 移出失败: %s" % str(e)[:90])
                        lines.append("- 移出失败: %d 字节 (见日志)" % s)
            log("   移出进度 %d / %d" % (moved, min(len(todo_move), a.max_ops)))
    elif not a.apply:
        log("   (演练) 将移出 %d 个 -> %s" % (len(todo_move), HL_OUT))
    lines.append("移出: %d 个成功 | %d 个失败 | 上限 %d" % (moved, failed, a.max_ops))

    lines.append("待下载清单: %s" % ("OK" if ok_q else ("未上传(演练)" if not a.apply else "FAIL")))
    rp = "/tmp/in_dedup_report.txt"
    io.open(rp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    if a.apply:
        upload(rp, REPORT, "报告")
    else:
        log("   (演练) 报告未上传; 内容:")
        for l in lines:
            log("     " + l)
    return 0


if __name__ == "__main__":
    import logmask              # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
