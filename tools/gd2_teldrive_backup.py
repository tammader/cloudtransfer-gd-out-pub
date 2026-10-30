# -*- coding: utf-8 -*-
"""gdrive2 -> Teldrive 冷备份: 自动找出「含视频的文件夹」, 整个文件夹备份

扫描逻辑(按文件夹为单位, 不是按单个文件):
  1) 列出 源(gdrive2) 全部文件, 按所在目录分组
  2) 只要某个目录里有视频文件, 这个目录就入选(默认连目录里的字幕/图片一起传)
  3) 目标端已有且同名同大小的文件跳过; 整个目录都齐了就跳过这个目录
  4) 按本轮总量预算(--max-transfer-gb)挑若干个目录, 到量收工下次继续
  5) 目标端保持原目录结构: teldrive:gd2-backup/<原目录相对路径>/...
  6) 单文件超过 --max-file-gb(默认 1.5) 的直接跳过并列进报告 —— 见下方说明

为什么要有单文件上限(2026-10-30):
  这条 Teldrive 管道实测 ~125 KiB/s, 单文件传完所需时间 = 大小 / 125KiB/s:
      1.5 GB -> 3.4h(能完)   2 GB -> 4.7h(超预算)   3 GB -> 7.0h(永远传不完)
  而每轮预算只有 270~330 分钟。传不完不会"下次接着传": rclone 每轮都是全新
  copy, 半截 session 被 clear_open_sessions() 清掉, 下轮 --size-only 又不匹配,
  于是从第 0 字节重来。更糟的是 pending 按待传量降序, 这种大文件所在目录会
  一直排最前, 每轮把预算吃光, 小文件永远轮不到。
  所以超限文件必须跳过并显式列出, 不能让它悄悄占着队首。

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
STAGE = os.path.join(WORK, "stage")

# 视频扩展名(判到「末尾」, 所以 xxx.part001.mp4 这种切分名也算)
VIDEO_RE = re.compile(
    r"\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpg|mpeg|m2ts|ts|rmvb|rm|3gp|vob|divx|f4v|asf)$",
    re.I)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def mask_dir(d):
    """裸目录名没有扩展名/远端前缀, logmask 的正则抓不到, 这里显式换成 <path:hash>。
    公开仓库的日志任何人可读, 目录名也是隐私。"""
    if not d:
        return "(根目录)"
    try:
        return logmask._tag("path", d)
    except Exception:
        return d


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


# ---------------------------------------------------------------- 传输(落地模式)
#
# 为什么必须"先落地再传"(2026-10-22 定论, 有对照实验):
#   原来是 rclone copy gdrive2: teldrive: **流式直传**, 读端(Google)和写端(Teldrive)
#   死绑在一起。Teldrive 一个 8Mi 分片要几百 ms~几十秒, 写端一慢就反压读端, Google
#   的下载流被拖到没数据可送 -> Unikraft 边缘网关 60s 收不到东西 -> 504 -> rclone 判定
#   整个文件失败, 从第 0 字节重传。450MB 的文件要 57 个分片, 中途必挂, 永远到不了终点。
#   实测 run #13: 4 小时 / 475 次 504 / 完成 0 个文件 / 速度 200~900 KiB/s。
#
#   同一时刻 yd2-teldrive 用「先下载到本地磁盘, 再本地->Teldrive, transfers=1」
#   对**同一台实例**报 `新传 8 个 / 15.16 GB, 失败 0`。
#   -> 问题不在 Teldrive 本身, 在流式这种传法。本文件照抄已验证的那条路。
#
# 分批而不是一次性全落地: runner 磁盘只有 ~86GB 可用, 而单轮计划可能上百 GB。
# 每批 --stage-batch-gb(默认 10GB): 下载 -> 上传 -> 删本地, 再来下一批。


def _rclone(args, budget_sec, tag):
    """跑一条 rclone(参数里已含子命令), 用 timeout 包住, 输出经 Python stdout 过脱敏。

    返回 rclone 退出码; 124/130 = 时间到被 timeout 收走(不算失败)。
    """
    cmd = ["timeout", "--signal=INT", "--kill-after=120s",
           "%ds" % max(60, int(budget_sec)), RCLONE] + args
    log("%s: %s ..." % (tag, " ".join(args[:2])))
    p = subprocess.Popen(cmd, cwd=WORK, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        print(line.rstrip("\n"), flush=True)
    return p.wait()


def _download_args(a, files_from):
    """拉 gdrive2 -> 本地磁盘。读本地盘是"即时"的, 可以开点并发吃满带宽。"""
    return [
        "copy", SRC, STAGE,
        "--files-from", files_from,
        "--size-only",
        "--transfers", str(max(1, min(a.transfers, 4))),
        "--checkers", "16",
        "--retries", "5", "--retries-sleep", "15s",
        "--low-level-retries", "10", "--contimeout", "60s",
        "--stats", "1m", "-v",
    ]


def _upload_args(a, files_from):
    """本地磁盘 -> Teldrive。transfers 固定 1, 串行写, 跟 yd2 那条验证过的路一致。"""
    args = [
        "copy", STAGE, DST,
        "--files-from", files_from,
        # 用 --size-only: 目标端同大小就跳过, 大小不同(含 0 字节空壳)就覆盖;
        # 不能用 --ignore-existing —— 那会让 0 字节空壳永远修不回来
        "--size-only",
        "--transfers", "1",
        "--checkers", "1",
        # 必须关掉单文件多线程上传(默认 4, >255MB 自动启用): 多线程并发往**同一个**
        # teldrive upload session 写分片, 服务端直接回 409 (conflict)。
        "--multi-thread-streams", "0",
        "--teldrive-chunk-size", "%dMi" % a.chunk_mb,
        # 必须关掉 BLAKE3 树哈希: 它按 16MiB 块算, 分片压到 8Mi 后客户端/服务端
        # 算出的哈希对不上(rclone 报 corrupted on transfer)。只校验大小足够。
        "--teldrive-hash-enabled=false",
        "--retries", str(a.retries),
        # 504 是边缘抖动, 等一会儿基本都能过; 急着重试反而又撞上去
        "--retries-sleep", "30s",
        "--low-level-retries", "10",
        "--contimeout", "60s",
        "--stats", "1m", "-v",
    ]
    if a.tpslimit and a.tpslimit > 0:
        args += ["--tpslimit", str(a.tpslimit)]
    return args


def _prune_empty(root):
    """删掉 stage 里的空目录, 免得越攒越多把磁盘塞满"""
    for d, _, _ in os.walk(root, topdown=False):
        if d == root:
            continue
        try:
            os.rmdir(d)
        except OSError:
            pass          # 非空/被占用都无所谓


def _stage_files(root):
    """列出 stage 里的文件: [(绝对路径, 大小, mtime)], 按 mtime 从旧到新。"""
    out = []
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((p, st.st_size, st.st_mtime))
    out.sort(key=lambda x: x[2])
    return out


def _trim_stage(a):
    """按磁盘压力清理 stage 里上一轮留下的本地副本。

    为什么不"收工就清"(2026-10-30 定论, 方案 Y):
      run 40982196440 里 0791ebde.mkv 传到 80% 时预算到点, 旧代码把整批
      `os.remove` 掉 —— 59 分钟的下载白做, 下轮还得从 Google Drive 重下。
      而 Teldrive 的半截 session 本来就不可续(clear_open_sessions 会清掉),
      所以"保留本地文件"救不回远端进度, 但能省掉一次完整重下, 而且让
      下轮 --size-only 对已传完的文件直接跳过, 不做无意义的重复上传。

    清理规则(只在真正紧张时动手):
      * 可用磁盘 >= KEEP_LOW_GB   -> 什么都不做
      * 否则从最旧的文件开始删, 直到可用磁盘回到 KEEP_TARGET_GB
    这样常态下文件会留到下一轮被复用, 只有磁盘吃紧时才牺牲最旧的那批。
    """
    KEEP_LOW_GB = 20.0
    KEEP_TARGET_GB = 30.0
    free = shutil.disk_usage(WORK).free
    if free >= KEEP_LOW_GB * GB:
        return
    files = _stage_files(STAGE)
    if not files:
        return
    log("磁盘只剩 %s (< %.0fGB), 清理 stage 旧文件"
        % (human(free), KEEP_LOW_GB))
    freed = 0
    for p, sz, _ in files:
        if shutil.disk_usage(WORK).free >= KEEP_TARGET_GB * GB:
            break
        try:
            os.remove(p)
            freed += sz
        except OSError:
            pass
    _prune_empty(STAGE)
    log("  清掉 %s, 磁盘剩 %s" % (human(freed), human(shutil.disk_usage(WORK).free)))


def run_staged(a, plan, budget_sec, t0):
    """落地模式主循环: 分批 下载 -> 上传 -> 删本地。返回 (退出码, 批数, 实做量)。

    批大小不是固定值, 每批开跑前按「--stage-batch-gb」和「当前可用磁盘 * 0.6」
    取较小者现算 —— 前面某批失败留下残file 时, 后面的批次会自动缩量而不是撑爆磁盘。

    第三个返回值是**本轮真正切进批里的文件数和字节数**, 不是 plan 的全量:
    进度条的分母必须用它。旧代码拿 plan_bytes(整轮计划 2912 GB)当分母,
    实际只做几十 GB, 进度永远停在 5% 附近, 看着像卡死。
    """
    os.makedirs(STAGE, exist_ok=True)
    # 先看上一轮留下了什么: 常态下不动它(下轮直接复用), 磁盘吃紧才清旧的
    kept = _stage_files(STAGE)
    if kept:
        log("stage 里有上一轮留下的 %d 个文件 / %s"
            % (len(kept), human(sum(x[1] for x in kept))))
    _trim_stage(a)
    todo_all = [(p, s) for _, todo, _ in plan for p, s in todo]
    deadline = t0 + budget_sec
    rc_all, n_batch, i = 0, 0, 0
    did_bytes, did_files = 0, 0
    log("开跑: 落地模式(下载->上传) / 预算 %d 分钟 / 每批 %.0fGB / 分片 %dMi / 上传串行"
        % (a.budget_min, a.stage_batch_gb, a.chunk_mb))

    while i < len(todo_all):
        left = deadline - time.time()
        if left < 180:
            log("剩余 %.0f 分钟, 不够再下一批, 收工(下次继续)" % (left / 60))
            break

        free = shutil.disk_usage(WORK).free
        # 磁盘兜底: 连下一个文件都装不下就收工(不是按"批次够不够 1GB"判断 —
        # 单个大文件独占一批时本来就可能接近这个量级)
        nxt = todo_all[i][1]
        if free < max(nxt * 1.2, 200 * MB):
            log("磁盘只剩 %s, 装不下下一个文件(%s), 收工"
                % (human(free), human(nxt)))
            break
        cap = min(a.stage_batch_gb * GB, free * 0.6)

        # 磁盘小的时候把批切碎, 不要"一批装下所有能装的东西"。
        # 2026-10-27 run 40595914103: 磁盘只剩 7.68 GB -> cap 4.6 GB -> 一批 8 个文件,
        # 8 个里有 7 个卡在 504, 整轮 270 分钟就耗在这一批上, 最后只成 1 个。
        # 而 run 40696898122 磁盘 87 GB, 切成 3 批 58 个文件, 撞了 504 就换下一批,
        # 那些文件下一批重传时基本都过了(日志里多次 "Attempt 2/5 succeeded")。
        # 结论: 批数比批大小重要 —— 磁盘小时宁可 1GB 一批, 也要留出多轮换血的机会。
        SHRUNK_CAP = 1024.0 * 1024 * 1024          # 1GB
        # 判据是"磁盘装不下一个正常批", 不是"cap 被压小了":
        # cap 被压小可能是磁盘真紧张(405: 7.68GB -> 4.6GB), 也可能只是标称值
        # 本来就没顶满(406: 87GB 磁盘, cap = 10GB 标称, 但没必要再切碎)。
        if free < a.stage_batch_gb * 3 * GB and free >= 4 * SHRUNK_CAP:
            # 磁盘只够装两三个正常批时切碎, 换取多批换血;
            # 再小(<4GB)就别切了 —— cap 会小到装不下单个文件, 那批永远切不出来。
            log("磁盘偏小(%s), 批大小 %s -> %s 以保留多批换血"
                % (human(free), human(cap), human(SHRUNK_CAP)))
            cap = min(cap, SHRUNK_CAP)

        # 从游标位置往后切一批
        batch, acc = [], 0
        while i < len(todo_all):
            p, s = todo_all[i]
            if batch and acc + s > cap:
                break
            batch.append((p, s))
            acc += s
            i += 1
        if not batch:
            break

        n_batch += 1
        did_bytes += acc
        did_files += len(batch)
        bf = os.path.join(WORK, "batch_%03d.txt" % n_batch)
        with open(bf, "w", encoding="utf-8") as f:
            for p, _ in batch:
                f.write(p + "\n")
        log("---- 第 %d 批: %d 个文件 / %s (磁盘可用 %s) ----"
            % (n_batch, len(batch), human(acc), human(free)))

        # 1) 下载: 给三成剩余时间(实测 Google -> runner 比 Teldrive 写快一个量级)
        #    本地已经有的(上一轮留下的)不再重复下载 —— 那是纯粹的时间浪费。
        #    大小对得上才算可复用: 半截文件不能直接传, 会让 Teldrive 收到残缺数据。
        have, miss = [], []
        for p, s in batch:
            full = os.path.join(STAGE, p)
            try:
                ok = os.path.isfile(full) and os.path.getsize(full) == s
            except OSError:
                ok = False
            (have if ok else miss).append(p)
        if have:
            have_set = set(have)
            have_bytes = sum(sz for p, sz in batch if p in have_set)
            log("   本地已有 %d/%d 个完整文件, 跳过下载: %s"
                % (len(have), len(batch), human(have_bytes)))
        if miss:
            with open(bf, "w", encoding="utf-8") as f:
                for p in miss:
                    f.write(p + "\n")
            dl_budget = min(left * 0.35, max(300.0, acc / GB * 8))
            rc1 = _rclone(_download_args(a, bf), dl_budget, "下载")
            if rc1 not in (0, 124, 130):
                log("!! 下载退出码 %d, 照旧往下传(已落地的会传, 没落地的下轮重来)" % rc1)
        else:
            log("   这批全部在本地, 不用下载")

        # 2) 上传 —— 只为真正落地的文件构造清单: 下载失败的那些不在里面,
        #    免得 rclone 对着不存在的源文件报 error reading source, 把退出码搞脏。
        landed = [p for p, _ in batch if os.path.isfile(os.path.join(STAGE, p))]
        if not landed:
            log("!! 这批一个文件都没落地(下载全失败), 跳过上传")
            _prune_empty(STAGE)
            continue
        uf = os.path.join(WORK, "up_%03d.txt" % n_batch)
        with open(uf, "w", encoding="utf-8") as f:
            for p in landed:
                f.write(p + "\n")
        left2 = deadline - time.time()
        if left2 < 120:
            log("下载吃完预算, 收工(下次继续)")
            rc_all = 124
            break
        # 给整批留满剩余预算, 不要按文件数封顶。
        # 2026-10-27 run 40595914103 的教训是反过来的: 那一轮 8 个文件里 7 个卡在
        # 504, 需要的就是"慢慢磨到成功"(de992058 磨了 1h44m 才 Copied)。
        # 按文件数给每批封顶会让这批更早放弃, 把本来能成的也丢掉。
        rc2 = _rclone(_upload_args(a, uf), left2, "上传")
        if rc2 in (124, 130):
            log("上传到点, 优雅收工(下次继续) —— 这不是失败")
            log("本地副本全部保留(%d 个), 下轮直接复用, 不重复下载"
                % len(batch))
            rc_all = rc2
            break
        if rc2 != 0:
            rc_all = rc2

        # 3) 不再删本地副本 —— 见 _trim_stage 的说明(方案 Y, 2026-10-30)。
        #    已传完的: 下轮 --size-only 会跳过, 不做重复上传;
        #    没传完的: 下轮重新传, 但省掉一次从 Google Drive 的完整下载。
        #    真正需要腾磁盘时由下次开跑的 _trim_stage 按压力统一处理。
        log("本批完成, 本地保留 %d 个(下轮复用), 磁盘剩 %s"
            % (len(batch), human(shutil.disk_usage(WORK).free)))

    return rc_all, n_batch, {"bytes": did_bytes, "files": did_files}


def run_stream(a, plan, budget_sec):
    """老的流式直传(gdrive2 -> teldrive 边读边写), 保留只为对照 / 回退。

    ⚠️ 2026-10-22 实测不可用: 一小时 475 次 504, 完成 0 个文件。
    原因见本文件「传输(落地模式)」那段注释。别在定时任务里用它。
    """
    ff = os.path.join(WORK, "files.txt")
    with open(ff, "w", encoding="utf-8") as f:
        for _, todo, _ in plan:
            for p, _ in todo:
                f.write(p + "\n")
    log("!! 流式直传(对照模式): 预算 %d 分钟 / 并发 %d / 分片 %dMi"
        % (a.budget_min, a.transfers, a.chunk_mb))
    args = [
        "copy", SRC, DST,
        "--files-from", ff,
        "--size-only",
        "--transfers", str(a.transfers),
        "--checkers", str(a.checkers),
        "--multi-thread-streams", "0",
        "--teldrive-chunk-size", "%dMi" % a.chunk_mb,
        "--teldrive-hash-enabled=false",
        "--retries", str(a.retries),
        "--retries-sleep", "30s",
        "--low-level-retries", "10",
        "--contimeout", "60s",
        "--stats", "1m", "-v",
    ]
    if a.tpslimit and a.tpslimit > 0:
        args += ["--tpslimit", str(a.tpslimit)]
    rc = _rclone(args, budget_sec, "流式直传")
    if rc in (124, 130):
        log("时间预算到点, 优雅收工(下次继续) —— 这不是失败")
    elif rc != 0:
        log("!! rclone 退出码 %d —— 看上面日志" % rc)
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="不加=只演练看计划")
    ap.add_argument("--max-file-gb", type=float, default=1.5,
                    help="单文件大小上限(GB), 默认 1.5。超过的直接跳过并写进报告。"
                         "依据: 实测管道 ~125KiB/s, 1.5GB 需 3.4h(能传完), "
                         "2GB 需 4.7h(超过 270min 预算传不完), 3GB 需 7h(永远传不完)。"
                         "超限文件每轮都会重传且必然失败, 还会占住队首吃光预算, "
                         "所以宁可跳过。设 0 = 不限制(不推荐)")
    ap.add_argument("--max-transfer-gb", type=float, default=150.0,
                    help="本轮总量上限(GB); 实测 ~8MiB/s, 270 分钟约 130GB, 默认 150 够用")
    ap.add_argument("--budget-min", type=int, default=270,
                    help="本轮时间预算(分钟)。GitHub job 硬上限 350, 留 80 分钟给收尾"
                         " —— 2026-10-19 有一轮卡到 350 被强杀, 整轮白跑")
    ap.add_argument("--max-dirs", type=int, default=0, help="本轮最多处理几个文件夹; 0=不限")
    ap.add_argument("--transfers", type=int, default=3,
                    help="并发文件数。求稳默认 3(Telegram 侧并发高容易 504/限流)")
    ap.add_argument("--checkers", type=int, default=8)
    ap.add_argument("--chunk-mb", type=int, default=4,
                    help="Teldrive 分片(MB)。默认 4。"
                         "硬上限是 Unikraft 网关: 8MiB 是请求体上限(9Mi 起 413),"
                         "但真正的杀手是**60 秒无数据超时** —— 一片必须在 60s 内传完,"
                         "否则边缘网关回 504、rclone 从第 0 字节重传整个文件。"
                         "换算: 需要的最低速率 = 分片MiB * 1024 / 60 KiB/s,"
                         "即 4Mi 需 68 KiB/s、6Mi 需 102 KiB/s、8Mi 需 136 KiB/s。"
                         "2026-10-30 实测: 8Mi 在 ~133 KiB/s 下每个文件都死在 ~22MiB(4%)"
                         "反复 504, 整轮 12 个文件成 0; 同一条管道同一天改 4Mi 后"
                         "单文件连续推进 17 分钟无一次 504。"
                         "所以 8Mi 在这条管道上不是'临界'而是'必挂', 6Mi 只剩 1.3 倍余量"
                         "也会抖; 4Mi 有 2 倍余量。"
                         "注意调小不会变慢: 瓶颈是 Telegram 管道的带宽, 不是分片大小,"
                         "4Mi 时每片只需 30s, 远没到超时, 放大分片省不到时间只增加风险。")
    ap.add_argument("--retries", type=int, default=5,
                    help="失败重试次数; 求稳可以调大(默认 5, 配 30s 间隔)")
    ap.add_argument("--tpslimit", type=float, default=10.0,
                    help="每秒最多 API 请求数, 给 teldrive/Telegram 留余量(0=不限)")
    ap.add_argument("--no-clear-sessions", action="store_true",
                    help="不清理上一轮残留的 open upload session(默认会清, 防 409 冲突)")
    ap.add_argument("--no-stage", action="store_true",
                    help="关掉落地模式, 回到 gdrive2 -> teldrive 流式直传"
                         " —— 2026-10-22 实测流式必 504(475 次/0 完成), 只留作对照, 别用")
    ap.add_argument("--stage-batch-gb", type=float, default=10.0,
                    help="落地模式每批大小(GB): 下载这么多 -> 传上去 -> 删本地, 再下一批。"
                         "默认 10; runner 磁盘约 86GB 可用, 脚本还会按实际空闲自动缩量")
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
    # 单文件上限: 超过 max_file_bytes 的直接跳过, 并单独记下来列进报告。
    # 这些文件在这条管道上永远传不完(见文件头说明), 留着只会每轮重传、吃光预算、
    # 还把 pending 排序顶到最前, 让小文件饿死。
    max_file_bytes = int(a.max_file_gb * GB) if a.max_file_gb and a.max_file_gb > 0 else 0
    skipped_big = []          # [(path, size)] 本轮因超限被跳过的文件

    pending = []
    for d in video_dirs:
        if a.only and d != a.only:
            continue
        e = video_dirs[d]
        files = e["files"] if not a.video_only else [x for x in e["files"] if is_video(x[0])]
        todo = [(p, s) for p, s in files if dst_map.get(p) != s]
        if max_file_bytes:
            kept = []
            for p, s in todo:
                if s > max_file_bytes:
                    skipped_big.append((p, s))
                else:
                    kept.append((p, s))
            todo = kept
        if not todo:
            continue                       # 这个文件夹已经全备份过了
        pending.append((d, todo, sum(s for _, s in todo)))
    pending.sort(key=lambda x: (-x[2], x[0]))

    if skipped_big:
        skipped_big.sort(key=lambda x: -x[1])
        log("!! 单文件超过 %.2f GB 而跳过 %d 个 / %s (这段速率下传不完, 列进报告)"
            % (a.max_file_gb, len(skipped_big),
               human(sum(s for _, s in skipped_big))))
        for p, s in skipped_big[:a.preview]:
            log("   - %s (%s)" % (mask_dir(p), human(s)))
        if len(skipped_big) > a.preview:
            log("   ... 其余 %d 个省略" % (len(skipped_big) - a.preview))

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
        log("   + %s  (%d 个文件/其中视频 %d, %s)" % (mask_dir(d), len(todo), vids, human(need)))
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

    # ---- 统计基准: 必须在「删空壳 + 清 session」之后重取 ----
    # 为什么不能直接用上面的 dst_map: 那个清单是在删空壳之前建的, 空壳被删掉后
    # 又重建, 大小从 0 变真值, 会被误算成"本轮新增"; 更糟的是删掉后本轮没覆盖到的
    # 空壳会从 lst 里消失, dst_files 反而下降 —— 两个数字一起失真。
    # (2026-10-28 run 40696898122: 实传 58 个文件, 但按旧基准算出来只有 1 个)
    base_map = {}
    for it in lsjson(DST, allow_missing=True):
        if not it.get("IsDir"):
            base_map[it["Path"]] = int(it.get("Size", 0) or 0)
    log("统计基准(清空壳/清 session 之后): %d 个" % len(base_map))

    # ---- 磁盘预检: 落地模式要先把文件下到本地, 磁盘不够就别硬切批 ----
    # 2026-10-27 run 40595914103: runner 只剩 7.68 GB, cap 被压到 4.6 GB,
    # 第一批塞 4.09 GB 后撞上 504 风暴, 拖到预算耗尽, 整轮只啃完一批。
    free_now = shutil.disk_usage(WORK).free
    if free_now < 8 * GB:
        raise SystemExit(
            "!! runner 磁盘只有 %.1f GB 可用, 落地模式至少要 8 GB\n"
            "   先清 /tmp 或换更大磁盘再跑; 不要用 --stage-batch-gb 硬顶" % (free_now / GB))
    log("磁盘预检: 可用 %s (OK)" % human(free_now))

    # ---- 真跑 ----
    budget_sec = a.budget_min * 60
    did = {"bytes": 0, "files": 0}
    if a.no_stage:
        rc = run_stream(a, plan, budget_sec)
    else:
        rc, n_batch, did = run_staged(a, plan, budget_sec, t0)
        log("落地模式跑完 %d 批 / 实切 %d 个文件 %s"
            % (n_batch, did["files"], human(did["bytes"])))

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
    if skipped_big:
        lines.append("")
        lines.append("---- 超限跳过(> %.2f GB, 当前速率下传不完) ----" % a.max_file_gb)
        for p, s in skipped_big[:100]:
            lines.append("%s  (%s)" % (p, human(s)))
        if len(skipped_big) > 100:
            lines.append("... 其余 %d 个省略" % (len(skipped_big) - 100))
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
    # 本轮新增 = 跑完后目标端有、且大小 > 0，而**统计基准**(清空壳/清 session 之后)
    # 里没有或大小不同的文件。
    #   - 用 base_map 而不是 dst_map: dst_map 是删空壳之前的清单, 空壳 0 字节 ->
    #     真值会被当成新增(假涨); 空壳删掉后本轮没覆盖到的, 会从 lst 里消失,
    #     dst_files 反而下降(假跌)。两个假象一起把差值压到只剩 1 个。
    #   - 排除 size == 0: 本轮没传完的 0 字节壳不算"新增"。
    new_files = [p for p in dst_map2
                 if dst_map2[p] > 0 and base_map.get(p, -1) != dst_map2[p]]
    new_bytes = sum(dst_map2[p] for p in new_files)
    # 顺带把"目标端累计"跟基准分开报, 涨跌一眼能看出来
    dst_delta = len(dst_map2) - len(base_map)
    lost = [p for p in base_map if p not in dst_map2]
    log("本轮新增 %d 个 / %s; 目标端 %d -> %d (%+d); 消失 %d 个"
        % (len(new_files), human(new_bytes), len(base_map), len(dst_map2),
           dst_delta, len(lost)))
    dump_stats({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": ("真跑-流式" if a.no_stage else "真跑-落地"), "apply": True,
        "event": os.environ.get("TD_EVENT", ""),
        "src_files": len(src_items),
        "video_dirs": len(video_dirs),
        "plan_dirs": len(plan), "plan_bytes": planned_bytes,
        "plan_detail": [{"dir": d or "(根目录)",
                         "n": len(todo), "bytes": need} for d, todo, need in plan[:8]],
        "dst_files": len(dst_map2), "dst_bytes": sum(dst_map2.values()),
        "base_files": len(base_map), "dst_delta": dst_delta, "lost_files": len(lost),
        "new_files": len(new_files), "new_bytes": new_bytes,
        # 进度条分母: 本轮真正切进批的量, 不是 plan 的全量(2912 GB)。
        # 旧版拿 plan_bytes 当分母, 实际只做几十 GB, 进度永远停 5%。
        "run_files": did["files"], "run_bytes": did["bytes"],
        "skipped_big": len(skipped_big),
        "skipped_big_bytes": sum(s for _, s in skipped_big),
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
