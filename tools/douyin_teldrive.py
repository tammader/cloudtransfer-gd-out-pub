# -*- coding: utf-8 -*-
"""抖音收藏 -> teldrive 备份流水线 (GitHub Actions / 本机通用)

为什么"下载再上传":
  抖音视频直链是 CDN 短时链接, rclone 没有抖音 backend, 无法云端直接 copy。
  所以: 取清单(douyin_fetch.py) -> 逐个下到 /tmp -> rclone copyto 上传 teldrive。
  teldrive 侧必须用**补丁版 rclone**(minChunk 4Mi / smallchunk): 8Mi 分片 + 关
  BLAKE3 树哈希 + 关单文件多线程(否则同一 upload session 并发写 -> 409)。

脱敏(仓库公开, Actions 日志任何人可读):
  `logmask.install()` 把 stdout/stderr 包一层, 文件名/路径一律换成 <file:hash>/<path:hash>。

稳定性(大哥要求「稳定不赶时间」):
  * 每个文件独立: 失败重试后记录跳过, 一个坏文件绝不拖死整轮;
  * 断点幂等: 目标端按「大小一致」判断, 已传的永远跳过;
  * 时间预算 / 单轮总量 / 单文件上限 三重闸门, 到点优雅收工;
  * 只下当前这一个文件, 传完立刻删 -> 磁盘占用恒定 = 单个文件大小。

环境变量:
  DOUYIN_COOKIE   : 取数用 (douyin_fetch 需要)
  DOUYIN_FAV_JSON : 可直接给现成清单, 跳过 fetch (本机验证用)
  TD_STATS_JSON   : 可选, 统计 JSON 落点
用法:
  python douyin_teldrive.py --dry
  python douyin_teldrive.py --apply --budget-min 60 --max-files 20
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import logmask                                   # noqa: E402
    if os.environ.get("LOG_MASK", "1") != "0":
        logmask.install()
except Exception:
    pass

WORK = os.environ.get("DY_WORK", "/tmp/dywork")
DST = os.environ.get("DY_TD_DST", "teldrive:douyin-backup")
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
GB = 1024 ** 3
MB = 1024 ** 2

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def human(b):
    if b < MB:
        return "%.0f KB" % (b / 1024)
    return "%.1f MB" % (b / MB) if b < GB else "%.2f GB" % (b / GB)


def safe_name(v):
    """用 aweme_id + 清洗后的 desc 组成稳定文件名, 保证幂等(同一视频永远同名)。"""
    aid = str(v.get("aweme_id") or "")
    desc = (v.get("desc") or "").strip()
    desc = re.sub(r"[\r\n\t]+", " ", desc)
    desc = re.sub(r'[\\/:*?"<>|#]+', "_", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > 60:
        desc = desc[:60]
    base = ("%s_%s" % (aid, desc)).strip("_") if desc else aid
    return base + ".mp4"


def load_favorites(a):
    """优先用现成 JSON(本机验证), 否则调用 douyin_fetch.py 现场取。"""
    p = os.environ.get("DOUYIN_FAV_JSON") or ""
    if p and os.path.exists(p):
        log("使用现成清单: %s" % p)
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f).get("items", [])
    # 现场取数
    out = os.path.join(WORK, "favorites.json")
    cmd = [sys.executable, os.path.join(HERE, "douyin_fetch.py"),
           "--out", out, "--max", str(a.max_fetch)]
    log("现场取数: %s" % " ".join(cmd[:3]))
    r = subprocess.run(cmd)
    if r.returncode != 0 or not os.path.exists(out):
        log("!! 取数失败(rc=%s)" % r.returncode)
        return []
    with open(out, encoding="utf-8-sig") as f:
        return json.load(f).get("items", [])


def collect_dst(base):
    try:
        r = subprocess.run(
            [RCLONE, "lsjson", base, "-R", "--files-only", "--no-modtime"],
            capture_output=True, text=True)
    except FileNotFoundError:
        log("!! 找不到 rclone 可执行文件(%s), 视为目标端为空" % RCLONE)
        return {}
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


def fetch(url, dest, retries=3):
    """下载单个视频。返回实际字节数或 None。多条候选 url 依次尝试。"""
    urls = [url] if isinstance(url, str) else list(url or [])
    for attempt in range(retries):
        for u in urls:
            try:
                got = 0
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                req = urllib.request.Request(u, headers={"User-Agent": UA,
                                                         "Referer": "https://www.douyin.com/"})
                with op.open(req, timeout=120) as r, open(dest, "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                if got > 0:
                    return got
            except Exception as e:
                log("     下载重试(候选)失败: %s" % str(e)[:90])
        time.sleep(3 + attempt * 5)
    return None


def upload(local, remote, retries=3):
    cmd = [
        RCLONE, "copyto", local, remote,
        "--teldrive-chunk-size", os.environ.get("TD_CHUNK", "8Mi"),
        "--teldrive-hash-enabled=false",     # 8Mi 分片下树哈希必不匹配
        "--teldrive-upload-concurrency", os.environ.get("TD_UP_CONC", "1"),
        "--multi-thread-streams", "0",       # 多线程写同一 session -> 409
        "--transfers", "1", "--checkers", "1",
        "--retries", str(retries), "--retries-sleep", "30s",
        "--low-level-retries", "10", "--contimeout", "60s",
        "--timeout", "0", "--stats", "0", "-v",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "rclone 不可用: %s" % RCLONE
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="不加=只列计划")
    ap.add_argument("--budget-min", type=int, default=60)
    ap.add_argument("--max-file-gb", type=float, default=0.5)
    ap.add_argument("--max-total-gb", type=float, default=5.0)
    ap.add_argument("--max-files", type=int, default=0, help="本轮最多真传几个(0=不限)")
    ap.add_argument("--max-fetch", type=int, default=200, help="取数上限")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--preview", type=int, default=10)
    a = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    t0 = time.time()
    log("目标 : %s" % DST)
    log("模式 : %s" % ("真跑(--apply)" if a.apply else "演练(只列计划)"))

    src = load_favorites(a)
    if not src:
        log("!! 收藏清单为空, 收工")
        dump_stats({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "error": "empty"})
        sys.exit(2)
    # 预估值缺失(0)时, 用单文件上限兜底, 避免把"尺寸未知"当成已存在
    log("源端收藏 %d 个 (预估 %s, 部分尺寸未知按下载实测)"
        % (len(src), human(sum(i.get("size") or 0 for i in src))))

    base = DST.rstrip("/")
    dst = collect_dst(base)
    log("目标端已有 %d 个 (%s)" % (len(dst), human(sum(dst.values()))))

    max_file = int(a.max_file_gb * GB)
    todo, skip_big, skip_dup = [], 0, 0
    for v in src:
        name = safe_name(v)
        s = int(v.get("size") or 0)
        if 0 < s > max_file:
            skip_big += 1
            continue
        if s > 0 and dst.get(name) == s:
            skip_dup += 1
            continue
        # 尺寸未知: 若目标端已有同名且非空, 视为已传, 跳过(避免重复)
        if s == 0 and dst.get(name):
            skip_dup += 1
            continue
        v["_name"] = name
        todo.append((name, s, v))
    log("待传 %d 个 (超限跳过 %d, 已备份 %d)" % (len(todo), skip_big, skip_dup))
    for name, s, _ in todo[:a.preview]:
        log("   + %s (%s)" % (name[:70], human(s) if s else "尺寸未知"))
    if len(todo) > a.preview:
        log("   ... 其余 %d 个省略" % (len(todo) - a.preview))

    st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "真跑" if a.apply else "演练",
          "src_files": len(src), "dst_files": len(dst),
          "todo_files": len(todo), "new_files": 0, "new_bytes": 0, "fail_files": 0}
    if not a.apply or not todo:
        dump_stats(st)
        return

    budget = a.budget_min * 60
    max_total = int(a.max_total_gb * GB)
    done_n = done_b = fail_n = 0
    tmpdir = os.path.join(WORK, "tmp")
    os.makedirs(tmpdir, exist_ok=True)

    for i, (name, s, v) in enumerate(todo, 1):
        if time.time() - t0 > budget:
            log("时间预算到点, 优雅收工(下次继续) —— 这不是失败")
            break
        if done_b >= max_total:
            log("本轮总量到上限 %s, 收工" % human(max_total))
            break
        if a.max_files and done_n >= a.max_files:
            log("已达本轮文件数上限 %d, 收工" % a.max_files)
            break
        local = os.path.join(tmpdir, name)
        log("[%d/%d] %s" % (i, len(todo), name[:70]))
        td0 = time.time()
        got = fetch(v.get("url_list") or v.get("url"), local, retries=a.retries)
        if got is None:
            fail_n += 1
            log("   !! 下载失败, 跳过")
            continue
        tdl = time.time() - td0
        tu0 = time.time()
        ok, msg = upload(local, "%s/%s" % (base, name), retries=a.retries)
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
        done_b += got
        log("   ok 下行 %.1fs(%.1f MiB/s) 上传 %.1fs(%.1f MiB/s) 累计 %s"
            % (tdl, got / MB / max(0.1, tdl), tu, got / MB / max(0.1, tu), human(done_b)))

    log("本轮: 新传 %d 个 / %s, 失败 %d" % (done_n, human(done_b), fail_n))
    st.update({"new_files": done_n, "new_bytes": done_b, "fail_files": fail_n,
               "minutes": round((time.time() - t0) / 60, 1)})
    dump_stats(st)


if __name__ == "__main__":
    main()
