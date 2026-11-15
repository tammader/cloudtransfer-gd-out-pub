# -*- coding: utf-8 -*-
"""迁移盘上的 onedrive 目录 -> 目标远端: 跨盘「拷贝」(源端只读, 不删不移)。

背景
  目标盘退役时, 它原来的内容被整体搬到了 迁移盘 的 onedrive 目录下(gd2-od2 迁移)。
  这条流程把那份数据**原样拷回** 目标盘 —— 原来什么目录结构, 拷回去还是什么结构。

为什么是「拷贝」而不是「搬」
  要的是 copy: 源端一个字节都不动, 拷完两边各有一份。
  真想搬(送达后删源)再加 --move-source; 默认关。

做法(跨盘没有 server-side copy, rclone 自动「下载到 runner -> 上传到目标」)
  * 先列两端(逐一级项递归列; 目标端列不动的特殊项, 如 Personal Vault, 跳过不碰)
  * 目标端「同名 + 字节数一致」= 已送到 -> 直接跳过(所以这流程可反复跑, 幂等)
  * 剩下的按累计大小切批, 每批搬完**只校验这批涉及的目录**(不重列整盘), 再下一批
  * 磁盘预算: 每批不超过 --disk-budget-gb; 0 = 按 runner 剩余空间自动算(留 --reserve-gb)
  * 时间预算: 到点优雅收工, 没搬完的下一轮继续
  * 同一文件连续失败 --fail-max 次后本轮不再重试(记云端失败清单)
  * 默认演练(dry-run); --apply 才真拷

配置(pathcfg: CI=Secret PATHS_JSON / 本机 paths.local.json)
  GO_SRC     源(迁移盘的 onedrive 目录, 形如 <远端>:<目录>)
  GO_DST     目标(形如 <远端>: 或 <远端>:<目录>)
  GO_REPORT  报告落点(rclone 路径; 可省)
  GO_FAILED  失败清单落点(rclone 路径; 可省)
环境
  TG_TOKEN / TG_CHAT_ID(可选) 发摘要;  LOG_MASK=1(默认) 日志里文件名/路径一律哈希化

用法
  python3 tools/gd2_od2_copy.py                        # 演练: 只报计划
  python3 tools/gd2_od2_copy.py --apply                # 真拷
  python3 tools/gd2_od2_copy.py --apply --only out2    # 只处理相对路径含 out2 的
  python3 tools/gd2_od2_copy.py --apply --order size-asc
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

import pathcfg                                     # noqa: E402  路径真值: CI=Secret / 本机=paths.local.json

try:
    import logmask                                 # noqa: E402  公开仓库: 文件名/路径/远端名一律哈希化
    logmask.install()
except Exception:
    pass

WORK = os.environ.get("GD2OD2_WORK", "/tmp/gd2od2")
GB = 1024 ** 3
MB = 1024 ** 2
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
_MISSING = ("directory not found", "not found", "object not found", "path not found",
            "item not found", "does not exist", "404")


def _conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf"),
              os.path.expanduser("~/AppData/Roaming/rclone/rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = _conf()


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def human(b):
    if not b:
        return "0 MB"
    return "%.0f MB" % (b / MB) if b < GB else "%.2f GB" % (b / GB)


def is_missing(msg):
    m = (msg or "").lower()
    return any(k in m for k in _MISSING)


def rclone(args, timeout=14400):
    return subprocess.run([RCLONE] + list(args) + ["--config", CONF],
                          capture_output=True, text=True, errors="replace", timeout=timeout)


def lsjson(remote, recursive=True, timeout=3600):
    """列一个目录 -> {相对路径: 字节数}; 只取文件。失败抛异常"""
    args = ["lsjson", remote, "--files-only", "--no-modtime"]
    if recursive:
        args += ["-R", "--fast-list"]
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


def lsd(remote, timeout=900):
    """列一级 -> [(名字, 是否目录)]。失败抛异常"""
    r = rclone(["lsjson", remote, "--dirs-only"], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:200] or "rclone 列 %s 失败" % remote)
    dirs = [x["Name"] for x in (json.loads(r.stdout or "[]") or [])]
    r2 = rclone(["lsjson", remote, "--files-only"], timeout=timeout)
    files = [x["Name"] for x in (json.loads(r2.stdout or "[]") or [])] if r2.returncode == 0 else []
    return [(d, True) for d in dirs] + [(f, False) for f in files]


def file_size(remote, timeout=600):
    r = rclone(["size", remote, "--json"], timeout=timeout)
    try:
        return int(json.loads(r.stdout or "{}").get("bytes") or 0)
    except Exception:
        return 0


def tree_of(remote, entries, allow_missing=True, tag=""):
    """按一级项逐个列出 -> {相对路径: 字节数}。
    allow_missing: 单项列不动(目标端还不存在 / 特殊项如 Personal Vault)就跳过, 不炸整轮。"""
    out, skipped = {}, []
    base = remote.rstrip("/")
    for name, isdir in entries:
        if isdir:
            try:
                for k, v in lsjson("%s/%s" % (base, name)).items():
                    out["%s/%s" % (name, k)] = v
            except Exception as e:
                if allow_missing:
                    skipped.append(name)
                    log("   (跳过列不动的一级项 %s%s: %s)" % (name, tag, str(e)[:90]))
                    continue
                raise
        else:
            out[name] = file_size("%s/%s" % (base, name))
    return out, skipped


def free_gb(path):
    try:
        d = path if os.path.isdir(path) else (os.path.dirname(path) or "/")
        u = shutil.disk_usage(d)
        return u.free / GB, u.total / GB
    except Exception:
        return None, None


def load_json_remote(path, default):
    if not path:
        return default
    tmp = os.path.join(WORK, "dl.json")
    r = rclone(["copyto", path, tmp, "--retries", "2"], timeout=600)
    if r.returncode != 0 or not os.path.exists(tmp):
        return default
    try:
        with io.open(tmp, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def save_json_remote(path, obj, tag):
    if not path:
        return
    tmp = os.path.join(WORK, tag + ".json")
    try:
        if not obj:
            rclone(["deletefile", path, "--retries", "2"], timeout=600)
            return
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        r = rclone(["copyto", tmp, path, "--retries", "3"], timeout=900)
        if r.returncode != 0:
            log("   !! 清单写入失败: %s" % (r.stderr or "")[:120])
    except Exception as e:
        log("   !! 清单写入异常: %s" % str(e)[:100])


def write_list(names, tag):
    p = os.path.join(WORK, "_%s.txt" % tag)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(names) + "\n")
    return p


def tg_send(text):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not tok or not chat:
        log("   (没配 TG_TOKEN/TG_CHAT_ID, 不发 Telegram)")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as x:
            log("   TG 摘要已发(%s)" % x.status)
    except Exception as e:
        log("   !! TG 发送失败: %s" % str(e)[:120])


def batches_of(names, sizes, cap_bytes):
    """按累计大小切批(单个文件超过 cap 也单独成一批)"""
    out, cur, acc = [], [], 0
    for n in names:
        s = sizes.get(n, 0)
        if cur and acc + s > cap_bytes:
            out.append(cur)
            cur, acc = [], 0
        cur.append(n)
        acc += s
    if cur:
        out.append(cur)
    return out


def finish(lines, report):
    """报告落盘 + 上传到报告区"""
    rp = os.path.join(WORK, "gd2_od2_report.txt")
    text = "\n".join(lines) + "\n"
    try:
        if not os.path.isdir(WORK):
            os.makedirs(WORK, exist_ok=True)
        with io.open(rp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except Exception as e:
        log("!! 本地报告写入失败: %s" % str(e)[:100])
    if not report:
        log("   (没配 GO_REPORT, 报告只留本地)")
        return
    r = rclone(["copyto", rp, report, "--retries", "3"], timeout=900)
    log("报告 -> %s: %s" % (report, "OK" if r.returncode == 0 else "失败 " + (r.stderr or "")[:120]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真拷; 不加则只演练")
    ap.add_argument("--move-source", action="store_true",
                    help="送达后删源(把「拷贝」变成「搬运」; 默认关)")
    ap.add_argument("--max-total-gb", type=float, default=0.0, help="本轮总量上限(GB); 0=不限")
    ap.add_argument("--batch-gb", type=float, default=8.0, help="每批搬运量上限(GB)")
    ap.add_argument("--disk-budget-gb", type=float, default=0.0, help="每批磁盘上限(GB); 0=按剩余空间自动算")
    ap.add_argument("--reserve-gb", type=float, default=2.0, help="给系统留的磁盘余量(GB)")
    ap.add_argument("--budget-min", type=int, default=310, help="本轮时间预算(分钟)")
    ap.add_argument("--fail-max", type=int, default=3, help="同一文件连续失败几次后本轮不再试")
    ap.add_argument("--order", choices=["name", "size-asc", "size-desc"], default="name",
                    help="处理顺序(默认按名字; 想先出成果用 size-asc)")
    ap.add_argument("--only", default=os.environ.get("ONLY_NAME", ""),
                    help="只处理相对路径里含该子串的文件(调试用)")
    a = ap.parse_args()

    SRC = pathcfg.require("GO_SRC")
    DST = pathcfg.require("GO_DST")
    REPORT = pathcfg.get("GO_REPORT", "")
    FAILED = pathcfg.get("GO_FAILED", "")

    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()

    disk_budget = a.disk_budget_gb
    fg, tg_ = free_gb(WORK)
    if disk_budget <= 0:
        disk_budget = max(1.0, (fg - a.reserve_gb)) if fg else 8.0
    cap_gb = max(0.5, min(a.batch_gb, disk_budget))

    lines = ["# 目标盘 拷贝报告 %s UTC  [%s]" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                 "执行模式" if a.apply else "演练模式(dry-run)"),
             "# 源 -> 目标: %s -> %s" % (SRC, DST),
             "# 规则: 目标端「同名同大小」即视为已送达 -> 跳过; 源端%s" %
             ("送达后删除(--move-source)" if a.move_source else "只读, 不删不移"),
             "# 每批上限 %s | 磁盘余量 %s(总 %s) | 时间预算 %d 分钟"
             % (human(cap_gb * GB), human(fg * GB) if fg else "未知",
                human(tg_ * GB) if tg_ else "未知", a.budget_min)]
    log("源 %s -> 目标 %s" % (SRC, DST))
    log("每批上限 %s | 磁盘余量 %s | 时间预算 %d 分钟"
        % (human(cap_gb * GB), human(fg * GB) if fg else "未知", a.budget_min))

    # ---- 列源端 ----
    try:
        entries = lsd(SRC)
    except Exception as e:
        log("!! 列源失败: %s" % str(e)[:200])
        lines.append("\n!! 列源失败: %s" % str(e)[:200])
        finish(lines, REPORT)
        return 1
    isdir_of = dict(entries)

    src, skipped_src = tree_of(SRC, entries)
    if a.only:
        # 候选细化到文件级: 相对路径含该子串才算(所以 --only 能直接挑一批小文件做验证)
        src = {k: v for k, v in src.items() if a.only in k}
        log("按 --only=%r 过滤到文件级: %d 个" % (a.only, len(src)))
    for name, isdir in entries:
        sub = {k: v for k, v in src.items() if k == name or k.startswith(name + "/")}
        log("   源 %s: %d 个 / %s" % (name, len(sub), human(sum(sub.values()))))
    if skipped_src:
        lines.append("源端列不动的一级项(跳过): %s" % ", ".join(skipped_src))

    if not src:
        lines.append("\n源端没文件")
        log("源端没文件")
        finish(lines, REPORT)
        return 0
    lines.append("\n源端: %d 个一级项 / %d 个文件 / %s"
                 % (len(entries), len(src), human(sum(src.values()))))

    # ---- 列目标端(判断哪些已经送到) ----
    try:
        dst_entries = lsd(DST)
    except Exception as e:
        if is_missing(str(e)):
            dst_entries = []
            log("   (目标 %s 还没有一级项, 当成空的)" % DST)
        else:
            lines.append("\n!! 列目标失败(不敢乱搬, 直接收工): %s" % str(e)[:200])
            log("!! 列目标失败: %s" % str(e)[:200])
            finish(lines, REPORT)
            return 1
    dst, _ = tree_of(DST, dst_entries)
    already = [n for n in src if n in dst and dst[n] == src[n]]
    diff = [n for n in src if n in dst and dst[n] != src[n]]
    todo = [n for n in src if n not in dst or dst[n] != src[n]]   # 缺的 + 同名不同大小(要覆盖)
    log("目标 %s: %d 个文件 | 已送达(跳过) %d | 同名不同大小(重拷) %d | 缺 %d"
        % (DST, len(dst), len(already), len(diff), len(todo) - len(diff)))
    lines.append("目标端: %d 个文件 | 已送达(跳过) %d | 同名不同大小(重拷) %d | 缺 %d"
                 % (len(dst), len(already), len(diff), len(todo) - len(diff)))

    failed = load_json_remote(FAILED, {})
    skip_fail = [n for n in todo if int(failed.get(n, 0)) >= a.fail_max]
    todo = [n for n in todo if n not in skip_fail]
    if a.order == "size-asc":
        todo.sort(key=lambda n: src[n])
    elif a.order == "size-desc":
        todo.sort(key=lambda n: -src[n])
    else:
        todo.sort()

    picked, tot = [], 0
    for n in todo:
        if a.max_total_gb and tot + src[n] > a.max_total_gb * GB:
            break
        picked.append(n)
        tot += src[n]

    log("本轮搬 %d 个 / %s | 已跳过 %d | 失败跳过 %d | 留到下轮 %d"
        % (len(picked), human(tot), len(already), len(skip_fail), max(0, len(todo) - len(picked))))
    lines.append("本轮搬 %d 个(%s) | 已跳过 %d | 失败跳过 %d | 留到下轮 %d"
                 % (len(picked), human(tot), len(already), len(skip_fail), max(0, len(todo) - len(picked))))

    if not a.apply:
        for n in picked[:20]:
            log("   [dry] 待拷 %s (%s)" % (n, human(src[n])))
        lines.append("\n(演练: 没有搬运, 源端与目标端都没动)")
        log("演练结束(加 --apply 才真拷)")
        finish(lines, REPORT)
        return 0

    if not picked:
        lines.append("\n没有要搬的")
        if failed:
            save_json_remote(FAILED, failed, "failed")
        finish(lines, REPORT)
        return 0

    ok_all, bad_all = [], []
    bs = batches_of(picked, src, cap_gb * GB)
    log("分 %d 批搬运(每批 <= %s)" % (len(bs), human(cap_gb * GB)))
    for bi, batch in enumerate(bs, 1):
        if (time.time() - t0) / 60 > a.budget_min:
            log("!! 到时间预算 %d 分钟, 收工(剩下的下轮继续)" % a.budget_min)
            lines.append("\n!! 到时间预算, 已搬完的都校验了, 剩余下轮继续")
            break
        bsz = sum(src[n] for n in batch)
        tops = sorted({n.split("/", 1)[0] for n in batch})
        log("第 %d/%d 批: %d 个 / %s | 涉及一级项 %s"
            % (bi, len(bs), len(batch), human(bsz), ",".join(tops)))
        ff = write_list(batch, "batch")
        t = time.time()
        verb = "move" if a.move_source else "copy"
        r = rclone([verb, SRC, DST, "--files-from", ff,
                    "--size-only",                      # 跨网盘 modtime 不可比, 只比大小
                    "--transfers", "4", "--checkers", "8",
                    "--drive-chunk-size", "64M",
                    "--retries", "3", "--retries-sleep", "10s", "--low-level-retries", "10",
                    "--stats", "60s", "--stats-one-line"], timeout=14400)
        el = (time.time() - t) / 60
        log("   搬运完成: 退出码=%d 用时 %.1f 分钟" % (r.returncode, el))
        if r.returncode != 0:
            log("   (rclone 非零: %s)" % (r.stderr or "").strip()[:200])

        # 校验: 只重列这批涉及的一级项; 目标端「同名同大小」才算送达
        local_ok, local_bad = [], []
        try:
            sub_entries = [(x, isdir_of.get(x, True)) for x in tops]
            dst2, _ = tree_of(DST, sub_entries, allow_missing=False, tag=" (校验)")
            local_ok = [n for n in batch if n in dst2 and dst2[n] == src[n]]
            local_bad = [n for n in batch if n not in local_ok]
        except Exception as e:
            log("   !! 校验时列目标失败, 这批不留成功记录: %s" % str(e)[:140])
            lines.append("!! 第 %d 批校验失败, 全部留下轮重试: %s" % (bi, str(e)[:140]))
            local_bad = list(batch)
        log("   校验: 送达 %d | 未送达 %d" % (len(local_ok), len(local_bad)))
        lines.append("第 %d 批: 送达 %d / %s | 未送达 %d | 用时 %.1f 分钟"
                     % (bi, len(local_ok), human(sum(src[n] for n in local_ok)), len(local_bad), el))
        ok_all += local_ok
        for n in local_bad:
            failed[n] = int(failed.get(n, 0)) + 1
        bad_all += local_bad
        if failed:
            save_json_remote(FAILED, failed, "failed")

    # ---- 收尾统计 ----
    try:
        left_all, _ = tree_of(SRC, entries)
        left = len(left_all) - len(ok_all)
        left_size = sum(v for k, v in left_all.items() if k not in ok_all)
    except Exception:
        left, left_size = -1, 0
    lines.append("\n---")
    lines.append("本轮: 送达 %d 个 / %s | 未送达 %d 个 | 源端还剩 %s 个 / %s"
                 % (len(ok_all), human(sum(src[n] for n in ok_all)), len(bad_all),
                    left, human(left_size)))
    if bad_all:
        lines.append("未送达(下轮重试, 连续 %d 次后本轮不再试):" % a.fail_max)
        for n in bad_all[:15]:
            lines.append("  - %s (%s, 已失败 %d 次)" % (n, human(src[n]), failed.get(n, 0)))
    lines.append("用时 %.1f 分钟" % ((time.time() - t0) / 60))
    log("合计: 送达 %d / %s | 未送达 %d | 用时 %.1f 分钟"
        % (len(ok_all), human(sum(src[n] for n in ok_all)), len(bad_all), (time.time() - t0) / 60))

    finish(lines, REPORT)
    tg_send("[目标盘拷贝] %s\n本轮送达 %d 个 / %s | 未送达 %d | 源端剩 %s 个\n%s"
            % ("执行" if a.apply else "演练", len(ok_all), human(sum(src[n] for n in ok_all)),
               len(bad_all), left, time.strftime("%m-%d %H:%M")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
