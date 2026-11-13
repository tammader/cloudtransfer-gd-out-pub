# -*- coding: utf-8 -*-
"""gd6:out3 -> ydy2/ycl 单向备份(增量, 源端只读)

为什么走 alist 而不是 rclone 直连:
  ydy2 = 139 移动云盘(个人云), runner 上没有它的 rclone 远端 ——
  云端跑法是在 runner 里现装 OpenList, 用 Secret ALIST_STORAGES_TY 注入 /ydy2 挂载,
  上传走 alist 原生 PUT /api/fs/put (File-Path 头 + 原始 body)。
  这套是 ty-upload-test / out2-ty-sync / od2-dbjm 已经在跑的范式, 这次直接复用。

单向保证(每条都有代码守着):
  1) 源端只读: 只用 rclone lsjson / copyto 读 gd6:out3, 从不写、从不删;
  2) 目标端只新增: 同名同字节 = 已有 -> 跳过; 同名不同字节 = 冲突 -> 记录并跳过, **绝不覆盖**;
  3) 上传后必须 POST /api/fs/get (refresh=true) 校验字节数一致, 不一致记失败;
  4) 目标清单两路来源: alist 实时列(refresh=true 穿透 30 分钟缓存) + GY_STATE 已完成清单,
     alist 读不到/被限流时用清单兜底, 不会重复传;
  5) 单文件超 GY_MAX_FILE_MB 的跳过并记录(139 个人云对大文件有上限, 由调用方按需调);
  6) 默认演练(--dry), --apply 才真传。

配置(pathcfg: CI=Secret PATHS_JSON / 本机 paths.local.json):
  GY_SRC       源(rclone 路径), 例 gdrive6:out3
  GY_DST_DIR   目标(alist 挂载路径), 例 /ydy2/ycl
  GY_REPORT    报告落点(rclone 路径)
  GY_STATE     已完成清单(rclone 路径, TSV: 名字<TAB>字节)
可选(有默认值): GY_MAX_FILE_MB=2048 GY_MAX_FILES=200 GY_MAX_TOTAL_GB=50 GY_BUDGET_MIN=300
环境: ALIST_URL / ALIST_TOKEN / RCLONE_CONF_PATH / TG_TOKEN / TG_CHAT_ID
"""
import argparse
import concurrent.futures
import configparser
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pathcfg                                    # noqa: E402

try:
    import logmask
    logmask.install()
except Exception:
    pass

GB = 1024 ** 3
MB = 1024 ** 2
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244").rstrip("/")
WORK = os.environ.get("GY_WORK", "/tmp/gd6ydy2")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def conf_path():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = conf_path()


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def human(b):
    if not b:
        return "0 MB"
    return "%.1f MB" % (b / MB) if b < GB else "%.2f GB" % (b / GB)


# ---------------- alist ----------------
def alist_token():
    t = os.environ.get("ALIST_TOKEN", "").strip()
    if t:
        return t
    c = configparser.ConfigParser()
    c.read(CONF, encoding="utf-8")
    if not c.has_section("alist"):
        raise SystemExit("!! 既没有 ALIST_TOKEN, rclone.conf 里也没有 [alist] 段")
    return c["alist"].get("bearer_token", "")


TOKEN = alist_token()


def alist_post(path, payload, timeout=180):
    req = urllib.request.Request(ALIST + path, data=json.dumps(payload).encode(),
                                 headers={"Authorization": TOKEN,
                                          "Content-Type": "application/json"})
    d = json.loads(OP.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    if d.get("code") != 200:
        raise RuntimeError("alist %s -> %s %s" % (path, d.get("code"), d.get("message")))
    return d.get("data")


def alist_list(path, refresh=True):
    """-> {名字: (字节, 是否目录)}"""
    d = alist_post("/api/fs/list", {"path": path, "page": 1, "per_page": 0,
                                    "refresh": refresh})
    out = {}
    for x in (d or {}).get("content") or []:
        out[x["name"]] = (int(x.get("size") or 0), bool(x.get("is_dir")))
    return out


def alist_get(path, refresh=True):
    try:
        d = alist_post("/api/fs/get", {"path": path, "refresh": refresh})
    except Exception:
        return None
    if not d:
        return None
    return int(d.get("size") or 0), bool(d.get("is_dir"))


def alist_mkdir(path):
    return alist_post("/api/fs/mkdir", {"path": path})


class _Progress(object):
    """给 http.client 当 body: 边发边报进度 + 算速度"""

    def __init__(self, fh, total, label):
        self.fh, self.total, self.label = fh, total, label
        self.n, self.t0, self.last = 0, time.time(), 0.0

    def read(self, n=-1):
        b = self.fh.read(n)
        if b:
            self.n += len(b)
            now = time.time()
            if now - self.last >= 8:
                self.last = now
                el = max(now - self.t0, 0.001)
                sys.stdout.write("\r    ⬆ %s  %5.1f%%  %s/%s  %.1f MB/s   "
                                 % (self.label, 100.0 * self.n / max(self.total, 1),
                                    human(self.n), human(self.total), self.n / el / MB))
                sys.stdout.flush()
        return b

    @property
    def secs(self):
        return time.time() - self.t0


def alist_put(remote_path, local_file, size):
    with open(local_file, "rb") as fh:
        body = _Progress(fh, size, os.path.basename(remote_path)[:26])
        req = urllib.request.Request(ALIST + "/api/fs/put", data=body, method="PUT",
                                     headers={"Authorization": TOKEN,
                                              "File-Path": urllib.parse.quote(remote_path),
                                              "Content-Type": "application/octet-stream",
                                              "Content-Length": str(size)})
        d = json.loads(OP.open(req, timeout=7200).read().decode("utf-8", "replace"))
    sys.stdout.write("\n")
    if d.get("code") != 200:
        raise RuntimeError("上传失败: %s %s" % (d.get("code"), d.get("message")))
    return body.secs


# ---------------- rclone ----------------
def rclone(args, timeout=7200):
    env = dict(os.environ)
    return subprocess.run([RCLONE] + list(args) + ["--config", CONF], capture_output=True,
                          text=True, errors="replace", timeout=timeout, env=env)


def src_list(root):
    """源端清单 -> {相对路径: 字节}"""
    r = rclone(["lsjson", root, "-R", "--files-only", "--fast-list", "--no-modtime"], timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("列源失败: %s" % (r.stderr or "")[:200])
    out = {}
    for x in json.loads(r.stdout or "[]"):
        if not x.get("IsDir"):
            out[x["Path"]] = int(x.get("Size") or 0)
    return out


# ---------------- 已完成清单 ----------------
def load_state(path):
    if not path:
        return {}
    tmp = os.path.join(WORK, "state_in.tsv")
    os.makedirs(WORK, exist_ok=True)
    r = rclone(["copyto", path, tmp, "--retries", "2"], timeout=600)
    if r.returncode != 0 or not os.path.exists(tmp):
        log("   (清单读不到, 当空处理: %s)" % (r.stderr or "").strip()[:80])
        return {}
    out = {}
    for ln in io.open(tmp, encoding="utf-8", errors="replace").read().splitlines():
        if not ln or ln.startswith("#"):
            continue
        p = ln.rstrip("\n").split("\t")
        if len(p) >= 2:
            try:
                out[p[0]] = int(p[1])
            except Exception:
                pass
    return out


def save_state(path, done):
    if not path:
        return
    os.makedirs(WORK, exist_ok=True)
    tmp = os.path.join(WORK, "state_out.tsv")
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("# gd6:out3 -> ydy2/ycl 已完成清单  名字<TAB>字节  更新 %s\n"
                % time.strftime("%Y-%m-%d %H:%M:%S"))
        for k in sorted(done):
            f.write("%s\t%d\n" % (k, done[k]))
    r = rclone(["copyto", tmp, path, "--retries", "3"], timeout=900)
    log("   清单写回: %s" % ("OK" if r.returncode == 0 else (r.stderr or "")[:100]))


def tg_send(text):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not (tok and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        json.loads(OP.open(req, timeout=60).read().decode())
        return True
    except Exception as e:
        log("   TG 发送失败: %s" % str(e)[:100])
        return False


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真传; 不加=只演练")
    ap.add_argument("--max-file-mb", type=int, default=int(pathcfg.get("GY_MAX_FILE_MB", "2048")))
    ap.add_argument("--max-files", type=int, default=int(pathcfg.get("GY_MAX_FILES", "200")))
    ap.add_argument("--max-total-gb", type=float, default=float(pathcfg.get("GY_MAX_TOTAL_GB", "50")))
    ap.add_argument("--budget-min", type=int, default=int(pathcfg.get("GY_BUDGET_MIN", "300")))
    ap.add_argument("--only", default="", help="只处理名字含该子串的文件(调试)")
    ap.add_argument("--parallel", type=int, default=int(pathcfg.get("GY_PARALLEL", "1")),
                    help="同时传几个文件(多文件并发)。单流实测约 0.45 MB/s, 并发用来吃满带宽")
    ap.add_argument("--no-tg", action="store_true")
    a = ap.parse_args()

    SRC = pathcfg.require("GY_SRC")
    DST = pathcfg.require("GY_DST_DIR").rstrip("/")
    REPORT = pathcfg.get("GY_REPORT", "")
    STATE = pathcfg.get("GY_STATE", "")
    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)

    log("源 %s (只读) -> 目标 %s (alist 挂载)" % (SRC, DST))
    log("模式 %s | 单文件上限 %d MB | 本轮上限 %d 个 / %.0f GB | 时间预算 %d 分钟"
        % ("真传(--apply)" if a.apply else "演练", a.max_file_mb, a.max_files,
           a.max_total_gb, a.budget_min))

    # 目标目录
    d = alist_get(DST, refresh=True)
    if d is None:
        if a.apply:
            try:
                alist_mkdir(DST)
                log("目标目录不存在 -> 已创建 %s" % DST)
            except Exception as e:
                raise SystemExit("!! 目标目录 %s 不存在且创建失败: %s" % (DST, str(e)[:120]))
        else:
            log("!! 目标目录 %s 读不到(演练不创建; 真跑会自动 mkdir)" % DST)

    src = src_list(SRC)
    log("源文件 %d 个 / %s" % (len(src), human(sum(src.values()))))
    if not src:
        log("源端是空的, 收工")
        return 0

    done = load_state(STATE)
    log("已完成清单 %d 条" % len(done))

    # 目标现有(按需列父目录, 带缓存)
    cache = {}

    def target_has(rel):
        parent, _, name = rel.rpartition("/")
        key = parent
        if key not in cache:
            p = DST + ("/" + parent if parent else "")
            try:
                cache[key] = alist_list(p, refresh=True)
            except Exception as e:
                log("   !! 列目标目录 %s 失败(用清单兜底): %s" % (p, str(e)[:100]))
                cache[key] = {}
        return cache[key].get(name)

    todo, skip_done, conflict, oversize = [], [], [], []
    for rel, size in sorted(src.items()):
        if a.only and a.only not in rel:
            continue
        if size > a.max_file_mb * MB:
            oversize.append((rel, size))
            continue
        if done.get(rel) == size:
            skip_done.append((rel, size))
            continue
        hit = target_has(rel)
        if hit and hit[0] == size and not hit[1]:
            skip_done.append((rel, size))
            done[rel] = size
            continue
        if hit and not hit[1]:
            conflict.append((rel, size, hit[0]))
            continue
        todo.append((rel, size))

    plan_bytes = sum(s for _, s in todo)
    plan = []
    acc = 0.0
    for rel, size in todo:
        if len(plan) >= a.max_files or acc + size > a.max_total_gb * GB:
            break
        plan.append((rel, size))
        acc += size
    log("候选 %d 个 | 已在目标/清单 %d | 冲突(同名不同大小) %d | 超单文件上限 %d"
        % (len(todo), len(skip_done), len(conflict), len(oversize)))
    log("本轮计划 %d 个 / %s (剩余 %d 个下轮继续)"
        % (len(plan), human(sum(s for _, s in plan)), len(todo) - len(plan)))

    sent, sent_bytes, failed = 0, 0, []
    speeds = []
    lock = __import__("threading").Lock()

    def do_one(rel, size):
        """一个文件的完整链路: 下载 -> alist PUT -> 回读校验。返回 (状态, 说明, 上传秒数)"""
        lp = os.path.join(WORK, rel.replace("/", "_"))
        remote = DST + "/" + rel
        try:
            r = rclone(["copyto", "%s/%s" % (SRC.rstrip("/"), rel), lp,
                        "--retries", "3", "--low-level-retries", "10", "--stats", "0"], timeout=5400)
            if r.returncode != 0 or not os.path.exists(lp) or os.path.getsize(lp) != size:
                return ("fail", "下载失败/大小不符 %s" % (r.stderr or "")[:80], 0.0)
            els = alist_put(remote, lp, size)
            got = alist_get(remote, refresh=True)
            if not got or got[0] != size:
                return ("fail", "上传后校验不符(远端 %s / 本地 %s)" % (got[0] if got else "?", size), els)
            return ("ok", "", els)
        except Exception as e:
            return ("fail", str(e)[:130], 0.0)
        finally:
            if os.path.exists(lp):
                os.remove(lp)

    if a.apply:
        log("并发数 %d (单流基线约 0.45 MB/s, 看聚合能不能近似线性)" % a.parallel)
        pending = list(plan)
        idx = [0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            running = {}

            def submit_next():
                if pending and (time.time() - t0) / 60 <= a.budget_min:
                    rel, size = pending.pop(0)
                    idx[0] += 1
                    log("---- [%d/%d] %s (%s) 开工 ----" % (idx[0], len(plan), rel, human(size)))
                    running[ex.submit(do_one, rel, size)] = (rel, size)

            for _ in range(max(1, a.parallel)):
                submit_next()
            while running:
                done_set, _ = concurrent.futures.wait(
                    running, return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done_set:
                    rel, size = running.pop(f)
                    try:
                        st, msg, els = f.result()
                    except Exception as e:
                        st, msg, els = "fail", str(e)[:130], 0.0
                    if st == "ok":
                        with lock:
                            sent += 1
                            sent_bytes += size
                            done[rel] = size
                            sp = size / max(els, 0.001) / MB
                            speeds.append(sp)
                        log("   ✓ %s 用时 %.0fs (%.1f MB/s) | 校验 OK" % (rel, els, sp))
                        with lock:
                            if sent % 5 == 0:
                                save_state(STATE, done)
                    else:
                        with lock:
                            failed.append("%s: %s" % (rel, msg))
                        log("   !! %s 失败: %s" % (rel, msg))
                    submit_next()
            if (time.time() - t0) / 60 > a.budget_min and pending:
                log("到时间预算, 收工(剩 %d 个下轮继续)" % len(pending))
    save_state(STATE, done)

    lines = ["# gd6:out3 -> ydy2/ycl 单向备份报告  %s  [%s]"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练"),
             "# 源 %s (只读) -> 目标 %s | 判据: 目标同名同字节 = 已有; 同名不同字节 = 冲突不覆盖" % (SRC, DST),
             "源文件        : %d 个 / %s" % (len(src), human(sum(src.values()))),
             "目标已有(清单): %d 个" % len(skip_done),
             "冲突(跳过)    : %d 个%s" % (len(conflict),
                                      ("  例: " + ", ".join(x[0] for x in conflict[:3])) if conflict else ""),
             "超单文件上限  : %d 个(> %d MB)%s" % (len(oversize), a.max_file_mb,
                                              ("  例: " + ", ".join("%s(%s)" % (x[0], human(x[1])) for x in oversize[:3])) if oversize else ""),
             "本轮计划      : %d 个 / %s" % (len(plan), human(sum(s for _, s in plan))),
             "本轮实传      : %d 个 / %s" % (sent, human(sent_bytes)),
             "失败          : %d 个" % len(failed),
             "耗时          : %.1f 分钟" % ((time.time() - t0) / 60)]
    if speeds:
        lines.append("单文件上传速度: 中位 %.1f MB/s | 最快 %.1f | 最慢 %.1f (n=%d)"
                     % (sorted(speeds)[len(speeds) // 2], max(speeds), min(speeds), len(speeds)))
    if sent_bytes:
        wall_min = max((time.time() - t0) / 60, 1e-6)
        lines.append("聚合速率      : %.2f MB/s (并发 %d, 含下载/校验的端到端)"
                     % (sent_bytes / (wall_min * 60) / MB, a.parallel))
    for f in failed[:40]:
        lines.append("!! %s" % f)
    if len(todo) > len(plan):
        lines.append("")
        lines.append("剩余 %d 个未处理, 下轮继续(时间/数量上限)" % (len(todo) - len(plan)))
    body = "\n".join(lines)
    print(body, flush=True)

    if REPORT:
        rp = os.path.join(WORK, "gd6_ydy2_report.txt")
        io.open(rp, "w", encoding="utf-8", newline="\n").write(body + "\n")
        r = rclone(["copyto", rp, REPORT, "--retries", "3"], timeout=900)
        log("报告 -> %s: %s" % (REPORT, "OK" if r.returncode == 0 else (r.stderr or "")[:120]))
    if not a.no_tg:
        head = "📦 gd6:out3 → ydy2/ycl %s\n源 %d 个 / %s | 本轮传 %d 个 / %s\n失败 %d"
        tg_send(head % ("· 演练" if not a.apply else "· 已执行", len(src),
                        human(sum(src.values())), sent, human(sent_bytes), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
