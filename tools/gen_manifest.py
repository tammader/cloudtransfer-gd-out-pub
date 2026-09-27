# -*- coding: utf-8 -*-
"""天翼编号清单导出 -> OneDrive2/dbqd  (云端版, GitHub Actions 上运行)

复刻本机 export_compare.py 的产出, 让云端流水线不再依赖本机:
  1) /天翼个人/out2 + /天翼家庭/out2 的编号      -> onedrive2:dbqd/compare_stems.txt
  2) /天翼个人/out2 名字+大小                    -> onedrive2:dbqd/ty_out.tsv
  3) /天翼个人/TelegramVideos 名字+大小          -> onedrive2:dbqd/ty_tg.tsv
  4) 本轮结果                                    -> onedrive2:dbqd/gen_manifest_report.txt

跑法: runner 上现装 OpenList 挂天翼, 列目录走 alist /api/fs/list, 上传走 rclone(onedrive2)。
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
OD_DIR = os.environ.get("GM_OD", "onedrive2:dbqd")
# 编号清单覆盖的目录 (逗号分隔的 alist 路径)
STEM_DIRS = os.environ.get("GM_STEM_DIRS", "/天翼个人/out2,/天翼家庭/out2")
# 带大小清单: (alist 路径, 输出文件名)
SIZE_MANIFESTS = [
    ("/天翼个人/out2", "ty_out.tsv"),
    ("/天翼个人/TelegramVideos", "ty_tg.tsv"),
]
REPORT = os.environ.get("GM_REPORT", "onedrive2:dbqd/gen_manifest_report.txt")
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

    header = ("# 天翼个人/out2 + 天翼家庭/out2 编号清单\n"
              "# 由云端 gen-manifest (tools/gen_manifest.py) 生成, 云端流水线读它比对\n"
              "# 更新时间: %s | 文件 %d 个 | 编号 %d 个\n"
              % (time.strftime("%Y-%m-%d %H:%M:%S"), total_files, len(have)))
    tmp = "/tmp/gen_compare_stems.txt"
    io.open(tmp, "w", encoding="utf-8").write(header + "\n".join(sorted(have)) + "\n")
    ok_stem = upload(tmp, OD_DIR + "/compare_stems.txt", "compare_stems.txt")
    log("  编号 %d 个 -> %s/compare_stems.txt (%s)" % (len(have), OD_DIR, ", ".join(parts)))

    # 带大小清单
    size_desc = []
    for remote, fname in SIZE_MANIFESTS:
        try:
            items = alist_list(remote)
            rows = ["%s\t%d" % (n, s) for (n, s, isd) in items if not isd]
            tf = "/tmp/gen_" + fname
            io.open(tf, "w", encoding="utf-8").write(
                "# %s 清单 (名字<TAB>字节) 更新: %s\n"
                % (remote, time.strftime("%Y-%m-%d %H:%M:%S")) + "\n".join(rows) + "\n")
            ok = upload(tf, OD_DIR + "/" + fname, fname)
            size_desc.append("%s %s %d 条" % (fname, "OK" if ok else "FAIL", len(rows)))
            log("  %-12s -> %s %d 条 (%s)" % (fname, OD_DIR, len(rows), "OK" if ok else "FAIL"))
        except Exception as e:
            size_desc.append("%s 异常" % fname)
            problems.append("%s: %s" % (remote, str(e)[:90]))
            log("  !! %s 导出异常: %s" % (fname, str(e)[:90]))

    # 报告
    lines = ["# 云端天翼清单导出报告  更新: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
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
    sys.exit(main())
