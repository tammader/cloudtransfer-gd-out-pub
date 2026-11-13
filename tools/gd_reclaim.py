# -*- coding: utf-8 -*-
"""gd2(源盘) -> gd6(备份盘)「已备份文件」回收: 只在目标盘存在同名同大小副本时, 才删源盘那一个。

背景
  gd-backup 每 12 小时把 源盘:{out,out3,TG目录} 目录级 copy 到 目标盘同名目录(源端只读)。
  镜像跑完之后, 源盘上同一批文件就是冗余的; 源盘配额吃紧(4.36T/5T)时可以把这份删掉。

为什么删了没事(每条都有代码守着)
  1) 只处理 GB_DIRS 登记的目录 —— 与 gd-backup 完全同源, 不碰未镜像的目录;
  2) 逐文件比对: **目标盘同名且字节数一致** 才删; 目标缺失 / 大小不符 / 取不到大小 -> 一律留下;
     (判据与 gd-backup 自己的「同名同大小 = 已备份」完全一致)
  3) 目录级兜底: 目标盘该目录列不到文件(权限/配额/网络) -> 整个目录跳过, 一个都不删;
  4) 默认跳过队列目录(--queue-dirs, 默认 out,out2): out 是 gd-out2 的输入队列,
     文件走完流程才会被服务端 move 到 out2 —— 删队列目录等于让没走完的内容从流水线上消失;
     确实要连队列一起清, 得显式加 --include-queue;
  5) 默认演练(dry-run); --apply 才真删;
  6) 删除默认**不进回收站**(--drive-use-trash=false): Google Drive 回收站里的文件照样占配额,
     想真正腾出空间只能这么删; 想留 30 天反悔期就加 --use-trash;
  7) 删完复核: 重列目标盘, 报「确认还在」的比例 + about 的 Used 变化。

配置(pathcfg: CI=Secret PATHS_JSON / 本机 paths.local.json)
  GB_SRC_ROOT / GB_DST_ROOT / GB_DIRS   —— 与 gd-backup 同源(不重复配置)
  GRC_REPORT(可选)                      —— 报告落点(rclone 路径, 目录级)
环境
  TG_TOKEN / TG_CHAT_ID(可选) —— 发摘要;  LOG_MASK=1 时日志脱敏

用法
  python3 tools/gd_reclaim.py                     # 演练: 只报「可删 X 个 / Y GB」
  python3 tools/gd_reclaim.py --apply             # 真删(跳过回收站)
  python3 tools/gd_reclaim.py --apply --use-trash # 真删但进回收站
  python3 tools/gd_reclaim.py --audit             # 顺带打印「谁在读这个源盘」的交叉核对
"""
import argparse
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

import pathcfg                                    # noqa: E402  路径真值: CI=Secret / 本机=paths.local.json

# 冷备那条流程的 key(GT_*)单独放在第二个 Secret, 合并进来, 好做交叉核对
_td = os.environ.get("PATHS_JSON_TD", "").strip()
if _td:
    try:
        _merged = dict(pathcfg._load())
        _merged.update(json.loads(_td))
        pathcfg._cache = _merged
    except Exception as e:
        print("!! PATHS_JSON_TD 解析失败, 忽略: %s" % e, file=sys.stderr)

try:
    import logmask                                # 公开仓库: 文件名/路径一律哈希化
    logmask.install()
except Exception:
    pass

GB = 1024 ** 3
MB = 1024 ** 2
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"


def _conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = _conf()
BATCH = int(os.environ.get("GRC_BATCH", "400"))          # 每批删除多少个文件


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def human(b):
    if not b:
        return "0 MB"
    return "%.1f MB" % (b / MB) if b < GB else "%.2f GB" % (b / GB)


def rclone(args, timeout=3600):
    return subprocess.run([RCLONE] + list(args) + ["--config", CONF],
                          capture_output=True, text=True, errors="replace", timeout=timeout)


def lsjson(remote, recursive=True, timeout=3600):
    """列目录 -> {相对路径: 字节数}。失败抛异常(调用方决定跳不跳)"""
    args = ["lsjson", remote, "--files-only", "--no-modtime"]
    if recursive:
        args.append("-R")
        args.append("--fast-list")
    r = rclone(args, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:220] or "rclone lsjson 退出码 %s" % r.returncode)
    out = {}
    for x in (json.loads(r.stdout or "[]") or []):
        if x.get("IsDir"):
            continue
        try:
            out[x["Path"]] = int(x.get("Size") or 0)
        except Exception:
            continue
    return out


def about(remote):
    """(总, 已用, 回收站, 剩) —— 拿不到返回 None"""
    r = rclone(["about", remote, "--json"], timeout=600)
    if r.returncode != 0:
        log("   (about %s 失败: %s)" % (strip_remote(remote), (r.stderr or "").strip().splitlines()[-1][:120]
                                       if (r.stderr or "").strip() else "?"))
        return None
    try:
        d = json.loads(r.stdout or "{}")
        return (d.get("total"), d.get("used"), d.get("trashed"), d.get("free"))
    except Exception:
        return None


def strip_remote(s):
    return s.split(":", 1)[0] if ":" in s else s


def sync_dir(s_rem, t_rem, budget_min, transfers, checkers):
    """先补镜像: 源 -> 目标 目录级 copy(--size-only, 同名同大小跳过, 目标端只增不删)。
    跟 gd-backup 用同一套判据, 所以补完就能直接进删除判定。"""
    args = ["copy", s_rem, t_rem, "--size-only",
            "--transfers", str(transfers), "--checkers", str(checkers),
            "--retries", "5", "--retries-sleep", "15s", "--low-level-retries", "10",
            "--contimeout", "60s", "--stats", "60s", "--stats-one-line"]
    if budget_min > 0:
        args += ["--max-duration", "%dm" % int(budget_min)]
    log(">>> 补镜像 %s -> %s (预算 %d 分钟)" % (s_rem, t_rem, budget_min))
    r = rclone(args, timeout=7200)
    tail = ((r.stdout or "").strip().splitlines() or [""])[-1][:160]
    log("    退出码=%s%s | %s" % (r.returncode,
                                " (max-duration 到点, 正常)" if r.returncode == 10 else "", tail))
    if r.returncode not in (0, 10):
        log("    !! 拷贝报错: %s" % (r.stderr or "").strip()[:200])
    return r.returncode


AUDIT_KEYS = [
    ("GD_SRC", "出口 gd-out2: 源(每 3 小时读 out, 处理完同盘 move 到 out2)"),
    ("GD_DST", "出口 gd-out2: 目标(中转盘 out2)"),
    ("GD_ARCH", "出口 gd-out2: 归档区"),
    ("GR_A_SRC", "in2-ydout: 源(送达后删源)"),
    ("GR_B_SRC", "ydout-in3: 源(送达后同盘 move 到归档)"),
    ("GR_B_ARCH", "ydout-in3: 归档区"),
    ("GR_TG_SRC", "od2-tg-gd2: TG 目录的源(送达后删源)"),
    ("GR_TG_DST", "od2-tg-gd2: TG 目录的目标"),
    ("GT_SRC", "gd2->teldrive 冷备: 源(cron 已停)"),
    ("OT_SRC", "out2->目标网盘: 源"),
    ("OT_ARCH", "out2->目标网盘: 归档区"),
    ("DBJM_SRC", "out2->豆瓣酱: 源"),
    ("HL_IN", "上游盘 /in"),
    ("ID_OD_DIRS", "in-pipeline 判重**实时列**的目录(在这里=删完会被当成未归档, 可能回流重下)"),
    ("ID_STEM_MANIFESTS", "in-pipeline 判重读的云端编号清单"),
    ("GM_TY_TG", "gen-manifest: TG目录清单(alist 路径)"),
    ("GM_TY_OUT", "gen-manifest: out2 清单(alist 路径)"),
    ("GM_STEM_DIRS", "gen-manifest: 编号清单来源(alist 路径)"),
]


def _entries(v):
    """配置值可能是 'a,b,c' 这种多值 -> 拆开逐条核"""
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def audit_cross(root, targets):
    """谁还在读/动这个源盘? 逐 key 比 remote 与目录, 只打印布尔(公开仓库日志不进真名)"""
    r_root = strip_remote(root)
    log("---- 交叉核对: 谁在动 %s 上的 %s ----" % (r_root, ",".join(targets)))
    danger = []
    for k, why in AUDIT_KEYS:
        v = pathcfg.get(k)
        if not v:
            continue
        for i, one in enumerate(_entries(v)):
            name = k if len(_entries(v)) == 1 else "%s[%d]" % (k, i)
            if ":" in one and not one.startswith("/"):
                r, p = one.split(":", 1)
                r, p = r.strip(), p.strip("/")
            else:                       # 没带 remote = alist 挂载路径, 只认目录是否命中
                r, p = "(alist)", one.strip("/")
            same_disk = (r == r_root)
            hit = [t for t in targets if p == t or p.startswith(t + "/")]
            if same_disk and hit:
                tag = "**同一盘 + 目录命中 " + ",".join(hit) + "**"
                danger.append((name, hit))
            elif same_disk:
                tag = "同一盘  目录不重叠"
            elif hit:
                tag = "别的盘  目录名同 " + ",".join(hit)
            else:
                tag = "不同盘  无重叠"
            log("   %-18s %-30s %s" % (name, tag, why))
    log("   -- 流程内比对(只比 remote, 粗看) --")
    for a, b in (("GD_SRC", "GD_ARCH"), ("GR_B_SRC", "GR_B_ARCH"), ("GR_TG_SRC", "GR_TG_DST"),
                 ("GB_SRC_ROOT", "GB_DST_ROOT")):
        va, vb = pathcfg.get(a), pathcfg.get(b)
        if va and vb:
            log("   %s %s %s" % (a, "同一盘" if strip_remote(va) == strip_remote(vb) else "不同盘", b))
    if danger:
        log("   !! 下面这些 key 指着我们要删的目录, 删之前先确认它们的角色: %s"
            % ", ".join("%s->%s" % (n, ",".join(h)) for n, h in danger))
    return danger


def tg_send(text):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not (tok and chat):
        log("   (没配 TG_TOKEN/TG_CHAT_ID, 不发 Telegram)")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
        log("   Telegram -> %s" % d.get("ok"))
        return bool(d.get("ok"))
    except Exception as e:
        log("   Telegram 发送失败: %s" % str(e)[:120])
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删; 不加=只演练")
    ap.add_argument("--dirs", default="", help="只处理这些目录(逗号分隔); 留空=用 GB_DIRS")
    ap.add_argument("--queue-dirs", default=pathcfg.get("GRC_QUEUE_DIRS", "out,out2"),
                    help="队列/流动目录: 默认跳过(这些没走完流程, 删了会丢料)")
    ap.add_argument("--include-queue", action="store_true", help="连队列目录一起处理(危险)")
    ap.add_argument("--use-trash", action="store_true", help="删除进回收站(默认跳过回收站)")
    ap.add_argument("--sync", action="store_true",
                    help="先补镜像(源->目标 rclone copy --size-only), 再按同名同大小删源")
    ap.add_argument("--sync-budget-min", type=int, default=200, help="补镜像步骤的时间预算(分钟, 0=不限)")
    ap.add_argument("--transfers", type=int, default=4, help="补镜像并发文件数")
    ap.add_argument("--checkers", type=int, default=8)
    ap.add_argument("--audit", action="store_true", help="打印交叉核对")
    ap.add_argument("--max-gb", type=float, default=0.0, help="本轮最多删多少 GB(0=不限)")
    ap.add_argument("--no-tg", action="store_true", help="不发 Telegram")
    a = ap.parse_args()

    SRC_ROOT = pathcfg.require("GB_SRC_ROOT")
    DST_ROOT = pathcfg.require("GB_DST_ROOT")
    all_dirs = [x.strip() for x in (a.dirs or pathcfg.require("GB_DIRS")).split(",") if x.strip()]
    queues = {x.strip() for x in a.queue_dirs.split(",") if x.strip()}
    if a.include_queue:
        queues = set()
    dirs = [d for d in all_dirs if d not in queues]
    report_path = pathcfg.get("GRC_REPORT", "")
    t0 = time.time()

    log("源 : %s | 目标 : %s" % (SRC_ROOT, DST_ROOT))
    log("目录(跳过队列 %s): %s" % (",".join(sorted(queues)) or "无", ",".join(dirs)))
    log("模式: %s | 删除%s进回收站" % ("真删(--apply)" if a.apply else "演练(只列表)",
                                      "" if a.use_trash else "不"))
    if a.audit:
        audit_cross(SRC_ROOT, all_dirs)

    src_about = about(SRC_ROOT)
    if src_about:
        log("源盘 about: 总 %s / 已用 %s / 回收站 %s / 剩 %s"
            % tuple(human(x) for x in src_about))

    plan, leftovers, skipped, per_dir = [], [], [], []
    sync_info = []
    if a.sync:
        log("---- 第 1 步: 补镜像(源 -> 目标) ----")
        for d in dirs:
            rc = sync_dir("%s:%s" % (SRC_ROOT, d), "%s:%s" % (DST_ROOT, d),
                          a.sync_budget_min, a.transfers, a.checkers)
            sync_info.append((d, rc))
        log("---- 第 2 步: 现场重列两侧, 只删同名同字节的 ----")
    for d in dirs:
        s_rem, t_rem = "%s:%s" % (SRC_ROOT, d), "%s:%s" % (DST_ROOT, d)
        try:
            src = lsjson(s_rem)
        except Exception as e:
            log("!! %-14s 列源失败, 整个目录跳过: %s" % (d, str(e)[:150]))
            skipped.append((d, "列源失败"))
            continue
        try:
            dst = lsjson(t_rem)
        except Exception as e:
            log("!! %-14s 列目标失败, 整个目录跳过(绝不盲删): %s" % (d, str(e)[:150]))
            skipped.append((d, "列目标失败"))
            continue
        if src and not dst:
            log("!! %-14s 目标盘一个文件都没有 -> 跳过(源 %d 个)" % (d, len(src)))
            skipped.append((d, "目标为空"))
            continue
        ok = [(p, s) for p, s in src.items() if dst.get(p) == s]
        left = [(p, s) for p, s in src.items() if dst.get(p) != s]
        per_dir.append({"d": d, "src_n": len(src), "src_b": sum(src.values()),
                        "dst_n": len(dst), "dst_b": sum(dst.values()),
                        "ok_n": len(ok), "ok_b": sum(s for _, s in ok),
                        "left_n": len(left), "left_b": sum(s for _, s in left)})
        log("   %-14s 源 %5d 个 / %-10s | 目标 %5d 个 / %-10s | 可删 %5d 个 / %-10s | 源独有 %d 个 / %s"
            % (d, len(src), human(sum(src.values())), len(dst), human(sum(dst.values())),
               len(ok), human(sum(s for _, s in ok)), len(left), human(sum(s for _, s in left))))
        plan.append((d, ok, s_rem))
        leftovers += [(d, p, s) for p, s in left]

    can_n = sum(len(ok) for _, ok, _ in plan)
    can_b = sum(s for _, ok, _ in plan for _, s in ok)
    log("---- 汇总: 可删 %d 个 / %s; 源独有(留着) %d 个 / %s; 跳过目录 %d 个 ----"
        % (can_n, human(can_b), len(leftovers), human(sum(s for _, _, s in leftovers)), len(skipped)))

    # 清单落盘(明文清单只进报告区, 控制台走脱敏)
    wl = "/tmp/gd_reclaim_plan.tsv"
    with io.open(wl, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 源 %s -> 目标 %s | 生成 %s | 可删 %d 个 / %s\n"
                % (SRC_ROOT, DST_ROOT, time.strftime("%Y-%m-%d %H:%M:%S"), can_n, human(can_b)))
        f.write("# 判据: 目标盘同名 + 字节数一致\n")
        for d, ok, _ in plan:
            for p, s in ok:
                f.write("%s/%s\t%d\n" % (d, p, s))
        if leftovers:
            f.write("\n# ---- 源独有(未备份/大小不符, 不删) ----\n")
            for d, p, s in leftovers:
                f.write("%s/%s\t%d\n" % (d, p, s))

    deleted_n = deleted_b = 0
    failures = []
    if a.apply and can_n:
        remaining = can_b if a.max_gb <= 0 else a.max_gb * GB
        dflags = ["--drive-use-trash=false"] if not a.use_trash else []
        for d, ok, s_rem in plan:
            if not ok:
                continue
            rows = []
            acc = 0.0
            for p, s in sorted(ok, key=lambda x: x[1]):
                if a.max_gb > 0 and acc + s > remaining:
                    break
                rows.append((p, s))
                acc += s
            if not rows:
                continue
            lf = "/tmp/gd_reclaim_%s.txt" % abs(hash(d))
            log(">>> 删除 %s: %d 个 / %s%s" % (d, len(rows), human(acc),
                                              " (本轮上限 %s GB)" % a.max_gb if a.max_gb else ""))
            for i in range(0, len(rows), BATCH):
                chunk = rows[i:i + BATCH]
                with io.open(lf, "w", encoding="utf-8", newline="\n") as f:
                    for p, _ in chunk:
                        f.write(p + "\n")
                r = rclone(["delete", s_rem, "--files-from", lf, "--no-traverse",
                            "--transfers", "8", "--checkers", "16",
                            "--retries", "5", "--low-level-retries", "10"] + dflags,
                           timeout=7200)
                if r.returncode == 0:
                    deleted_n += len(chunk)
                    deleted_b += sum(s for _, s in chunk)
                    remaining -= sum(s for _, s in chunk)
                else:
                    failures.append("%s: 第 %d 批退出码 %s %s" % (d, i // BATCH + 1, r.returncode,
                                                              (r.stderr or "").strip()[:140]))
                    log("   !! 本批失败: %s" % (r.stderr or "").strip()[:180])
                log("   进度 %d / %d (%.1f 分钟)" % (deleted_n, can_n, (time.time() - t0) / 60))

    # ---- 复核: 目标盘那份是不是还在 ----
    verify = []
    if a.apply and deleted_n:
        for d, ok, _ in plan:
            if not ok:
                continue
            try:
                dst2 = lsjson("%s:%s" % (DST_ROOT, d))
                still = sum(1 for p, s in ok if dst2.get(p) == s)
                verify.append((d, still, len(ok)))
            except Exception as e:
                verify.append((d, -1, len(ok)))
                log("   !! %s 复核列目标失败: %s" % (d, str(e)[:120]))
    dst_about = about(DST_ROOT)
    src_about2 = about(SRC_ROOT)

    lines = ["# 源盘冗余回收(gd2 -> gd6 已备份文件) %s  [%s]"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练"),
             "# 源 %s -> 目标 %s | 目录: %s | 跳过队列: %s"
             % (SRC_ROOT, DST_ROOT, ",".join(dirs), ",".join(sorted(queues)) or "无"),
             "# 判据: 目标盘同名 + 字节数一致 才删; 目标为空/列不到 -> 整目录跳过"]
    for d, rc in sync_info:
        lines.append("补镜像 %s: 退出码 %s%s" % (d, rc, " (到时间预算, 下轮继续)" if rc == 10 else ""))
    for e in per_dir:
        lines.append("%-14s 源 %d 个 %s | 目标 %d 个 %s | 可删 %d 个 %s | 源独有 %d 个 %s"
                     % (e["d"], e["src_n"], human(e["src_b"]), e["dst_n"], human(e["dst_b"]),
                        e["ok_n"], human(e["ok_b"]), e["left_n"], human(e["left_b"])))
    for d, why in skipped:
        lines.append("!! 跳过 %s: %s" % (d, why))
    lines.append("可删合计: %d 个 / %s" % (can_n, human(can_b)))
    lines.append("实删: %d 个 / %s%s" % (deleted_n, human(deleted_b),
                                     "" if not a.max_gb else " (上限 %s GB)" % a.max_gb))
    for d, still, tot in verify:
        lines.append("复核 %s: 目标端确认仍在 %s/%d" % (d, still, tot))
    if src_about and src_about2:
        lines.append("源盘 about: 总 %s | 已用 %s -> %s | 回收站 %s -> %s | 剩 %s -> %s"
                     % (human(src_about[0]), human(src_about[1]), human(src_about2[1]),
                        human(src_about[2]), human(src_about2[2]),
                        human(src_about[3]), human(src_about2[3])))
        lines.append("实际释放(about 口径): %s" % human((src_about[1] or 0) - (src_about2[1] or 0)))
    if dst_about:
        lines.append("目标盘 about: 总 %s | 已用 %s | 剩 %s" % (human(dst_about[0]),
                                                          human(dst_about[1]), human(dst_about[3])))
    if leftovers:
        lines.append("")
        lines.append("## 源独有(未备份或大小不符, 没删, 需人工看) 前 40 条")
        for d, p, s in leftovers[:40]:
            lines.append("%s/%s\t%d" % (d, p, s))
    for f in failures:
        lines.append("!! %s" % f)
    body = "\n".join(lines)
    print(body, flush=True)

    if report_path:
        rp = "/tmp/gd_reclaim_report.txt"
        io.open(rp, "w", encoding="utf-8", newline="\n").write(body + "\n")
        rr = rclone(["copyto", rp, report_path.rstrip("/") + "/gd_reclaim_report.txt", "--retries", "3"],
                    timeout=900)
        log("报告 -> %s: %s" % (report_path, "OK" if rr.returncode == 0 else (rr.stderr or "")[:120]))
        r2 = rclone(["copyto", wl, report_path.rstrip("/") + "/gd_reclaim_plan.tsv", "--retries", "3"],
                    timeout=900)
        log("清单 -> %s: %s" % (report_path, "OK" if r2.returncode == 0 else (r2.stderr or "")[:120]))

    if not a.no_tg:
        head = "🧹 gd2 冗余回收 %s\n可删 %d 个 / %s" % (
            "· 演练" if not a.apply else "· 已执行", can_n, human(can_b))
        if a.apply:
            head += "\n实删 %d 个 / %s" % (deleted_n, human(deleted_b))
            if src_about and src_about2:
                head += "\n源盘已用 %s -> %s" % (human(src_about[1]), human(src_about2[1]))
        if skipped:
            head += "\n跳过目录: %s" % ", ".join("%s(%s)" % (d, w) for d, w in skipped)
        if leftovers:
            head += "\n源独有(未删) %d 个" % len(leftovers)
        tg_send(head)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
