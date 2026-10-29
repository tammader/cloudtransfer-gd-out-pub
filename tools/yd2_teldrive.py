# -*- coding: utf-8 -*-
"""移动云盘(个人 + 家庭) -> teldrive 备份流水线 (GitHub Actions, 公开仓库)

为什么要"下载再上传":
  139 移动云盘只有国内 OpenAPI, rclone 没有它的 backend, 云端 rclone 直接读不到。
  但仓库里已有两个现役客户端(ty-transfer / ydy2-transfer 在用):
    * tools/ty_cloud_personal.py : 个人云(personal_new), 路由 personal-kd-njs.yun.139.com
    * tools/ty139.py            : 家庭云, yun.139.com 的 familyCloud rebuild 接口
  于是: 列源(OpenAPI) -> 取 CDN 直链 -> 落本地临时文件 -> rclone copyto 上传 teldrive。
  teldrive 侧必须用**补丁版 rclone**(minChunk 4Mi/smallchunk): 8Mi 分片 + 关 BLAKE3
  树哈希 + 关单文件多线程(否则同一 upload session 并发写 -> 409)。

脱敏(仓库是公开的, Actions 日志任何人可读):
  `logmask.install()` 把 stdout/stderr 包一层, 文件名/路径一律换成 <file:hash>/<path:hash>。
  同一串每次得到同一标签 -> 排障仍能对应, 但无法反推原名。

稳定性(大哥要求「稳定不赶时间」):
  * 每个文件独立: 失败重试后记录跳过, 一个坏文件绝不拖死整轮;
  * 断点幂等: 目标端按「大小一致」判断, 已传的永远跳过;
  * 时间预算 / 单轮总量 / 单文件上限 三重闸门, 到点优雅收工;
  * 只下当前这一个文件, 传完立刻删 -> 磁盘占用恒定 = 单个文件大小。

环境变量:
  YDY_AUTHORIZATION  : 个人云 authorization (必填, --disk personal)
  TY139_AUTH         : 家庭云 authorization (必填, --disk family)
  TY139_CLOUD_ID     : 家庭云 cloudID       (必填, --disk family)
  TD_STATS_JSON      : 可选, 统计 JSON 落点
用法:
  python yd2_teldrive.py --disk both --dry
  python yd2_teldrive.py --disk both --apply --budget-min 270
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import logmask                                    # noqa: E402
logmask.install()                                 # 日志脱敏: 必须在任何输出之前

from ty_cloud_personal import TyPersonal          # noqa: E402
from ty139 import Ty139                           # noqa: E402

WORK = os.environ.get("YD2_WORK", "/tmp/yd2work")
DST = os.environ.get("YD2_TD_DST", "teldrive:yd2-backup")
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
GB = 1024 ** 3
MB = 1024 ** 2

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv",
             ".m4v", ".ts", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp",
             ".vob", ".divx", ".f4v", ".asf")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def human(b):
    if b < MB:
        return "%.0f KB" % (b / 1024)
    return "%.1f MB" % (b / MB) if b < GB else "%.2f GB" % (b / GB)


def is_video(name):
    return (name or "").lower().endswith(VIDEO_EXT)


# ---------------- 两种盘的统一适配 ----------------
class SrcPersonal(object):
    """移动个人云 (personal_new)"""
    name = "personal"
    prefix = "personal"

    def __init__(self):
        auth = (os.environ.get("YDY_AUTHORIZATION") or "").strip()
        if not auth:
            raise SystemExit("!! 缺 YDY_AUTHORIZATION")
        self.c = TyPersonal(auth)
        if not self.c.login():
            raise SystemExit("!! 个人云 login 失败(凭据可能过期)")

    def root(self):
        return "/"

    def list(self, node):
        return self.c.list_dir(node)

    def link(self, it):
        d = self.c.get_link(it["id"])
        return d[0] if isinstance(d, (tuple, list)) else d


class SrcFamily(object):
    """移动家庭云 (family) —— 根目录 catalog_id 是空串, 不是 '/'"""
    name = "family"
    prefix = "family"

    def __init__(self):
        auth = (os.environ.get("TY139_AUTH") or "").strip()
        cid = (os.environ.get("TY139_CLOUD_ID") or "").strip()
        if not auth or not cid:
            raise SystemExit("!! 缺 TY139_AUTH / TY139_CLOUD_ID")
        self.c = Ty139(auth, cid, None)

    def root(self):
        return ""

    def list(self, node):
        return self.c.list_dir(node)

    def link(self, it):
        u, _ = self.c.get_link(it["id"], it.get("path") or "")
        return u


def collect(src, want_video_only=True):
    """递归列源盘 -> ([(相对路径, size, item)], 目录数)"""
    out, queue, ndirs = [], [(src.root(), "")], 0
    while queue:
        node, rel = queue.pop(0)
        try:
            items = src.list(node)
        except Exception as e:
            log("  !! 列目录失败 %s: %s" % (rel or "/", str(e)[:100]))
            continue
        ndirs += 1
        for it in items:
            name = it.get("name") or ""
            p = (rel + "/" + name) if rel else name
            if it.get("is_dir"):
                queue.append((it.get("id"), p))
                continue
            sz = int(it.get("size") or 0)
            if sz <= 0:
                continue
            if want_video_only and not is_video(name):
                continue
            it["_rel"] = p
            out.append((p, sz, it))
        if ndirs % 100 == 0:
            log("  已列 %d 个目录, 视频 %d 个" % (ndirs, len(out)))
    return out, ndirs


def collect_dst(base):
    """rclone 列目标端已有 -> {相对路径(去掉 base 前缀): size}"""
    r = subprocess.run(
        [RCLONE, "lsjson", base, "-R", "--files-only", "--no-modtime"],
        capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()
        if "not found" in err or "directory not found" in err:
            log("目标端 %s 还不存在 -> 首次备份会自动创建" % base)
            return {}
        log("!! 列目标端失败: %s" % err[:200])
        return {}
    try:
        items = json.loads(r.stdout or "[]")
    except Exception:
        return {}
    return {it["Path"]: int(it.get("Size") or 0) for it in items}


def fetch(src, it, size, dest, retries=3):
    for attempt in range(retries):
        try:
            url = src.link(it)
            if not url:
                raise RuntimeError("直链为空")
            got = 0
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(url, timeout=120) as r, open(dest, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    got += len(b)
            if got != size:
                raise RuntimeError("字节数不符 %d != %d" % (got, size))
            return got
        except Exception as e:
            log("     下载重试 %d/%d: %s" % (attempt + 1, retries, str(e)[:100]))
            time.sleep(3 + attempt * 5)
    return None


def upload(local, remote, retries=3):
    cmd = [
        RCLONE, "copyto", local, remote,
        "--teldrive-chunk-size", "8Mi",
        "--teldrive-hash-enabled=false",     # 8Mi 分片下树哈希必不匹配
        "--multi-thread-streams", "0",       # 多线程写同一 session -> 409
        "--transfers", "1", "--checkers", "1",
        "--retries", str(retries), "--retries-sleep", "30s",
        "--low-level-retries", "10", "--contimeout", "60s",
        "--stats", "0", "-v",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "")[-300:]
    return True, ""


def dump_stats(st):
    p = os.environ.get("TD_STATS_JSON", "")
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        log("统计 JSON 已写")
    except Exception as e:
        log("统计写入失败(不影响备份): %s" % str(e)[:80])


def run_one(src, a, t0, t_deadline=None, slice_s=None):
    """跑一个盘, 返回统计 dict

    t0        : 本盘预算起点 —— 调用方传**真实时钟**(time.time())。
    slice_s   : 本盘分到的秒数。给了就用它做预算, 不再信 a.budget_min
                (a.budget_min 现在表示整轮总预算, 由 main 切分)。
    t_deadline: 真实时钟下的硬截止, 到了无论如何停(留给收尾出图)
    """
    log("=" * 20 + " %s " % src.name + "=" * 20)
    base = "%s/%s" % (DST.rstrip("/"), src.prefix)
    ts = time.time()
    budget = slice_s if slice_s is not None else a.budget_min * 60
    src_items, ndirs = collect(src)
    log("源端视频 %d 个 / %s (%d 个目录, %.0fs)"
        % (len(src_items), human(sum(s for _, s, _ in src_items)), ndirs,
           time.time() - ts))

    dst = collect_dst(base)
    log("目标端已有 %d 个 (%s)" % (len(dst), human(sum(dst.values()))))

    max_file = int(a.max_file_gb * GB)
    todo, skip_big = [], 0
    for p, s, it in src_items:
        if s > max_file:
            skip_big += 1
            continue
        if dst.get(p) == s:
            continue
        todo.append((p, s, it))
    todo.sort(key=lambda x: -x[1])
    log("待传 %d 个 / %s (超限跳过 %d, 已备份 %d)"
        % (len(todo), human(sum(s for _, s, _ in todo)), skip_big,
           len(src_items) - len(todo) - skip_big))
    for p, s, _ in todo[:a.preview]:
        log("   + %s (%s)" % (p[:70], human(s)))
    if len(todo) > a.preview:
        log("   ... 其余 %d 个省略" % (len(todo) - a.preview))

    if not a.apply or not todo:
        return {"mode": "演练" if not a.apply else "真跑", "src_files": len(src_items),
                "src_bytes": sum(s for _, s, _ in src_items),
                "dst_files": len(dst), "dst_bytes": sum(dst.values()),
                "todo_files": len(todo), "todo_bytes": sum(s for _, s, _ in todo),
                "new_files": 0, "new_bytes": 0, "fail_files": 0}

    max_total = int(a.max_total_gb * GB)
    done_n = done_b = fail_n = 0
    tmpdir = os.path.join(WORK, "tmp_" + src.name)
    os.makedirs(tmpdir, exist_ok=True)

    for i, (p, s, it) in enumerate(todo, 1):
        if time.time() - t0 > budget:
            log("时间预算到点, 优雅收工(下次继续) —— 这不是失败")
            break
        if t_deadline and time.time() > t_deadline:
            log("撞到硬截止(留给收尾/出图), 优雅收工 —— 这不是失败")
            break
        if done_b >= max_total:
            log("本轮总量到上限 %s, 收工" % human(max_total))
            break
        local = os.path.join(tmpdir, "%d%s" % (i, os.path.splitext(p)[1] or ".bin"))
        log("[%d/%d] %s (%s)" % (i, len(todo), p[:70], human(s)))
        td0 = time.time()
        if fetch(src, it, s, local, retries=a.retries) is None:
            fail_n += 1
            log("   !! 下载失败, 跳过")
            continue
        tdl = time.time() - td0
        tu0 = time.time()
        ok, msg = upload(local, "%s/%s" % (base, p), retries=a.retries)
        tu = time.time() - tu0
        try:
            os.remove(local)
        except Exception:
            pass
        if not ok:
            fail_n += 1
            log("   !! 上传失败, 跳过: %s" % msg[:140])
            continue
        done_n += 1
        done_b += s
        log("   ok 下行 %.1fs(%.1f MiB/s) 上传 %.1fs(%.1f MiB/s) 累计 %s"
            % (tdl, s / MB / max(0.1, tdl), tu, s / MB / max(0.1, tu), human(done_b)))

    log("%s 本轮: 新传 %d 个 / %s, 失败 %d" % (src.name, done_n, human(done_b), fail_n))
    return {"mode": "真跑", "src_files": len(src_items),
            "src_bytes": sum(s for _, s, _ in src_items),
            "dst_files": len(dst), "dst_bytes": sum(dst.values()),
            "todo_files": len(todo), "todo_bytes": sum(s for _, s, _ in todo),
            "new_files": done_n, "new_bytes": done_b, "fail_files": fail_n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disk", default="both", choices=("personal", "family", "both"))
    ap.add_argument("--apply", action="store_true", help="不加=只列计划")
    ap.add_argument("--budget-min", type=int, default=270,
                    help="整轮总预算(含列目录), 两盘共用这条总账")
    ap.add_argument("--max-file-gb", type=float, default=2.0)
    ap.add_argument("--max-total-gb", type=float, default=150.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--preview", type=int, default=8)
    ap.add_argument("--reserve-min", type=int, default=15,
                    help="留给出图/发TG/收尾的尾部时间, 不进传输预算")
    ap.add_argument("--min-slice-min", type=int, default=45,
                    help="后跑的盘至少保证这么多分钟, 不够就优先它")
    ap.add_argument("--rotate", default="auto", choices=("auto", "personal", "family", "none"),
                    help="先跑哪个盘; auto=按偶数/奇数轮交替, 避免同一盘长期饿死对方")
    a = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()
    log("目标 : %s" % DST)
    log("模式 : %s / 盘: %s" % ("真跑(--apply)" if a.apply else "演练(只列计划)", a.disk))

    srcs = []
    if a.disk in ("personal", "both"):
        srcs.append(SrcPersonal())
    if a.disk in ("family", "both"):
        srcs.append(SrcFamily())

    # ---- 轮换: personal 每轮都能吃满预算, family 就会被永久饿死(10-26 之后就是这个症状) ----
    rotate = a.rotate
    if rotate == "auto" and len(srcs) > 1:
        # 用"当天第几个 6 小时窗口"做奇偶, 不需要额外状态文件
        slot = int(time.time() // (6 * 3600))
        rotate = "family" if slot % 2 == 0 else "personal"
        log("轮换: 本窗口先跑 %s (slot=%d)" % (rotate, slot))
    if rotate in ("personal", "family") and len(srcs) > 1:
        srcs.sort(key=lambda s: 0 if s.name == rotate else 1)
    log("执行顺序: %s" % " -> ".join(s.name for s in srcs))

    # ---- 预算: 总额 a.budget_min, 先扣掉尾部预留, 再在两盘之间分配 ----
    total_deadline = t0 + a.budget_min * 60
    hard_deadline = total_deadline - a.reserve_min * 60
    transfer_budget = max(0.0, a.budget_min - a.reserve_min) * 60

    stats = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "apply": bool(a.apply),
             "disks": {}}
    # 每盘的实际结束时刻(真实时钟)。它只增不减, 所以两盘的总时长天然 <= hard_deadline,
    # 不管上一盘是跑满还是提前收工 —— 这是"不超发"的唯一可靠保证。
    wall_cursor = t0
    for idx, src in enumerate(srcs):
        # left_wall: 从"上一盘实际结束点"到硬截止, 还剩多少秒
        left_wall = max(0.0, hard_deadline - wall_cursor)
        remaining_disk = len(srcs) - idx
        if remaining_disk == 1:
            slice_s = left_wall                 # 最后一个盘把真实剩下的全拿走
        else:
            # 后面的盘至少留 min_slice_min; 当前盘最多用 剩下的 - 保底
            keep = (remaining_disk - 1) * a.min_slice_min * 60
            slice_s = max(0.0, min(left_wall - keep, left_wall * 0.6))
        slice_s = min(slice_s, left_wall)

        if slice_s < 60:
            log("!! %s 分到的预算是 %.0f 分钟, 不足 1 分钟 -> 本轮跳过它(下轮轮换会先跑它)"
                % (src.name, slice_s / 60))
            stats["disks"][src.name] = {
                "mode": "真跑" if a.apply else "演练", "src_files": 0, "src_bytes": 0,
                "dst_files": 0, "dst_bytes": 0, "todo_files": 0, "todo_bytes": 0,
                "new_files": 0, "new_bytes": 0, "fail_files": 0,
                "skipped_reason": "budget_slice_too_small"}
            continue

        log("%s 分到预算 %.0f 分钟 (整轮 %d 分, 尾部预留 %d 分)"
            % (src.name, slice_s / 60, a.budget_min, a.reserve_min))
        # t0 传真实时钟, 预算用 slice_s 显式给, 两者不再混淆
        disk_deadline = min(time.time() + slice_s, hard_deadline)
        stats["disks"][src.name] = run_one(
            src, a, t0=time.time(), t_deadline=disk_deadline, slice_s=slice_s)
        # 游标推进到"本盘实际结束点": 跑满了就是 disk_deadline, 提前收工就是现在
        wall_cursor = max(wall_cursor, min(time.time(), disk_deadline))

    tot = {"new_files": 0, "new_bytes": 0, "todo_files": 0, "todo_bytes": 0,
           "fail_files": 0}
    for v in stats["disks"].values():
        for k in tot:
            tot[k] += v.get(k, 0)
    stats.update(tot)
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    log("合计: 新传 %d 个 / %s, 失败 %d, 用时 %.1f 分"
        % (tot["new_files"], human(tot["new_bytes"]), tot["fail_files"],
           stats["minutes"]))
    dump_stats(stats)


if __name__ == "__main__":
    main()
