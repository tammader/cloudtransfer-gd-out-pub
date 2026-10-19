# -*- coding: utf-8 -*-
"""gdrive2 -> Teldrive 冷备份: 自动找出「含视频的文件夹」, 整个文件夹备份

扫描逻辑(按文件夹为单位, 不是按单个文件):
  1) 列出 源(gdrive2) 全部文件, 按所在目录分组
  2) 只要某个目录里有视频文件, 这个目录就入选(默认连目录里的字幕/图片一起传)
  3) 目标端已有且同名同大小的文件跳过; 整个目录都齐了就跳过这个目录
  4) 按本轮总量预算(--max-transfer-gb)挑若干个目录, 到量收工下次继续
  5) 目标端保持原目录结构: teldrive:gd2-backup/<原目录相对路径>/...

为什么必须用 tgdrive/rclone fork:
  官方 rclone **没有** teldrive backend。fork 是官方版的超集, 同一个二进制
  gdrive2 / teldrive 都能连, 不用装两份。

路径真值: pathcfg(CI=Secret PATHS_JSON / 本机 paths.local.json)
  本流程新增的 key 放**第二个** Secret PATHS_JSON_TD(同为 JSON 字典), 两个字典合并,
  同名 key 以 PATHS_JSON_TD 为准 —— 免得去动现有那条 PATHS_JSON。

需要的配置 key:
  GT_SRC    源根, 例如 gdrive2:
  GT_DST    目标, 例如 teldrive:gd2-backup
  GT_REPORT 报告落点, 例如 gdrive2:报告区/gd2_teldrive_report.txt

需要的环境变量(CI 由 Secret 注入):
  TELDRIVE_API_KEY / TELDRIVE_API_HOST —— Teldrive Web UI 里 Settings → API keys 生成

默认演练: 不加 --apply 只打印计划, 不动任何东西。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg  # 路径真值来源: CI=Secret PATHS_JSON, 本机=paths.local.json

# ---- 公开仓库脱敏: 必须在任何输出之前装, 文件名/盘名一律换哈希标签 ----
import logmask                                     # noqa: E402
logmask.install()                                  # LOG_MASK=0 可关(仅本机调试)

# ---- 允许第二个配置字典(新增流程专用), 把 GT_* 塞进 PATHS_JSON_TD 即可 ----
_td = os.environ.get("PATHS_JSON_TD", "").strip()
if _td:
    try:
        _merged = dict(pathcfg._load())
        _merged.update(json.loads(_td))
        pathcfg._cache = _merged
    except Exception as e:
        print("!! PATHS_JSON_TD 解析失败, 忽略: %s" % e, file=sys.stderr)

SRC = pathcfg.require("GT_SRC")
DST = pathcfg.require("GT_DST")
REPORT = pathcfg.require("GT_REPORT")

GB = 1024 ** 3
MB = 1024 ** 2
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
WORK = os.environ.get("GT_WORK", "/tmp/gd2teldrive")

# 视频扩展名(判到「末尾」, 所以 xxx.part001.mp4 这种切分名也算)
VIDEO_RE = re.compile(
    r"\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpg|mpeg|m2ts|ts|rmvb|rm|3gp|vob|divx|f4v|asf)$",
    re.I)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def human(b):
    return "%.1f MB" % (b / MB) if b < GB else "%.2f GB" % (b / GB)


def dump_stats(stats):
    """把本轮统计写成 JSON, 供 tools/gd2_teldrive_report.py 出图发 Telegram

    落点由环境变量 TD_STATS_JSON 指定; 没设就静默跳过(不影响备份本身)。
    """
    p = os.environ.get("TD_STATS_JSON", "").strip()
    if not p:
        return
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)
        log("统计 JSON -> %s" % p)
    except Exception as e:
        log("!! 统计 JSON 写失败(不影响备份): %s" % str(e)[:120])


def is_video(name):
    return bool(VIDEO_RE.search(name or ""))


def check_backend():
    """官方 rclone 没有 teldrive backend, 装错了要早报错"""
    r = subprocess.run([RCLONE, "help", "backend", "teldrive"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "!! 当前 rclone 不含 teldrive backend —— 必须用 tgdrive/rclone fork\n"
            "   下载地址: https://github.com/tgdrive/rclone/releases\n"
            "   验证命令: rclone help backend teldrive")
    ver = subprocess.run([RCLONE, "version"], capture_output=True, text=True)
    log("rclone 就绪: %s" % (ver.stdout.strip().splitlines()[0] if ver.stdout else "?"))


def clear_open_sessions(limit=2000):
    """开跑前清掉上一轮「预算到点被 SIGINT」留下的 open upload session。

    为什么必须做(2026-10-19 实测踩坑):
      每轮到点收工, 正在传的文件会留下半截 session。攒几轮之后, 同一路径的会话
      互相打架, 新上传直接 `TelDrive API error 409 (conflict): operation conflicts
      with current state`, 整轮反复重试, 310 分钟只跑出 45 GiB。
      清理是幂等的: 只删 state=open 的, 已完成的不受任何影响; 最坏情况是某个
      半截文件下轮重传, 反正它本来也没传完。

    失败一律不影响主流程(返回 0), 这只是「尽量把路清干净」。
    """
    host = (os.environ.get("TELDRIVE_API_HOST") or "").strip()
    key = (os.environ.get("TELDRIVE_API_KEY") or "").strip()
    if not host or not key:
        log("清理 open session: 跳过(没配 TELDRIVE_API_HOST / TELDRIVE_API_KEY)")
        return 0
    if not host.lower().startswith("http"):
        host = "https://" + host
    base = host.rstrip("/") + "/api/v1"
    hdr = {"X-Api-Key": key, "User-Agent": "cloudtransfer-backup"}

    def call(path, method="GET"):
        req = urllib.request.Request(base + path, method=method, headers=hdr)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()

    n, cursor, seen = 0, "", 0
    try:
        while n < limit:
            q = "/uploads?limit=200&state=open" + ("&cursor=" + cursor if cursor else "")
            st, body = call(q)
            if st != 200:
                log("清理 open session: 列表返回 %s, 停手" % st)
                break
            d = json.loads(body)
            items = d.get("items") or []
            for it in items:
                uid = it.get("id")
                if not uid:
                    continue
                try:
                    call("/uploads/%s" % uid, "DELETE")
                    n += 1
                except Exception as e:
                    log("  删会话失败 %s: %s" % (it.get("name", "?")[:40], str(e)[:80]))
            seen += len(items)
            cursor = d.get("nextCursor") or ""
            if not items or not cursor:
                break
        log("清理 open session: 删掉 %d 个(扫到 %d 个)" % (n, seen))
    except Exception as e:
        log("清理 open session 出错(不影响备份): %s" % str(e)[:150])
    return n


def lsjson(remote, allow_missing=False):
    """allow_missing=True 时, 目标目录还不存在不算错误(首次备份就是这种情况), 返回空清单"""
    r = subprocess.run(
        [RCLONE, "lsjson", remote, "-R", "--files-only", "--fast-list", "--no-modtime"],
        capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()
        if allow_missing and ("directory not found" in err or "not found" in err):
            log("目标端 %s 还不存在 -> 当作空清单(首次备份会自动创建)" % remote)
            return []
        raise SystemExit("!! 列目录失败 %s: %s" % (remote, err[:400]))
    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise SystemExit("!! 解析 lsjson 失败: %s" % e)


def build_dirs(items):
    """按目录分组 -> {目录: {'files': [(相对路径, 大小)], 'video': bool, 'total': int}}"""
    dirs = {}
    for it in items:
        if it.get("IsDir"):
            continue
        p, size = it["Path"], int(it.get("Size", 0) or 0)
        d = os.path.dirname(p)
        e = dirs.setdefault(d, {"files": [], "video": False, "total": 0})
        e["files"].append((p, size))
        e["total"] += size
        if is_video(p):
            e["video"] = True
    return dirs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="不加=只演练看计划")
    ap.add_argument("--max-transfer-gb", type=float, default=150.0,
                    help="本轮总量上限(GB); 实测 ~8MiB/s, 270 分钟约 130GB, 默认 150 够用")
    ap.add_argument("--budget-min", type=int, default=270,
                    help="本轮时间预算(分钟)。GitHub job 硬上限 350, 留 80 分钟给收尾"
                         " —— 2026-10-19 有一轮卡到 350 被强杀, 整轮白跑")
    ap.add_argument("--max-dirs", type=int, default=0, help="本轮最多处理几个文件夹; 0=不限")
    ap.add_argument("--transfers", type=int, default=3,
                    help="并发文件数。求稳默认 3(Telegram 侧并发高容易 504/限流)")
    ap.add_argument("--checkers", type=int, default=8)
    ap.add_argument("--chunk-mb", type=int, default=8,
                    help="Teldrive 分片(MB)。实测 Unikraft 网关单请求体上限 8MiB"
                         " (8Mi OK / 9Mi 起 413 Request Entity Too Large), 默认 8;"
                         " 调大必炸, 调小只会更慢")
    ap.add_argument("--retries", type=int, default=5,
                    help="失败重试次数; 求稳可以调大(默认 5, 配 30s 间隔)")
    ap.add_argument("--tpslimit", type=float, default=10.0,
                    help="每秒最多 API 请求数, 给 teldrive/Telegram 留余量(0=不限)")
    ap.add_argument("--no-clear-sessions", action="store_true",
                    help="不清理上一轮残留的 open upload session(默认会清, 防 409 冲突)")
    ap.add_argument("--video-only", action="store_true", help="只传视频文件(默认整个文件夹都传)")
    ap.add_argument("--no-fast-list", action="store_true", help="关闭 --fast-list(配额敏感时用)")
    ap.add_argument("--only", default="", help="只处理这一个文件夹(相对路径, 调试用)")
    ap.add_argument("--preview", type=int, default=20, help="演练时最多打印几个文件夹")
    a = ap.parse_args()

    check_backend()
    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()

    log("源   : %s" % SRC)
    log("目标 : %s" % DST)
    log("模式 : %s" % ("真跑(--apply)" if a.apply else "演练(只列计划)"))

    global _NO_FAST
    _NO_FAST = a.no_fast_list

    src_items = lsjson(SRC)
    log("源端文件总数: %d" % len(src_items))

    dst_map = {}
    for it in lsjson(DST, allow_missing=True):
        if not it.get("IsDir"):
            dst_map[it["Path"]] = int(it.get("Size", 0) or 0)
    log("目标端已有: %d 个" % len(dst_map))

    dirs = build_dirs(src_items)
    video_dirs = {d: v for d, v in dirs.items() if v["video"]}
    log("含视频的文件夹: %d 个 (全部文件夹 %d 个)" % (len(video_dirs), len(dirs)))

    # 挑本轮要做的文件夹: 整个目录里还有文件没备份的才入选
    max_bytes = int(a.max_transfer_gb * GB)

    # 先把每个目录「还差多少」算出来, 再按待传量从大到小排。
    # 为什么要降序: 以前是按字母序, 一个 263 GiB 的大目录排在零碎小目录后面,
    # 小目录先占满 plan 之后, 大目录因为「塞不下」被整段跳过 —— 于是每轮只啃几 GB 零头,
    # 大头永远排队等下一轮, 等于饿死。降序让大目录每轮第一个吃预算, 稳步收敛。
    pending = []
    for d in video_dirs:
        if a.only and d != a.only:
            continue
        e = video_dirs[d]
        files = e["files"] if not a.video_only else [x for x in e["files"] if is_video(x[0])]
        todo = [(p, s) for p, s in files if dst_map.get(p) != s]
        if not todo:
            continue                       # 这个文件夹已经全备份过了
        pending.append((d, todo, sum(s for _, s in todo)))
    pending.sort(key=lambda x: (-x[2], x[0]))

    plan, planned_bytes = [], 0
    for d, todo, need in pending:
        # 塞不下就跳过(去看后面有没有更小的目录);
        # plan 为空时例外 —— 否则比上限还大的单个目录永远轮不到
        if plan and planned_bytes + need > max_bytes:
            continue
        plan.append((d, todo, need))
        planned_bytes += need
        if a.max_dirs and len(plan) >= a.max_dirs:
            break
        if planned_bytes >= max_bytes:
            break

    log("本轮入选 %d 个文件夹, 需传 %s (上限 %s)"
        % (len(plan), human(planned_bytes), human(max_bytes)))
    for d, todo, need in plan[:a.preview]:
        vids = sum(1 for p, _ in todo if is_video(p))
        log("   + %s  (%d 个文件/其中视频 %d, %s)" % (d or "(根目录)", len(todo), vids, human(need)))
    if len(plan) > a.preview:
        log("   ... 其余 %d 个省略" % (len(plan) - a.preview))

    if not a.apply:
        dump_stats({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "演练", "apply": False,
            "event": os.environ.get("TD_EVENT", ""),
            "src_files": len(src_items),
            "video_dirs": len(video_dirs),
            "plan_dirs": len(plan), "plan_bytes": planned_bytes,
            "plan_detail": [{"dir": d or "(根目录)",
                             "n": len(todo), "bytes": need} for d, todo, need in plan[:8]],
            "dst_files": len(dst_map), "dst_bytes": sum(dst_map.values()),
            "new_files": 0, "new_bytes": 0,
            "done_dirs": 0, "left_dirs": 0, "left_detail": [],
            "minutes": round((time.time() - t0) / 60, 1),
            "budget_min": a.budget_min, "chunk_mb": a.chunk_mb,
            "transfers": a.transfers, "rc": 0, "budget_hit": False,
            "src": SRC, "dst": DST,
            "note": "演练模式: 只列计划, 没传任何东西",
        })
        log("演练结束 —— 确认无误后加 --apply 真跑")
        return

    # ---- 清掉目标端的 0 字节空壳 ----
    # 之前分片过大被网关 413 打回时, teldrive 会留下同名的 0 字节文件;
    # 不清掉的话既占清单又可能让 --size-only 误判, 先删干净再传。
    shells = [p for p in dst_map if dst_map[p] == 0]
    if shells:
        log("目标端 0 字节空壳 %d 个 -> 先删除" % len(shells))
        for p in shells[:2000]:
            subprocess.run([RCLONE, "deletefile", "%s/%s" % (DST.rstrip("/"), p)],
                           capture_output=True, text=True)

    # ---- 清掉上一轮留下的 open upload session(409 冲突的根源) ----
    # 放在真跑前、0 字节清理之后: 此时本 job 还没有任何上传, 删掉的全是历史残留。
    if not a.no_clear_sessions:
        clear_open_sessions()

    # ---- 真跑 ----
    ff = os.path.join(WORK, "files.txt")
    with open(ff, "w", encoding="utf-8") as f:
        for _, todo, _ in plan:
            for p, _ in todo:
                f.write(p + "\n")

    budget_sec = a.budget_min * 60
    cmd = [
        # --kill-after: 到点先发 INT 让 rclone 优雅收尾(写完手上这个分片);
        # 2 分钟还不退就 KILL —— 防止 rclone 卡在某个 504 上把整个 job 拖到
        # GitHub 350 分钟硬上限被杀, 那时连统计都来不及写(2026-10-19 就这样丢过一轮)。
        "timeout", "--signal=INT", "--kill-after=120s", "%ds" % budget_sec,
        RCLONE, "copy", SRC, DST,
        "--files-from", ff,
        # 用 --size-only: 目标端同大小就跳过, 大小不同(含 0 字节空壳)就覆盖;
        # 不能用 --ignore-existing —— 那会让 0 字节空壳永远修不回来
        "--size-only",
        "--transfers", str(a.transfers),
        "--checkers", str(a.checkers),
        # 必须关掉 rclone 的单文件多线程上传(默认 4, >255MB 的文件自动启用):
        # 多线程会并发往**同一个** teldrive upload session 写分片, 服务端直接回
        # `409 (conflict): operation conflicts with current state`, 一轮几百次重试,
        # 310 分钟只跑出 45 GiB。关掉后单文件单流写, 吞吐回到稳定的 ~8MiB/s。
        "--multi-thread-streams", "0",
        "--teldrive-chunk-size", "%dMi" % a.chunk_mb,
        # 必须关掉 BLAKE3 树哈希: 它按 16MiB 块算, 而我们把分片压到 8Mi 后
        # 客户端/服务端算出的哈希对不上(rclone 报 corrupted on transfer)。
        # 关掉后只校验大小, 对视频冷备足够。
        "--teldrive-hash-enabled=false",
        "--retries", str(a.retries),
        # 求稳优先: 重试间隔拉到 30s。504 是 Unikraft 边缘抖动, 等一会儿基本都能过,
        # 急着 10s 重试反而又撞上去。
        "--retries-sleep", "30s",
        "--low-level-retries", "10",
        "--contimeout", "60s",
        "--stats", "1m",
        "-v",
    ]
    # 请求速率上限: 给 teldrive/Telegram 留余量, 避免被限流打成 504
    if a.tpslimit and a.tpslimit > 0:
        cmd += ["--tpslimit", str(a.tpslimit)]
    log("开跑: 预算 %d 分钟 / 并发 %d / 分片 %dMi / 重试 %d 次"
        % (a.budget_min, a.transfers, a.chunk_mb, a.retries))
    # rclone 的输出必须经 Python 的 stdout 走一道(而不是直接继承 fd),
    # 否则文件名/路径会原样落进公开仓库的 Actions 日志, logmask 拦不到。
    p = subprocess.Popen(cmd, cwd=WORK, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        print(line.rstrip("\n"), flush=True)
    rc = p.wait()
    if rc in (124, 130):
        log("时间预算到点, 优雅收工(下次继续) —— 这不是失败")
    elif rc != 0:
        log("!! rclone 退出码 %d —— 看上面日志" % rc)

    # ---- 复核 + 报告 ----
    dst_map2 = {}
    for it in lsjson(DST, allow_missing=True):
        if not it.get("IsDir"):
            dst_map2[it["Path"]] = int(it.get("Size", 0) or 0)

    done_dirs, todo_left = [], []
    for d, todo, need in plan:
        ok = [(p, s) for p, s in todo if dst_map2.get(p) == s]
        miss = [(p, s) for p, s in todo if dst_map2.get(p) != s]
        if miss:
            todo_left.append((d, len(miss), sum(s for _, s in miss)))
        else:
            done_dirs.append(d)
    log("本轮整目录完成 %d 个, 仍有残留 %d 个" % (len(done_dirs), len(todo_left)))

    lines = []
    lines.append("gdrive2 -> Teldrive 备份报告 (含视频文件夹)")
    lines.append("时间      : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("模式      : %s" % ("真跑" if a.apply else "演练"))
    lines.append("源端文件  : %d" % len(src_items))
    lines.append("含视频目录: %d 个" % len(video_dirs))
    lines.append("本轮入选  : %d 个目录 / %s" % (len(plan), human(planned_bytes)))
    lines.append("整目录完成: %d 个" % len(done_dirs))
    lines.append("仍有残留  : %d 个目录" % len(todo_left))
    lines.append("耗时      : %.1f 分钟" % ((time.time() - t0) / 60))
    lines.append("")
    lines.append("---- 未完成明细(下次会重挑) ----")
    for d, n, s in todo_left[:300]:
        lines.append("%s  (缺 %d 个 / %s)" % (d or "(根目录)", n, human(s)))
    rp = os.path.join(WORK, "report.txt")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    rr = subprocess.run([RCLONE, "copyto", rp, REPORT], capture_output=True, text=True)
    log("报告已写到 报告区: %s" % ("OK" if rr.returncode == 0 else "失败 " + rr.stderr.strip()[:200]))

    # ---- 出图/发 Telegram 用的统计 ----
    # 本轮新增 = 跑完后目标端有, 而开跑前没有(或大小不同)的文件
    new_files = [p for p in dst_map2 if dst_map.get(p) != dst_map2[p]]
    dump_stats({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "真跑", "apply": True,
        "event": os.environ.get("TD_EVENT", ""),
        "src_files": len(src_items),
        "video_dirs": len(video_dirs),
        "plan_dirs": len(plan), "plan_bytes": planned_bytes,
        "plan_detail": [{"dir": d or "(根目录)",
                         "n": len(todo), "bytes": need} for d, todo, need in plan[:8]],
        "dst_files": len(dst_map2), "dst_bytes": sum(dst_map2.values()),
        "new_files": len(new_files), "new_bytes": sum(dst_map2[p] for p in new_files),
        "done_dirs": len(done_dirs), "left_dirs": len(todo_left),
        "left_detail": [{"dir": d or "(根目录)", "n": n, "bytes": s}
                        for d, n, s in todo_left[:8]],
        "minutes": round((time.time() - t0) / 60, 1),
        "budget_min": a.budget_min, "chunk_mb": a.chunk_mb,
        "transfers": a.transfers, "rc": rc, "budget_hit": rc in (124, 130),
        "src": SRC, "dst": DST,
        "note": ("时间预算到点, 优雅收工(下次继续)" if rc in (124, 130)
                 else ("rclone 退出码 %d" % rc if rc != 0 else "")),
    })


if __name__ == "__main__":
    main()
