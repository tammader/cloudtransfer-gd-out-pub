# -*- coding: utf-8 -*-
"""目标网盘编号清单导出 -> 中转远端/报告区  (云端版, GitHub Actions 上运行)

复刻本机 export_compare.py 的产出, 让云端流水线不再依赖本机:
  1) /目标网盘/out2 + /副网盘/out2 的编号      -> 中转远端:报告区/compare_stems.txt
  2) /目标网盘/out2(+副网盘/out2) 名字+大小      -> 中转远端:报告区/ty_out.tsv   (可多目录合并)
  3) /目标网盘/TG目录 名字+大小          -> 中转远端:报告区/ty_tg.tsv
  4) 本轮结果                                    -> 中转远端:报告区/gen_manifest_report.txt

跑法: runner 上现装 OpenList 挂云盘, 列目录走 alist /api/fs/list, 上传走 rclone(中转远端)。
环境: ALIST_URL(http://127.0.0.1:5244), ALIST_TOKEN, ~/.config/rclone/rclone.conf
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
import pathcfg          # 路径真值来自配置: CI=Secret PATHS_JSON, 本机=paths.local.json
OD_DIR = pathcfg.require("GM_OD")
# 编号清单覆盖的目录 (逗号分隔的 alist 路径)
STEM_DIRS = pathcfg.require("GM_STEM_DIRS")
# 带大小清单: (alist 路径, 输出文件名)
#   路径支持**逗号分隔多目录** -> 合并成一份, 先列到的目录优先(同名不覆盖)。
#   为什么要多目录: 主网盘空间不够, 会不断把 out2 的文件挪到副网盘/out2;
#   清单只扫一个目录的话, 被挪走的文件在消费方(out-sync)眼里就"天翼没有" -> 重复上传回源。
SIZE_MANIFESTS = [
    (pathcfg.require("GM_TY_OUT"), "ty_out.tsv"),
    (pathcfg.require("GM_TY_TG"), "ty_tg.tsv"),
]
REPORT = pathcfg.require("GM_REPORT")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def alist_token():
    t = os.environ.get("ALIST_TOKEN", "").strip()
    if not t:
        raise SystemExit("!! 需要 ALIST_TOKEN")
    return t


def alist_list(path):
    """列目录 -> [(name, size, is_dir), ...]"""
    req = urllib.request.Request(
        ALIST + "/api/fs/list",
        data=json.dumps({"path": path, "page": 1, "per_page": 0, "refresh": True}).encode(),
        method="POST",
        headers={"Authorization": alist_token(), "Content-Type": "application/json"})
    d = json.loads(OP.open(req, timeout=180).read().decode("utf-8", "replace"))
    if d.get("code") != 200:
        raise RuntimeError("alist list %s -> %s %s" % (path, d.get("code"), d.get("message")))
    items = (d.get("data") or {}).get("content") or []
    return [(x["name"], int(x.get("size") or 0), bool(x.get("is_dir"))) for x in items]


def stem(name):
    """复刻本机 export_compare.py: 去扩展名/_D/前缀/.partNNN, 小写"""
    s = os.path.splitext(name)[0]
    s = re.sub(r"_D$", "", s)
    s = re.sub(r"^(已处理_|D_)", "", s)
    s = re.sub(r"\.part\d+$", "", s)
    return s.strip().lower()


def conf_path():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


def rclone(args, timeout=1800):
    return subprocess.run([RCLONE] + args + ["--config", conf_path()],
                          capture_output=True, text=True, errors="replace", timeout=timeout)


def upload(local, remote, label):
    rc = rclone(["copyto", local, remote, "--retries", "3", "--retries-sleep", "5s"])
    if rc.returncode != 0:
        log("  !! %s 上传失败: %s" % (label, (rc.stderr or "")[:160]))
        return False
    return True


def main():
    dirs = [d.strip() for d in STEM_DIRS.split(",") if d.strip()]
    have, total_files, parts, problems = set(), 0, [], []
    for d in dirs:
        try:
            items = alist_list(d)
        except Exception as e:
            parts.append("%s 读取异常" % d)
            problems.append("%s: %s" % (d, str(e)[:110]))
            log("  !! %s 读取异常: %s" % (d, str(e)[:110]))
            continue
        names = [n for (n, s, isd) in items if not isd]
        for n in names:
            have.add(stem(n))
        total_files += len(names)
        parts.append("%s %d 个" % (d, len(names)))
        log("  %-26s %d 个文件" % (d, len(names)))

    if not have:
        log("!! 没拿到任何编号(两处都读不到?), 不覆盖清单")
        return 1

    header = ("# 目标网盘/out2 + 副网盘/out2 编号清单\n"
              "# 由云端 gen-manifest (tools/gen_manifest.py) 生成, 云端流水线读它比对\n"
              "# 更新时间: %s | 文件 %d 个 | 编号 %d 个\n"
              % (time.strftime("%Y-%m-%d %H:%M:%S"), total_files, len(have)))
    tmp = "/tmp/gen_compare_stems.txt"
    io.open(tmp, "w", encoding="utf-8").write(header + "\n".join(sorted(have)) + "\n")
    ok_stem = upload(tmp, OD_DIR + "/compare_stems.txt", "compare_stems.txt")
    log("  编号 %d 个 -> %s/compare_stems.txt (%s)" % (len(have), OD_DIR, ", ".join(parts)))

    # 带大小清单: 一个 spec 可含多个目录(逗号分隔) -> 合并成一份, 先列到的目录优先
    size_desc = []
    for spec, fname in SIZE_MANIFESTS:
        dirs = [d.strip() for d in spec.split(",") if d.strip()]
        try:
            merged, per, conf = {}, [], []
            for d in dirs:
                cnt = 0
                for (n, s, isd) in alist_list(d):
                    if isd:
                        continue
                    cnt += 1
                    if n in merged:
                        # 两个目录都有同名: 大小一致=正常镜像; 不一致要告警, 别静默取一个
                        if merged[n] != s:
                            conf.append("%s(%d vs %d)" % (n, merged[n], s))
                        continue
                    merged[n] = s
                per.append("%s %d 个" % (d, cnt))
            rows = ["%s\t%d" % (n, merged[n]) for n in sorted(merged)]
            tf = "/tmp/gen_" + fname
            io.open(tf, "w", encoding="utf-8").write(
                "# %s 清单 (名字<TAB>字节) 更新: %s\n"
                "# 来源目录: %s\n"
                % (spec, time.strftime("%Y-%m-%d %H:%M:%S"), ", ".join(dirs))
                + "\n".join(rows) + "\n")
            ok = upload(tf, OD_DIR + "/" + fname, fname)
            size_desc.append("%s %s %d 条" % (fname, "OK" if ok else "FAIL", len(rows)))
            log("  %-12s -> %s %d 条 (%s) [%s]"
                % (fname, OD_DIR, len(rows), "OK" if ok else "FAIL", "; ".join(per)))
            if conf:
                problems.append("%s 同名不同大小 %d 条: %s"
                                % (fname, len(conf), "; ".join(conf[:5])))
        except Exception as e:
            size_desc.append("%s 异常" % fname)
            problems.append("%s: %s" % (spec, str(e)[:90]))
            log("  !! %s 导出异常: %s" % (fname, str(e)[:90]))

    # 报告
    lines = ["# 云端云盘清单导出报告  更新: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "编号清单: %s" % ("OK" if ok_stem else "FAIL"),
             "编号数: %d (文件 %d 个)" % (len(have), total_files),
             "覆盖: %s" % ", ".join(parts),
             "带大小清单: %s" % "; ".join(size_desc)]
    if problems:
        lines.append("")
        lines.append("## 读取异常")
        lines += ["- " + p for p in problems]
    rp = "/tmp/gen_manifest_report.txt"
    io.open(rp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    upload(rp, REPORT, "report")
    log("  报告 -> %s" % REPORT)
    # 若编号清单上传失败 -> 非零(让 run 标红)
    return 0 if ok_stem else 1


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
