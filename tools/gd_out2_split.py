# -*- coding: utf-8 -*-
"""源远端/out -> 中转远端/out2 (大文件按 <=280MB 切成 N 片) + 源文件归档到 源远端/out2

流程 (每轮分多个批次, 每批尽量写满 runner 的工作区):
  1) 读 源远端:out 清单, 按剩余磁盘空间挑一批文件 (小文件优先)
  2) 一次性把这批下载到 runner (rclone --files-from, 多线程)
  3) 逐个处理:
       <= 300MB : 直接上传 中转远端/out2, 校验大小
       >  300MB : ffmpeg 无损按 <=280MB 切成 N 段, 命名 <原名去扩展>.part001...partNNN.<ext>
                  -> 每段都上传并校验 (段必须 <= 上限, 超标就缩短段时长重切)
     上传校验通过 -> 源文件移入 源远端:out2 (归档) -> 删掉本地文件, 腾空间
  4) 这一批处理完, 重新按剩余空间挑下一批, 直到用完 --max-ops 或时间预算

磁盘: 分批大小 = min(剩余空间 - 预留, --disk-budget-gb)。切分时源文件与分段同时在盘上,
      所以选批时按 "本批总量 + 本批最大的待切文件" 估算峰值, 保证不超过预算。

铁律 (沿用上游流水线):
  - 只允许 ffmpeg 无损切割真视频; 非视频/切不开 -> 跳过并记录, **绝不二进制切块**, 也绝不动源文件
  - 源文件只在"该传的东西都上传且校验成功"之后才移走
  - 默认演练 (--dry); --apply 才真动
  - 每轮报告写到 中转远端:报告区/gd_out2_report.txt

用法:
  python3 tools/gd_out2_split.py                       # 演练, 只看计划
  python3 tools/gd_out2_split.py --apply --max-ops 20  # 真跑, 本轮最多 20 个源文件
"""
import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg          # 路径真值来自配置: CI=Secret PATHS_JSON, 本机=paths.local.json
SRC = pathcfg.require("GD_SRC")
DST = pathcfg.require("GD_DST")
ARCH = pathcfg.require("GD_ARCH")
WORK = os.environ.get("GD_WORK", "/tmp/gdout2")
REPORT = pathcfg.require("GD_REPORT")
FAILED = pathcfg.require("GD_FAILED")
MAX_FAIL = 3                     # 同一文件连续失败这么多次就不再重试(防一个坏文件每轮占坑)
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv")
GB = 1024 ** 3
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"


def find_conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf"),
              os.path.expanduser("~/AppData/Roaming/rclone/rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = find_conf()


def rclone(args, timeout=7200):
    return subprocess.run([RCLONE] + args + ["--config", CONF],
                          capture_output=True, text=True, errors="replace",
                          timeout=timeout)


def human(b):
    return "%.0f MB" % (b / 1048576) if b < GB else "%.2f GB" % (b / GB)


def free_bytes():
    try:
        u = shutil.disk_usage(WORK if os.path.isdir(WORK) else "/")
        return u.free
    except Exception:
        return 0


def lsjson(remote, timeout=900):
    r = rclone(["lsjson", remote, "--files-only"], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:160]))
    items = json.loads(r.stdout or "[]")
    return {x["Name"]: int(x.get("Size") or 0) for x in items}


def stat_size(path):
    """单个文件的远端大小; 不存在返回 None"""
    r = rclone(["lsjson", "--stat", path], timeout=600)
    if r.returncode != 0:
        return None
    try:
        return int(json.loads(r.stdout or "{}").get("Size") or 0)
    except Exception:
        return None


def refresh_gd_token():
    """跑 rclone 前刷新 源远端 的 access_token (见 tools/gd_token.py)"""
    try:
        import gd_token
        return gd_token.main() == 0
    except Exception as e:
        print("  !! gd_token 刷新异常: %s" % str(e)[:150])
        return False


def load_failed():
    """连续失败清单: {文件名: {count, note, size, time}} (存云端, runner 无状态)"""
    tmp = os.path.join(WORK, "failed.json")
    r = rclone(["copyto", FAILED, tmp, "--retries", "2"], timeout=600)
    if r.returncode != 0 or not os.path.exists(tmp):
        return {}
    try:
        with open(tmp, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_failed(fails):
    if not fails:
        try:
            rclone(["deletefile", FAILED, "--retries", "2"], timeout=600)
        except Exception:
            pass
        return
    tmp = os.path.join(WORK, "failed.json")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fails, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("  !! 失败清单写入异常: %s" % str(e)[:80])
        return
    rclone(["copyto", tmp, FAILED, "--retries", "3"], timeout=600)


def local_size(p):
    return os.path.getsize(p) if os.path.exists(p) else 0


def rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _dur(p):
    try:
        return float(subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", p], text=True).strip() or 0)
    except Exception:
        return 0.0


def _seg_once(src_path, stem, ext, seg_time):
    """用 segment muxer 按关键帧切一次; 返回切出的段(按名排序)"""
    for old in glob.glob(glob.escape(os.path.join(WORK, stem)) + ".part???" + ext):
        rm(old)
    pat = os.path.join(WORK, stem + ".part%03d" + ext)
    subprocess.check_call([FFMPEG, "-y", "-v", "error", "-i", src_path,
                           "-c", "copy", "-f", "segment",
                           "-segment_start_number", "1",
                           "-segment_time", str(seg_time),
                           "-reset_timestamps", "1", pat])
    return sorted(glob.glob(glob.escape(os.path.join(WORK, stem)) + ".part???" + ext))


def split_max(src_path, stem, ext, max_bytes):
    """ffmpeg 无损切成 N 段, 保证每段 <= max_bytes(切点仍落在关键帧上, -c copy 不重编码)。

    为什么不再"对半切": 中转盘 out2 的唯一去向(下游归档盘)对**单文件**有 ~300MB 硬上限,
    源文件 >600MB 时对半切出来的两段各自仍 >300MB -> 两段都永远传不进 /dbjm,
    连带中转源盘 out2 里的原件也永远归档不了。
    这里按 ceil(大小 / max_bytes) 算段数, 切完校验每段 <= max_bytes;
    关键帧不均匀导致某段超标就按 0.9 缩短段时长重试(最多 6 次)。
    返回段路径列表(按名排序); 切不出合格结果返回 []。
    """
    dur = _dur(src_path)
    if dur < 2:
        print("    !! 时长异常(%.1fs), 放弃切割" % dur)
        return []
    size = local_size(src_path)
    want = max(2, int(math.ceil(size / float(max_bytes))))
    seg = dur / want
    for _ in range(6):
        if seg < 1:
            break
        try:
            parts = _seg_once(src_path, stem, ext, seg)
        except Exception as e:
            print("    !! ffmpeg 切割失败(seg=%.1fs): %s" % (seg, str(e)[:70]))
            seg *= 0.9
            continue
        if len(parts) >= 2:
            sizes = [local_size(p) for p in parts]
            over = [s for s in sizes if s > max_bytes]
            if not over:
                print("    %d 段 (seg≈%.1fs): %s" % (
                    len(parts), seg, " + ".join("%.0f MB" % (s / 1048576) for s in sizes)))
                return parts
            print("    (seg≈%.1fs -> %d 段, 其中 %d 段超 %.0fMB, 缩短重试)"
                  % (seg, len(parts), len(over), max_bytes / 1048576))
        else:
            print("    (seg≈%.1fs 只切出 %d 段, 缩短重试)" % (seg, len(parts)))
        for p in parts:
            rm(p)
        seg *= 0.9
    return []


def pick_batch(cands, budget, max_files, thresh, too_big):
    """按"本批总量 + 本批最大的待切文件"估算峰值, 不超过 budget

    切大文件时源文件与两段同时在盘上 -> 峰值 = 本批源文件总量 + 当前文件大小。
    """
    batch, total, mx = [], 0, 0
    for name, size in cands:
        if len(batch) >= max_files:
            break
        if not batch and size * 2 > budget:          # 单个就放不下(源+段)
            too_big.append((name, size))
            continue
        new_mx = max(mx, size if size > thresh else 0)
        if total + size + new_mx > budget:
            if batch:
                break
            too_big.append((name, size))             # 第一个就塞不下, 少见
            continue
        batch.append((name, size))
        total += size
        mx = new_mx
    return batch, total, mx


def download_batch(batch):
    """一次性把这批文件拉到本地; 返回 {名字: 本地字节}"""
    names = [n for n, _ in batch]
    lf = os.path.join(WORK, "_files_from.txt")
    with open(lf, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    print("  ⬇ 批量下载 %d 个 (合计 %s)" % (len(names),
                                        human(sum(s for _, s in batch))))
    r = rclone(["copy", SRC, WORK, "--files-from", lf,
                "--transfers", "4", "--checkers", "8", "--no-traverse",
                "--retries", "3", "--low-level-retries", "10", "--stats", "30s",
                "--stats-one-line"], timeout=10800)
    if r.returncode != 0:
        print("  !! 批量下载报错(继续处理已下好的): %s" % (r.stderr or "")[:200])
    got = {}
    for n, s in batch:
        lp = os.path.join(WORK, n)
        if local_size(lp) == s:
            got[n] = s
        else:
            print("  !! %s 本地不全 (%s / 期望 %s)" % (n, human(local_size(lp)), human(s)))
            rm(lp)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真动; 不加则只演练")
    ap.add_argument("--max-ops", type=int, default=200, help="本轮最多处理多少个源文件")
    ap.add_argument("--threshold-mb", type=int, default=300, help="超过该大小就切片上传")
    ap.add_argument("--seg-mb", type=int, default=280,
                    help="切片单片上限(MB); 下游网盘对单文件有 ~300MB 硬上限, 留 20MB 余量")
    ap.add_argument("--budget-min", type=int, default=310, help="本轮时间预算(分钟), 到点收工")
    ap.add_argument("--max-total-gb", type=float, default=5.0,
                    help="本轮处理的源文件总量上限(GB), 到了就收工")
    ap.add_argument("--min-total-gb", type=float, default=4.0,
                    help="本轮总量目标下限(GB); 凑不到(没文件了)会提示")
    ap.add_argument("--disk-budget-gb", type=float, default=0,
                    help="每批占用的磁盘上限(GB); 0=按剩余空间自动算(留 --reserve-gb)")
    ap.add_argument("--reserve-gb", type=float, default=2.0, help="给系统留的余量(GB)")
    ap.add_argument("--min-free-gb", type=float, default=1.0, help="低于此剩余空间就停止再下新的")
    ap.add_argument("--only", default="", help="只处理指定文件名(调试用)")
    a = ap.parse_args()
    thresh = a.threshold_mb * 1024 ** 2
    seg_bytes = a.seg_mb * 1024 ** 2
    budget_s = a.budget_min * 60
    reserve = int(a.reserve_gb * GB)
    max_total = int(a.max_total_gb * GB)
    min_total = int(a.min_total_gb * GB)
    t0 = time.time()

    os.makedirs(WORK, exist_ok=True)

    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append("# 源远端/out -> 中转远端/out2 报告  %s UTC  [%s]"
                 % (ts, "执行模式" if a.apply else "演练模式(dry-run)"))
    lines.append("# 规则: >%dMB 按 <=%dMB 切成 N 片; <=%dMB 直接上传; 上传校验通过后源文件移入 %s"
                 % (a.threshold_mb, a.seg_mb, a.threshold_mb, ARCH))
    print("rclone 配置: %s | ffmpeg: %s | 阈值 %dMB(单片<=%dMB) | 上限 %d 个/轮 | 本轮总量 %.1f~%.1f GB | 每批磁盘上限 %s"
          % (CONF, FFMPEG, a.threshold_mb, a.seg_mb, a.max_ops, a.min_total_gb, a.max_total_gb,
             ("%.1f GB" % a.disk_budget_gb) if a.disk_budget_gb else "自动(剩余-%.1fGB)" % a.reserve_gb))
    print("工作区: %s | 可用空间 %s" % (WORK, human(free_bytes())))
    lines.append("工作区 %s | 起始可用空间 %s | 预留 %s"
                 % (WORK, human(free_bytes()), human(reserve)))

    # ---- 刷 源远端 凭证 ----
    if not refresh_gd_token():
        lines.append("\n!! gd_token 刷新失败, 本轮中止 (源远端 访问不了)")
        finish(lines, "\n".join(lines))
        return 1

    # ---- 拉清单 ----
    try:
        src = lsjson(SRC)
    except Exception as e:
        lines.append("\n!! 读取 %s 失败: %s" % (SRC, str(e)[:200]))
        finish(lines, "\n".join(lines))
        return 1
    try:
        dst = lsjson(DST)
    except Exception as e:
        lines.append("\n!! 读取 %s 失败: %s" % (DST, str(e)[:200]))
        finish(lines, "\n".join(lines))
        return 1
    big_n = sum(1 for v in src.values() if v > thresh)
    print("%s: %d 个文件 (>%dMB 的 %d 个) | %s: %d 个文件"
          % (SRC, len(src), a.threshold_mb, big_n, DST, len(dst)))
    lines.append("\n源 %s: %d 个文件, 其中 >%dMB 的 %d 个 | 目标 %s: %d 个文件"
                 % (SRC, len(src), a.threshold_mb, big_n, DST, len(dst)))

    # 小文件优先, 先把不费劲的搬完
    todo = sorted(src.items(), key=lambda kv: (kv[1], kv[0]))
    if a.only:
        todo = [(n, s) for n, s in todo if n == a.only]
        if not todo:
            lines.append("\n!! 源里没有文件: %s" % a.only)
            finish(lines, "\n".join(lines))
            return 1
    lines.append("---")

    fails = load_failed()
    if fails:
        print("连续失败清单: %d 个文件" % len(fails))

    n_up_small = n_up_split = n_arch = n_already = 0
    skipped, failed, too_big = [], [], []
    done = 0
    total_proc = 0            # 本轮已处理(进了批次)的源文件总字节数
    pending = list(todo)
    batch_no = 0

    lines.append("本轮总量目标: %.1f ~ %.1f GB (%d 个候选文件)"
                 % (a.min_total_gb, a.max_total_gb, len(pending)))

    while pending:
        if done >= a.max_ops:
            lines.append("\n已达本轮上限 %d 个, 收工 (剩余下次继续)" % a.max_ops)
            print("已达本轮上限 %d 个" % a.max_ops)
            break
        left_total = max_total - total_proc
        if left_total <= 0:
            lines.append("\n✅ 已达本轮总量上限 %.1f GB (已处理 %s), 收工"
                         % (a.max_total_gb, human(total_proc)))
            print("✅ 已达总量上限 %.1f GB, 收工" % a.max_total_gb)
            break
        left_s = budget_s - (time.time() - t0)
        if left_s <= 0:
            lines.append("\n⏱ 达到时间预算 %d 分钟, 收工 (剩余下次继续)" % a.budget_min)
            print("⏱ 达到时间预算, 收工")
            break
        free = free_bytes()
        if free - reserve < a.min_free_gb * GB:
            lines.append("\n⚠ 剩余空间不足 (%s), 停止再下新批次" % human(free))
            print("⚠ 剩余空间不足 %s, 停止" % human(free))
            break
        budget = min(free - reserve, int(a.disk_budget_gb * GB) if a.disk_budget_gb else free - reserve)
        budget = min(budget, left_total)          # 本批不能超过本轮剩余总量
        batch, total, mx = pick_batch(pending, budget, a.max_ops - done, thresh, too_big)
        if not batch:
            print("没有能放进当前预算的文件了, 停止")
            lines.append("\n没有能放进剩余预算的文件了, 停止 (本轮已处理 %s)" % human(total_proc))
            break

        batch_no += 1
        total_proc += total
        peak = total + mx
        print("\n" + "=" * 60)
        print("批次 %d: %d 个文件, 合计 %s | 本轮累计 %s / %.1f GB | 预计峰值占用 %s (预算 %s)"
              % (batch_no, len(batch), human(total), human(total_proc), a.max_total_gb,
                 human(peak), human(budget)))
        lines.append("\n[批次 %d] %d 个文件 合计 %s | 本轮累计 %s/%.1fGB | 峰值约 %s / 预算 %s | 当时可用 %s"
                     % (batch_no, len(batch), human(total), human(total_proc), a.max_total_gb,
                        human(peak), human(budget), human(free)))

        if a.apply:
            got = download_batch(batch)
            pending = [(n, s) for n, s in pending if n not in {b[0] for b in batch}]
        else:
            got = {n: s for n, s in batch}          # 演练: 假装都下好了
            pending = [(n, s) for n, s in pending if n not in {b[0] for b in batch}]

        for name, size in batch:
            if time.time() - t0 > budget_s:
                lines.append("⏱ 时间预算用尽, 本批剩余文件留到下次")
                print("⏱ 时间预算用尽")
                pending = []
                break
            head = "[%s] %s" % (human(size), name)
            if a.apply and name not in got:
                failed.append((name, "下载不完整"))
                rec = fails.get(name, {"count": 0})
                rec.update({"count": rec.get("count", 0) + 1, "note": "下载不完整",
                            "size_mb": round(size / 1048576, 1),
                            "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                fails[name] = rec
                lines.append("%s | !! 下载不完整, 未上传未归档" % head)
                continue
            if fails.get(name, {}).get("count", 0) >= MAX_FAIL:
                skipped.append(name)
                lines.append("%s | 已连续失败 %d 次, 本轮跳过 (详情见 %s)"
                             % (head, fails[name]["count"], FAILED))
                print("  ⏭ %s 连续失败 %d 次, 跳过" % (name, fails[name]["count"]))
                continue
            lp = os.path.join(WORK, name)
            try:
                if size <= thresh:
                    # ---- 小文件: 直接上传本地这份 ----
                    if dst.get(name) == size:
                        note = "已在 out2 且大小一致, 跳过上传"
                        ok = True
                        n_already += 1
                    elif not a.apply:
                        note = "(演练) 将直接上传 -> %s" % DST
                        ok = True
                    else:
                        print("\n[%s] ⬆ 直接上传: %s" % (human(size), name))
                        r = rclone(["copyto", lp, "%s/%s" % (DST, name),
                                    "--retries", "3", "--low-level-retries", "10",
                                    "--stats", "0"])
                        got_sz = stat_size("%s/%s" % (DST, name))
                        if r.returncode == 0 and got_sz == size:
                            note = "直接上传 OK (%s)" % human(size)
                            ok = True
                            n_up_small += 1
                            dst[name] = size
                        else:
                            note = "上传失败 rc=%d 远端大小=%s 期望=%d :: %s" % (
                                r.returncode, got_sz, size, (r.stderr or "")[:120])
                            ok = False
                else:
                    # ---- 大文件: 按 <= seg_mb 切成 N 片 (下游网盘对单文件有 ~300MB 硬上限) ----
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in VIDEO_EXT:
                        note = ">阈值但非视频, 不切割不上传 (源文件保持原样)"
                        ok = False
                    else:
                        stem = os.path.splitext(name)[0]
                        have = {k: v for k, v in dst.items()
                                if k.startswith(stem + ".part") and k.endswith(ext)}
                        if have and (stem + ".part001" + ext) in have \
                                and abs(sum(have.values()) - size) <= size * 0.05:
                            note = "分片已都在 out2 (%d 片), 跳过上传" % len(have)
                            ok = True
                            n_already += 1
                        elif not a.apply:
                            note = "(演练) 将按 <=%dMB 切片上传: %s.part001%s ..." % (
                                a.seg_mb, stem, ext)
                            ok = True
                        else:
                            print("\n[%s] ✂ 按 <=%dMB 切片: %s" % (human(size), a.seg_mb, name))
                            parts = split_max(lp, stem, ext, seg_bytes)
                            total_p = sum(local_size(p) for p in parts)
                            if (len(parts) < 2
                                    or any(local_size(p) > seg_bytes for p in parts)
                                    or abs(total_p - size) > size * 0.05):
                                note = ("切割结果异常: %s 段, 合计 %s / 原 %s"
                                        % (len(parts), human(total_p), human(size)))
                                ok = False
                            else:
                                ups = []
                                for p in parts:
                                    bn = os.path.basename(p)
                                    rr = rclone(["copyto", p, "%s/%s" % (DST, bn),
                                                 "--retries", "3", "--low-level-retries", "10",
                                                 "--stats", "0"])
                                    got_sz = stat_size("%s/%s" % (DST, bn))
                                    ups.append((bn, rr.returncode == 0 and got_sz == local_size(p),
                                                local_size(p), got_sz))
                                if all(u[1] for u in ups):
                                    note = "切片并上传 OK (%d 片): " % len(ups) + " + ".join(
                                        "%s(%s)" % (u[0], human(u[2])) for u in ups)
                                    ok = True
                                    n_up_split += 1
                                    for u in ups:
                                        dst[u[0]] = u[2]
                                else:
                                    bad = [u for u in ups if not u[1]]
                                    note = "分段上传失败: " + "; ".join(
                                        "%s 远端=%s" % (u[0], u[3] if u[3] is not None else "缺失")
                                        for u in bad)
                                    ok = False
                            for p in parts:
                                rm(p)

                # ---- 归档源文件 (上传成功就移走; "已在目标"也补一次归档) ----
                arch_note = ""
                if a.apply and ok:
                    print("  📦 源文件移入 %s" % ARCH)
                    r = rclone(["moveto", "%s/%s" % (SRC, name), "%s/%s" % (ARCH, name),
                                "--retries", "3", "--low-level-retries", "10"])
                    if r.returncode == 0:
                        n_arch += 1
                        arch_note = " | 已归档到 %s" % ARCH
                    else:
                        arch_note = " | !! 归档失败 rc=%d: %s" % (r.returncode, (r.stderr or "")[:100])
                elif not a.apply and ok:
                    arch_note = " | (演练) 随后归档到 %s" % ARCH

                done += 1
                print("  -> %s%s" % (note, arch_note))
                lines.append("%s | %s%s" % (head, note, arch_note))
                if ok:
                    fails.pop(name, None)               # 成功就洗掉失败记录
                elif "非视频" in note:
                    skipped.append(name)                # 非视频: 不算失败, 但要人看一眼
                else:
                    rec = fails.get(name, {"count": 0})
                    rec.update({"count": rec.get("count", 0) + 1, "note": note[:200],
                                "size_mb": round(size / 1048576, 1),
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                    fails[name] = rec
                    failed.append((name, note))
            except Exception as e:
                done += 1
                msg = "异常: %s" % str(e)[:160]
                print("  !! " + msg)
                lines.append("%s | !! %s" % (head, msg))
                rec = fails.get(name, {"count": 0})
                rec.update({"count": rec.get("count", 0) + 1, "note": msg,
                            "size_mb": round(size / 1048576, 1),
                            "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                fails[name] = rec
                failed.append((name, msg))
            finally:
                if a.apply:
                    rm(lp)                              # 腾空间给下一批

    if a.apply:
        save_failed({k: v for k, v in fails.items() if v.get("count")})
    if not a.apply:
        lines.append("\n(演练模式: 未做任何改动; 加 --apply 生效)")

    remain = len(src) - n_arch
    lines.append("\n---")
    lines.append("本轮 %d 批 | 处理 %d 个 | 小文件直传 %d | 切片上传 %d | 已在目标 %d | 归档源文件 %d | 跳过 %d | 失败 %d"
                 % (batch_no, done, n_up_small, n_up_split, n_already, n_arch,
                    len(skipped), len(failed)))
    lines.append("本轮处理总量 %s (%.2f GB) | 目标 %.1f~%.1f GB | %s"
                 % (human(total_proc), total_proc / GB, a.min_total_gb, a.max_total_gb,
                    "达标" if total_proc >= min_total else
                    ("已达上限" if total_proc >= max_total else "偏少(文件不够/时间到)")))
    lines.append("结束可用空间 %s" % human(free_bytes()))
    if total_proc < min_total and a.apply and pending:
        lines.append("注: 本轮总量不足 %.1f GB, 剩余 %d 个候选文件下次继续"
                     % (a.min_total_gb, len(pending)))
    if skipped:
        lines.append("跳过(非视频/连续失败, 源文件保持原样): " + ", ".join(skipped[:10]))
    if too_big:
        lines.append("放不下(单个文件超过每批空间): " + ", ".join(
            "%s(%s)" % (n, human(s)) for n, s in too_big[:8]))
    if failed:
        lines.append("失败明细:")
        for n, m in failed[:15]:
            lines.append("  - %s :: %s" % (n, m))
    lines.append("源目录预计剩余 %d 个文件待处理" % max(remain, 0))
    lines.append("模式: %s | 用时 %.1f 分钟" % ("执行" if a.apply else "预览",
                                              (time.time() - t0) / 60))
    print("\n合计: %d 批 | 总量 %s | 直传 %d | 切传 %d | 归档 %d | 跳过 %d | 失败 %d | 剩余约 %d"
          % (batch_no, human(total_proc), n_up_small, n_up_split, n_arch, len(skipped),
             len(failed),
             max(remain, 0)))
    finish(lines, "\n".join(lines))
    return 0


def finish(lines, text):
    """报告落盘 + 上传到 中转远端:报告区/"""
    rp = os.path.join(WORK, "gd_out2_report.txt")
    try:
        with open(rp, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception as e:
        print("!! 本地报告写入失败: %s" % str(e)[:100])
    r = rclone(["copyto", rp, REPORT, "--retries", "3"], timeout=900)
    if ":" not in REPORT:                      # 本地路径(调试): 直接拷
        try:
            shutil.copyfile(rp, REPORT)
            r = None
        except Exception as e:
            print("!! 报告拷贝失败: %s" % str(e)[:80])
    print("报告 -> %s: %s" % (REPORT, "OK" if (r is None or r.returncode == 0) else
                              "失败 " + (r.stderr or "")[:100]))


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
