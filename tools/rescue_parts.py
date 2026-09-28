# -*- coding: utf-8 -*-
"""临时救援 (一次性): 把中转盘 out2 里"超过单片上限"的旧分片再切一刀, 让它们能进归档盘。

背景 (2026-09-28): 早先 gd-out2 是"对半切", 源文件 >600MB 时切出的两片各自仍 >300MB,
被归档盘(豆包 /dbjm)的 ~300MB 单文件硬上限拒收 -> 一直卡在中转盘 out2 出不去,
连带源远端:out2 里的原件也归档不了。治本是改 gd-out2 的切分策略(已改), 本脚本负责清**存量**。

每个待处理文件:
  1) 下载到 runner 工作区
  2) ffmpeg 无损按 <= --seg-mb 切成 N 片(通常 2 片),
     命名 <原名去扩展>.part001...partNNN.<ext>
     -- 这种命名能被 od2-dbjm / out2-ty-sync 现有的分片判定直接认出, 不用改别的代码
  3) 新片上传回中转盘 out2 (交给 od2-dbjm 送归档盘)
  4) 原片上传到 源远端:out2 归档区 (交给 out2-ty-sync 送天翼; --no-arch 可关掉)
  5) 上面全部校验通过后, 删掉中转盘 out2 里的原片 (进回收站, 可捞)

默认演练(只看清单); --apply 才真动。报告写 报告区/rescue_parts_report.txt
"""
import argparse
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg                      # noqa: E402
import gd_out2_split as G           # noqa: E402  复用 rclone 封装 + split_max

SRC = pathcfg.require("DBJM_SRC")           # 中转盘 out2 (卡住的分片都在这儿)
ARCH = pathcfg.require("GD_ARCH")           # 源远端 out2 (原片归档区)
OD = pathcfg.require("GM_OD")               # 报告区
WORK = G.WORK
MB = 1048576


def rclone(args, timeout=7200):
    return G.rclone(args, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真动; 不加则只演练")
    ap.add_argument("--seg-mb", type=int, default=280, help="新分片的单片上限(MB)")
    ap.add_argument("--max-ops", type=int, default=100, help="本轮最多处理几个")
    ap.add_argument("--no-arch", action="store_true",
                    help="不把原片送到归档区(中间片不保留, 只留切出来的新片)")
    ap.add_argument("--purge-arch", action="store_true",
                    help="清理模式: 删掉归档区里'救援塞进去的原片'"
                         "(判据: 中转盘里存在它的 .part001 子片 —— 正常原件不会有这种情况)")
    ap.add_argument("--copy-arch", action="store_true",
                    help="拷贝模式: 把中转盘的文件逐个复制到归档区(多留一处备份; "
                         "归档区由 out2-ty-sync 接手送天翼)")
    a = ap.parse_args()
    seg_bytes = a.seg_mb * MB
    os.makedirs(WORK, exist_ok=True)

    if a.purge_arch:
        lines = ["# 清理报告: 归档区里的'救援原片'  %s UTC  [%s]"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练")]
    elif a.copy_arch:
        lines = ["# 拷贝报告: 中转盘 -> 归档区(多留一处备份)  %s UTC  [%s]"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练"),
                 "# 规则: %s 里的文件逐个复制到 %s; 归档区由 out2-ty-sync 接手送天翼" % (SRC, ARCH)]
    else:
        lines = ["# 救援报告: 超上限旧分片 -> 再切一刀  %s UTC  [%s]"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练"),
                 "# 规则: >%dMB 的按 <=%dMB 切成 N 片 -> 新片回 %s; 原片 -> %s; 全部校验通过后删原片"
                 % (a.seg_mb, a.seg_mb, SRC, "不保留" if a.no_arch else ARCH)]

    rname = ("purge_arch_report.txt" if a.purge_arch
             else "copy_arch_report.txt" if a.copy_arch
             else "rescue_parts_report.txt")
    report = OD + "/" + rname

    def finish(rc=0):
        txt = "\n".join(lines) + "\n"
        rp = os.path.join(WORK, rname)
        try:
            with io.open(rp, "w", encoding="utf-8") as f:
                f.write(txt)
        except Exception as e:
            print("!! 报告落盘失败: %s" % str(e)[:80])
        r = rclone(["copyto", rp, report, "--retries", "2"], timeout=600)
        print("报告 -> %s: %s" % (report, "OK" if r.returncode == 0 else (r.stderr or "")[:90]))
        return rc

    if a.copy_arch:
        # 拷贝: 中转盘 -> 归档区 (逐个 copyto + 大小校验; 归档区已有同名同大小就跳过)
        src = G.lsjson(SRC)
        arch = G.lsjson(ARCH)
        todo = [(n, s) for n, s in sorted(src.items()) if arch.get(n) != s]
        lines.append("拷贝模式: %s 共 %d 个 | 归档区 %s 已有 %d 个 | 待拷 %d 个 (合计 %s)"
                     % (SRC, len(src), ARCH, len(arch), len(todo),
                        G.human(sum(s for _, s in todo))))
        print(lines[1])
        if not todo:
            lines.append("没有需要拷贝的, 收工")
            return finish()
        if not a.apply:
            for n, s in todo[:30]:
                lines.append("  - [%s] %s" % (G.human(s), n))
            if len(todo) > 30:
                lines.append("  ... 另有 %d 个" % (len(todo) - 30))
            lines.append("(演练) 以上 %d 个将复制到归档区; 加 --apply 才真拷" % len(todo))
            return finish()
        n_ok = n_fail = 0
        for i, (n, s) in enumerate(todo, 1):
            r = rclone(["copyto", "%s/%s" % (SRC, n), "%s/%s" % (ARCH, n),
                        "--retries", "3", "--low-level-retries", "10", "--stats", "0"],
                       timeout=3600)
            got = G.stat_size("%s/%s" % (ARCH, n))
            if r.returncode == 0 and got == s:
                n_ok += 1
                lines.append("[%d/%d][%s] %s -> OK" % (i, len(todo), G.human(s), n))
            else:
                n_fail += 1
                lines.append("[%d/%d][%s] %s -> 失败 远端=%s %s"
                             % (i, len(todo), G.human(s), n, got, (r.stderr or "")[:100]))
            if i % 5 == 0:
                print("   已拷 %d/%d (成功 %d 失败 %d)" % (i, len(todo), n_ok, n_fail))
        lines.append("---")
        lines.append("拷贝完成: 成功 %d | 失败 %d" % (n_ok, n_fail))
        print(lines[-1])
        return finish()

    if a.purge_arch:
        # 清理: 归档区里"和它的 .part001 子片同时在场"的文件 = 救援塞进去的原片
        # (正常原件只会被切出一层 .partNNN 放在中转盘; 这里的 X 有 X.part001 子片, 说明 X 本身被再切过)
        arch = G.lsjson(ARCH)
        src = G.lsjson(SRC)
        victims = []
        for n in sorted(arch):
            stem, ext = os.path.splitext(n)
            if (stem + ".part001" + ext) in src:
                victims.append((n, arch[n]))
        lines.append("清理模式: 归档区 %s 共 %d 个文件 | 中转盘 %s 共 %d 个"
                     % (ARCH, len(arch), SRC, len(src)))
        lines.append("判定为'救援原片'的 %d 个:" % len(victims))
        for n, s in victims:
            lines.append("  - [%s] %s" % (G.human(s), n))
        print(lines[1])
        for l in lines[2:]:
            print("   " + l)
        if not victims:
            lines.append("没有需要清理的, 收工")
            return finish()
        if not a.apply:
            lines.append("(演练) 以上 %d 个将从归档区删除; 加 --apply 才真删" % len(victims))
            return finish()
        ok = fail = 0
        for n, s in victims:
            r = rclone(["deletefile", "%s/%s" % (ARCH, n), "--retries", "3"], timeout=600)
            if r.returncode == 0:
                ok += 1
                lines.append("  已删除: %s" % n)
            else:
                fail += 1
                lines.append("  删除失败: %s -> %s" % (n, (r.stderr or "")[:110]))
        lines.append("---")
        lines.append("清理完成: 成功 %d | 失败 %d" % (ok, fail))
        print(lines[-1])
        return finish()

    listing = G.lsjson(SRC)
    todo = [(n, s) for n, s in sorted(listing.items(), key=lambda kv: kv[1]) if s > seg_bytes]
    lines.append("源 %s: %d 个文件 | 其中 > %dMB 的 %d 个 (合计 %s)"
                 % (SRC, len(listing), a.seg_mb, len(todo),
                    G.human(sum(s for _, s in todo))))
    print(lines[-1])
    if not todo:
        lines.append("没有需要处理的, 收工")
        return finish()

    n_ok = n_fail = 0
    for idx, (name, size) in enumerate(todo[:a.max_ops], 1):
        stem, ext = os.path.splitext(name)
        lp = os.path.join(WORK, name)
        parts = []
        head = "[%d/%d][%s] %s" % (idx, min(len(todo), a.max_ops), G.human(size), name)
        print("\n" + head)

        if not a.apply:
            lines.append("%s | (演练) 将切成 <=%dMB 的 N 片 -> %s; 原片 -> %s; 然后删原片"
                         % (head, a.seg_mb, SRC, "不保留" if a.no_arch else ARCH))
            n_ok += 1
            continue

        try:
            r = rclone(["copyto", "%s/%s" % (SRC, name), lp, "--retries", "3",
                        "--low-level-retries", "10", "--stats", "0"], timeout=3600)
            if r.returncode != 0 or G.local_size(lp) != size:
                raise RuntimeError("下载失败: %s" % (r.stderr or "")[:110])
            print("    下载 OK %s" % G.human(G.local_size(lp)))

            parts = G.split_max(lp, stem, ext, seg_bytes)
            tot = sum(G.local_size(p) for p in parts)
            if (len(parts) < 2
                    or any(G.local_size(p) > seg_bytes for p in parts)
                    or abs(tot - size) > size * 0.05):
                raise RuntimeError("切割结果异常: %d 段, 合计 %s / 原 %s"
                                   % (len(parts), G.human(tot), G.human(size)))

            lines.append("%s | 切成 %d 片: %s" % (head, len(parts), " + ".join(
                "%s(%s)" % (os.path.basename(p), G.human(G.local_size(p))) for p in parts)))

            bad = []
            for p in parts:
                bn = os.path.basename(p)
                rr = rclone(["copyto", p, "%s/%s" % (SRC, bn), "--retries", "3",
                             "--low-level-retries", "10", "--stats", "0"], timeout=3600)
                got = G.stat_size("%s/%s" % (SRC, bn))
                if rr.returncode != 0 or got != G.local_size(p):
                    bad.append("新片 %s 远端=%s" % (bn, got))
            lines.append("    新片 -> %s: %s" % (SRC, "OK" if not bad else "; ".join(bad)))

            if not a.no_arch:
                rr = rclone(["copyto", lp, "%s/%s" % (ARCH, name), "--retries", "3",
                             "--low-level-retries", "10", "--stats", "0"], timeout=3600)
                got = G.stat_size("%s/%s" % (ARCH, name))
                if rr.returncode != 0 or got != size:
                    bad.append("原片 -> %s 远端=%s (期望 %s)" % (ARCH, got, size))
                    lines.append("    原片 -> %s: FAIL 远端=%s" % (ARCH, got))
                else:
                    lines.append("    原片 -> %s: OK" % ARCH)

            if bad:
                n_fail += 1
                lines.append("    !! 有失败, 本次**不删原片**, 下轮自动重试")
                print("    !! " + "; ".join(bad))
            else:
                rr = rclone(["deletefile", "%s/%s" % (SRC, name), "--retries", "3"], timeout=600)
                if rr.returncode == 0:
                    lines.append("    删原片(进回收站): OK")
                    n_ok += 1
                else:
                    n_fail += 1
                    lines.append("    删原片失败: %s" % (rr.stderr or "")[:110])
        except Exception as e:
            n_fail += 1
            lines.append("%s | !! %s" % (head, str(e)[:180]))
            print("    !! %s" % str(e)[:180])
        finally:
            G.rm(lp)
            for p in parts:
                G.rm(p)

    lines.append("---")
    lines.append("本轮: 处理 %d | 成功 %d | 失败 %d"
                 % (min(len(todo), a.max_ops), n_ok, n_fail))
    print("\n" + lines[-1])
    return finish()


if __name__ == "__main__":
    import logmask
    logmask.install()
    sys.exit(main())
