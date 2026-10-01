# -*- coding: utf-8 -*-
"""上游盘 清理 (云端版, GitHub Actions 定时执行)

处理**多个目录**(默认 /in 与 /in2), 每个目录三件事:
  1) in 清理: 子文件夹里 >200MB 的视频提取到该目录根(按编号去杂质改名);
     其余小文件 + 空子文件夹 -> 移入回收站(可恢复)
  2) 改名扫描: **扫描根目录所有文件**, 按规则清洗文件名(保留序号)
  3) 去重: 同主编号(非分卷/非序号)的重复文件, 保留最大的, 其余移到该目录对应的 out

改名规则: 编号 + 有意义的短标记(-UC/-C/-U/-WM) + 序号(原样保留) + 标准扩展名
          丢弃: 日文/中文标题、演员名、-FHD 等画质标记
序号规则: (1)(2)(3) / [1] / _1 / -1 代表**不同文件** -> 改名保留, 去重跳过

安全: 默认 --dry 只预览; --apply 才真操作; 删除走 trash(回收站);
      单批上限 --max-ops; 报告写到配置项 HC_REPORT 指定的位置

凭证(环境变量/Secrets): HALAL_CLIENT_ID / HALAL_CLIENT_SECRET / HALAL_REFRESH_TOKEN
路径真值: CI 走 Secret PATHS_JSON, 本机走 paths.local.json(见 pathcfg) —— **代码里不留盘名/目录名**
"""
import argparse
import io
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from halal6 import Halal6
import pathcfg                      # 路径真值: CI=Secret PATHS_JSON, 本机=paths.local.json

# (要处理的目录, 去重移出目录)
ROOTS = [("/in", "/out")]
IN_CLEAN_MIN = 200 * 1024 ** 2
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv")
ID_RE = re.compile(r"[A-Za-z]{1,10}-[0-9]{1,6}")
# 「序号标记」= 代表不同文件的编号: _1 _2 / -1 -2 / (1) (2) / [1] / （1）
VOL_RE = re.compile(r"^[_-](?:[0-9]{1,2}|[A-Za-z])$")
SEQ_TAIL_RE = re.compile(r"^(?:[_\-\u4e00-\u9fff]*[_\-\u4e00-\u9fff]*[_\-\u4e00-\u9fff]*)$")
STD_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv")
SHORT_TAGS = ("UC", "U", "C", "A", "WM")
DEDUP_ID_RE = re.compile(r"[A-Za-z]{1,10}-[0-9]{1,10}")
REPORT = pathcfg.require("HC_REPORT")   # 报告落点(真值在 Secret/配置文件里, 不进代码)


def human(b):
    return "%.0f MB" % (b / 1048576) if b < 1024 ** 3 else "%.2f GB" % (b / 1024 ** 3)


def split_ext(name):
    """按已知视频扩展名拆分(不能用 splitext: [icao.me] 里的点会被误判)"""
    low = name.lower()
    for e in STD_EXT:
        if low.endswith(e):
            return name[:-len(e)], name[-len(e):]
    return name, ""


def seq_tail(s):
    """返回末尾的序号标记(如 (3) / _2 / -1), 无则空串"""
    m = re.search(r"(?:[（(\[][0-9]{1,2}[）)\]]|[_\-][0-9]{1,2})\s*$", s)
    return m.group(0).strip() if m else ""


def clean_id(name):
    """老规则(只删编号前杂质), 保留给"提取时改名"用"""
    stem, ext = os.path.splitext(name)
    m = ID_RE.search(stem)
    if not m:
        return None
    start = m.start()
    fc = stem.lower().find("fc2")
    if fc != -1 and fc < start:
        start = fc
    new = stem[start:]
    return (new + ext) if new != stem else None


def clean_name_v2(name):
    """改名规则(保守版):
       编号 + 紧贴编号的非空格标识(原样保留, 如 -UC/-FHD/_000^WM/.1080p/q)
            + 序号((1)/(2)/_1/-1, 原样保留) + 标准扩展名
       丢弃: **空格之后的**日文/中文标题与演员名
       (纯空格长文本如 " 犯された美人過ぎる女教師 かすみ果穂-FHD" 整段去掉)
       返回 None = 无需改"""
    stem, ext = split_ext(name)
    m = ID_RE.search(stem)
    if not m:
        return None
    idpart = m.group(0)
    rest = stem[m.end():]
    seq = seq_tail(rest)
    if seq:
        rest = rest[:rest.rfind(seq)]
    tail = ""
    if rest and not rest[0].isspace():
        mm = re.match(r"[^\s]+", rest)          # 紧贴编号的连续非空白段
        tail = mm.group(0) if mm else ""
    ex = ext.lower() if ext.lower() in STD_EXT else ".mp4"
    if seq.startswith(("(", "（", "[")):
        new = idpart + tail + " " + seq + ex
    else:
        new = idpart + tail + seq + ex
    return new if new != name else None


def dedup_key(name):
    """(分组键, 是否独立文件): 序号/分卷 -> 独立, 不参与合并"""
    stem, _ = split_ext(name)
    m = DEDUP_ID_RE.search(stem)
    if not m:
        return (None, False)
    tail = stem[m.end():]
    if VOL_RE.match(tail) or seq_tail(tail):
        return (None, True)
    return (m.group(0).lower(), False)


def out_dir_for(root):
    """目录 -> 去重移出目录: /in -> /out, /in2 -> /out2, /in3 -> /out3"""
    m = re.search(r"(\d+)\s*$", root.rstrip("/"))
    return "/out" + (m.group(1) if m else "")


def rclone(args, timeout=1800):
    import subprocess
    conf = os.path.expanduser("~/.config/rclone/rclone.conf")
    return subprocess.run(["rclone"] + args + ["--config", conf],
                          capture_output=True, text=True, errors="replace",
                          timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-ops", type=int, default=300)
    ap.add_argument("--roots", default="", help="覆盖默认目录, 如 /in,/in2")
    ap.add_argument("--in-place-roots", default="",
                    help="这些目录里的重复文件**就地清理**(保留最大的, 其余进回收站), "
                         "不往任何 out 目录移; 完全只在这些目录内部操作")
    ap.add_argument("--skip-clean", action="store_true")
    ap.add_argument("--skip-rename", action="store_true")
    ap.add_argument("--skip-dedup", action="store_true")
    a = ap.parse_args()

    roots = ROOTS
    if a.roots:
        roots = [(x.strip(), out_dir_for(x.strip()))
                 for x in a.roots.split(",") if x.strip()]
    in_place = {x.strip() for x in a.in_place_roots.split(",") if x.strip()}

    cid = os.environ.get("HALAL_CLIENT_ID", "")
    sec = os.environ.get("HALAL_CLIENT_SECRET", "")
    rt = os.environ.get("HALAL_REFRESH_TOKEN", "")
    if not (cid and sec):
        print("!! 缺少 HALAL_CLIENT_ID / HALAL_CLIENT_SECRET")
        return 1
    c = Halal6(cid, sec, rt)
    print("登录:", c.login())
    if not c.token:
        return 1

    lines = ["# 上游盘 清理报告 %s [%s]"
             % (time.strftime("%Y-%m-%d %H:%M:%S"),
                "执行模式" if a.apply else "预览模式(dry-run)")]
    ops = 0

    for ROOT, OUT_DIR in roots:
        inplace = ROOT in in_place
        print()
        print("#" * 62)
        if inplace:
            print("# 目录 %s  (去重: 就地清理, 保留最大的, 其余进回收站)" % ROOT)
            lines.append("\n## %s  (去重: 就地清理, 只动该目录内部)" % ROOT)
        else:
            print("# 目录 %s  (去重移出 -> %s)" % (ROOT, OUT_DIR))
            lines.append("\n## %s  (去重移出 -> %s)" % (ROOT, OUT_DIR))

        # ---------- 1) in 清理 ----------
        if not a.skip_clean:
            print("[in 清理] %s" % ROOT)
            top = c.ls_all(ROOT)
            subs = [(p, n) for p, n, sz, d in top if d]
            root_files = {n for p, n, sz, d in top if not d}
            print("  顶层 %d 项 | 子文件夹 %d 个" % (len(top), len(subs)))
            lines.append("[in 清理] 顶层 %d 项 | 子文件夹 %d 个" % (len(top), len(subs)))
            for spath, sname in subs:
                items = c.ls_all(spath)
                bigs = [(p_, n_, sz_) for p_, n_, sz_, d_ in items
                        if not d_ and sz_ > IN_CLEAN_MIN and n_.lower().endswith(VIDEO_EXT)]
                smalls = [(p_, n_, sz_) for p_, n_, sz_, d_ in items
                          if not (not d_ and sz_ > IN_CLEAN_MIN
                                  and n_.lower().endswith(VIDEO_EXT))]
                print("  📁 %s: 大视频 %d | 其他 %d" % (sname, len(bigs), len(smalls)))
                lines.append("  📁 %s: 大视频 %d | 其他 %d" % (sname, len(bigs), len(smalls)))
                for p_, n_, sz_ in bigs:
                    # 提取时就用"新版改名规则"(一步到位, 不依赖后续改名扫描的列表一致性);
                    # 老规则 clean_id 作为兜底
                    nn = clean_name_v2(n_) or clean_id(n_) or n_
                    dst, k = nn, 2
                    while dst in root_files:
                        base, ext = os.path.splitext(nn)
                        dst = "%s(%d)%s" % (base, k, ext)
                        k += 1
                    root_files.add(dst)
                    if a.apply and ops < a.max_ops:
                        r = c.move([p_], ROOT)
                        ops += 1
                        print("     ⬆ 提取: %s -> %s (%s) %s"
                              % (n_, dst, human(sz_), "OK" if "_error" not in r else r.get("_error", "")))
                    else:
                        print("     [预览] 将提取: %s -> %s (%s)" % (n_, dst, human(sz_)))
                if smalls and a.apply and ops + len(smalls) <= a.max_ops:
                    r = c.trash([p_ for p_, _, _ in smalls])
                    ops += len(smalls)
                    print("     🗑 小文件入回收站 %d 个 %s"
                          % (len(smalls), "OK" if "_error" not in r else r.get("_error")))
                elif smalls:
                    print("     [预览] 小文件将入回收站 %d 个" % len(smalls))
                # 大视频已移出 / 小文件已清 -> 文件夹应为空, 复查后删掉(一次跑完, 不留空壳)
                if a.apply and ops < a.max_ops:
                    left = c.ls_all(spath)
                    if not left:
                        c.trash([spath])
                        ops += 1
                        print("     🗑 子文件夹(已空)入回收站: %s" % sname)
                        lines.append("     🗑 子文件夹已清空并删除: %s" % sname)
                    else:
                        print("     ⚠ 子文件夹仍剩 %d 项, 保留: %s" % (len(left), sname))
                        lines.append("     ⚠ 子文件夹剩 %d 项(未删): %s" % (len(left), sname))
                else:
                    print("     [预览] 子文件夹将在清空后入回收站: %s" % sname)
                    lines.append("     [预览] 子文件夹将清空并删除: %s" % sname)

        # ---------- 2) 改名扫描(全量) ----------
        if not a.skip_rename:
            print("[改名扫描] %s (根目录所有文件)" % ROOT)
            top = c.ls_all(ROOT)
            files = [(p_, n_) for p_, n_, sz_, d_ in top if not d_]
            n_dirs = sum(1 for _, _, _, d_ in top if d_)
            existing = {n_ for _, n_ in files}
            todo = []
            for p_, n_ in files:
                nn = clean_name_v2(n_)
                if not nn or nn == n_:
                    continue
                dst, k = nn, 2
                while dst in existing:
                    base, ext = os.path.splitext(nn)
                    dst = "%s(%d)%s" % (base, k, ext)
                    k += 1
                existing.add(dst)
                todo.append((p_, n_, dst))
            print("  扫描 %d 个文件 | 需改名 %d 个%s"
                  % (len(files), len(todo),
                     ("  (另有 %d 个文件夹, 由 in 清理阶段处理)" % n_dirs) if n_dirs else ""))
            lines.append("[改名扫描] 扫描 %d 个 | 需改名 %d 个%s"
                         % (len(files), len(todo),
                            ("  (文件夹 %d 个)" % n_dirs) if n_dirs else ""))
            for p_, n_, dst in todo[:20]:
                print("    ✏ %s -> %s" % (n_[:56], dst))
                lines.append("    ✏ %s -> %s" % (n_[:56], dst))
            if len(todo) > 20:
                print("    ... 其余 %d 个" % (len(todo) - 20))
            if todo and a.apply and ops + len(todo) <= a.max_ops:
                done = 0
                for p_, n_, dst in todo:
                    r = c.rename(p_, dst)
                    ops += 1
                    if "_error" in r:
                        print("     !! 改名失败 %s: %s" % (n_[:40], str(r)[:80]))
                    else:
                        done += 1
                print("  已改名 %d 个" % done)
                lines.append("  已改名 %d 个" % done)
            elif todo:
                print("  [预览] 不改(加 --apply 生效)")

        # ---------- 3) 去重 ----------
        if not a.skip_dedup:
            print("[去重] %s" % ROOT)
            top = c.ls_all(ROOT)
            files = [(p, n, sz) for p, n, sz, d in top if not d]
            if inplace:
                dst_names = set()          # 就地清理: 不碰任何 out 目录
            else:
                try:
                    dst_names = {n for p, n, sz, d in c.ls_all(OUT_DIR)}
                except Exception:
                    dst_names = set()
            groups, vols, scanned = {}, 0, 0
            for p, n, sz in files:
                if not n.lower().endswith(VIDEO_EXT):
                    continue
                scanned += 1
                key, is_indep = dedup_key(n)
                if is_indep or key is None:
                    if is_indep:
                        vols += 1
                    continue
                groups.setdefault(key, []).append((p, n, sz))
            dup = {k: v for k, v in groups.items() if len(v) > 1}
            print("  扫描视频 %d | 分卷/序号跳过 %d | 重复组 %d"
                  % (scanned, vols, len(dup)))
            lines.append("[去重] 扫描 %d | 分卷/序号跳过 %d | 重复组 %d"
                         % (scanned, vols, len(dup)))
            for key, items in sorted(dup.items()):
                items.sort(key=lambda x: (-x[2], len(x[1]), x[1]))
                print("  🔁 [%s] 保留 %s (%s)" % (key, items[0][1], human(items[0][2])))
                lines.append("  🔁 [%s] 保留 %s" % (key, items[0][1]))
                for p_, n_, sz_ in items[1:]:
                    # 就地清理的目录: 重复的直接进回收站(可捞), 绝不往别的目录移
                    if inplace:
                        if a.apply and ops < a.max_ops:
                            r = c.trash([p_])
                            ops += 1
                            print("     🗑 重复(就地清理)入回收站: %s (%s) %s"
                                  % (n_, human(sz_), "OK" if "_error" not in r else r.get("_error")))
                            lines.append("     🗑 重复入回收站: %s (%s)" % (n_, human(sz_)))
                        else:
                            print("     [预览] 重复将入回收站: %s (%s)" % (n_, human(sz_)))
                        continue
                    final = n_
                    if final in dst_names:
                        base, ext = os.path.splitext(n_)
                        k = 2
                        while "%s(%d)%s" % (base, k, ext) in dst_names:
                            k += 1
                        final = "%s(%d)%s" % (base, k, ext)
                    dst_names.add(final)
                    if a.apply and ops < a.max_ops:
                        r = c.move([p_], OUT_DIR)
                        ops += 1
                        print("     📦 已移出 -> %s/%s (%s) %s"
                              % (OUT_DIR, final, human(sz_),
                                 "OK" if "_error" not in r else r.get("_error")))
                    else:
                        print("     [预览] 将移出 -> %s/%s (%s)" % (OUT_DIR, final, human(sz_)))

    lines.append("\n合计操作 %d 次 | 模式: %s"
                 % (ops, "执行" if a.apply else "预览"))
    print()
    print("#" * 62)
    print("合计操作 %d 次 (上限 %d)" % (ops, a.max_ops))
    rp = "/tmp/halal_clean_report.txt"
    io.open(rp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    rc = rclone(["copyto", rp, REPORT], timeout=600)
    print("报告上传 %s: %s" % (REPORT, "OK" if rc.returncode == 0 else "失败"))
    return 0


if __name__ == "__main__":
    try:
        import logmask              # 日志脱敏: 文件名/路径/盘名 -> 中性代号(见 logmask.py)
        logmask.install()
    except Exception:
        pass
    sys.exit(main())
