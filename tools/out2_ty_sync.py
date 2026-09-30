# -*- coding: utf-8 -*-
"""源远端:out2 -> 目标网盘/out2 单向同步 + 两处齐全后归档到 源远端:out3

规则:
  1) 单向同步: 把 源远端:out2 里的文件传到 目标网盘/out2 (同名同大小则跳过, 源文件不动)
     判定"已有"时会同时看 `OT_TY_CHECK` 里的**所有目录**(缺省=OT_TY): 只要任一目录有同名同大小
     就算已归档 —— 因为目标盘空间不够, 会把已经落地的文件挪到镜像盘, 只看一个目录会导致重传。
  2) 完成判定: 同一个文件在 **目标网盘(任一判重目录)/** 和 **/归档挂载** 都齐了, 就把 源远端:out2 里的它
     `moveto` 到 **源远端:out3** (同盘服务端移动, 秒完成) —— 等于"这条文件走完了全流程"
  3) dbjm 里是 gd-out2 切过的分片, 所以判定要分两种:
       <= 阈值(默认300MB): dbjm 里有同名同大小
       >  阈值          : dbjm 里有 <名字去扩展>.part001.<ext> 和 .part002.<ext> 两片
     (若 dbjm 里直接有同名同大小, 也算齐 —— 兼容老数据)

跑法: runner 上现装 OpenList 挂「目标网盘」, 上传走 /api/fs/put; 源远端 走 rclone。
默认演练(--dry); --apply 才真动。报告 中转远端:报告区/out2_ty_report.txt
"""
import argparse
import glob
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

import pathcfg          # 路径真值来自配置: CI=Secret PATHS_JSON, 本机=paths.local.json
try:
    import ty189        # 自研 目标网盘 直连上传: 带 socket 超时 + 断点续传
except Exception:                                 # pragma: no cover
    ty189 = None
SRC = pathcfg.require("OT_SRC")
TY_DIR = pathcfg.require("OT_TY")                 # 上传目标 —— **必须单值**(要拼路径)
MOUNT = "/" + TY_DIR.strip("/").split("/")[0]     # 目的网盘在 alist 里的挂载名(从配置推)
# 判"云盘已有"时要一起看的目录(逗号多值; 缺省 = TY_DIR)。
# 为什么需要: 个人云空间不够, 会把 个人/out2 的文件不断挪到 家庭/out2; 只看 TY_DIR 的话,
# 被挪走的文件下一轮就被判"没有" -> 从源重传(又把个人云塞满)。TY_DIR 仍保持单值。
TY_CHECK_DIRS = [x.strip() for x in (pathcfg.get("OT_TY_CHECK") or TY_DIR).split(",") if x.strip()]
DBJM_DIR = pathcfg.require("OT_DBJM")
ARCH = pathcfg.require("OT_ARCH")
ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
WORK = os.environ.get("OT_WORK", "/tmp/out2ty")
REPORT = pathcfg.require("OT_REPORT")
DAILY = pathcfg.require("OT_DAILY")      # 云盘日上传配额账本(防超 200GB/日)
GB = 1024 ** 3
RCLONE = os.environ.get("RCLONE_BIN") or shutil.which("rclone") or "rclone"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def human(b):
    return "%.1f MB" % (b / 1048576) if b < GB else "%.2f GB" % (b / GB)


def alist_token():
    t = os.environ.get("ALIST_TOKEN", "").strip()
    if not t:
        raise SystemExit("!! 需要 ALIST_TOKEN")
    return t


def alist(method, path, payload, timeout=180):
    req = urllib.request.Request(ALIST + path, data=json.dumps(payload).encode(), method=method,
                                 headers={"Authorization": alist_token(),
                                          "Content-Type": "application/json"})
    d = json.loads(OP.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    if d.get("code") != 200:
        raise RuntimeError("alist %s -> %s %s" % (path, d.get("code"), d.get("message")))
    return d.get("data")


def alist_list(path, tries=3):
    """列目录。跨境链路抖动时 alist 会回 500 (`failed get dir: object not found`) ——
    那不是目录真没了, 而是上游 189 listFiles 超时后的兜底报错, 所以这里先重试几次。
    重试仍失败就抛给上层: 上层会判定"本轮跳过", 不会把整轮打成 failure。"""
    last = None
    for i in range(tries):
        try:
            d = alist("POST", "/api/fs/list", {"path": path, "page": 1, "per_page": 0, "refresh": True})
            out = {}
            for x in (d or {}).get("content") or []:
                out[x["name"]] = (int(x.get("size") or 0), bool(x.get("is_dir")))
            return out
        except Exception as e:
            last = e
            log("!! 列目录失败(第 %d/%d 次): %s" % (i + 1, tries, str(e)[:160]))
            if i + 1 < tries:
                time.sleep(15 * (i + 1))
    raise last


def alist_get(path):
    try:
        d = alist("POST", "/api/fs/get", {"path": path})
    except Exception:
        return None
    return int((d or {}).get("size") or 0)


def rclone(args, timeout=1500):
    env = dict(os.environ)
    return subprocess.run([RCLONE] + args + ["--config", CONF], capture_output=True,
                          text=True, errors="replace", timeout=timeout, env=env)


def _find_conf():
    for c in (os.environ.get("RCLONE_CONF_PATH", ""),
              os.path.expanduser("~/.config/rclone/rclone.conf"),
              os.path.join(os.environ.get("APPDATA", ""), "rclone", "rclone.conf")):
        if c and os.path.exists(c):
            return c
    return os.path.expanduser("~/.config/rclone/rclone.conf")


CONF = _find_conf()


def lsjson(remote, timeout=1800):
    r = rclone(["lsjson", remote, "--files-only"], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("列 %s 失败: %s" % (remote, (r.stderr or "")[:160]))
    return {x["Name"]: int(x.get("Size") or 0) for x in json.loads(r.stdout or "[]")}


STALL_SECS = 300          # 连续多久没有任何字节推进 -> 判定停滞, 主动断开(原来只能等 socket 超时 3600s)
PUT_MAX_SECS = 1800       # 单个文件上传总时长上限(慢但有进展的也不许无限拖)


class Progress(object):
    def __init__(self, fh, total, label, every=10):
        self.fh, self.total, self.label = fh, total, label
        self.n, self.t0, self.last, self.every = 0, time.time(), time.time(), every
        self.last_byte = time.time()

    def read(self, n=-1):
        b = self.fh.read(n)
        now = time.time()
        if b:
            self.n += len(b)
            self.last_byte = now
            if now - self.last >= self.every:
                self.last = now
                sys.stdout.write("\r    ⬆ %s %5.1f%%  %s/%s  %.1f MB/s   "
                                 % (self.label, 100.0 * self.n / max(self.total, 1), human(self.n),
                                    human(self.total), self.n / max(now - self.t0, 1e-6) / 1048576))
                sys.stdout.flush()
        # 停滞/超时硬闸(raise 会让 http 层直接中断本次 PUT, 不用等 socket 超时)
        if self.n < self.total and now - self.last_byte > STALL_SECS:
            raise RuntimeError("上传停滞 %.0f 分钟无进展(已传 %s / %s)"
                               % ((now - self.last_byte) / 60, human(self.n), human(self.total)))
        if now - self.t0 > PUT_MAX_SECS:
            raise RuntimeError("上传超过 %.0f 分钟上限(已传 %s / %s)"
                               % (PUT_MAX_SECS / 60.0, human(self.n), human(self.total)))
        return b

    @property
    def secs(self):
        return time.time() - self.t0


def alist_put(dst_path, local_file, size, label):
    with open(local_file, "rb") as fh:
        body = Progress(fh, size, label)
        req = urllib.request.Request(ALIST + "/api/fs/put", data=body, method="PUT",
                                     headers={"Authorization": alist_token(),
                                              "File-Path": urllib.parse.quote(dst_path),
                                              "Content-Type": "application/octet-stream",
                                              "Content-Length": str(size)})
        # socket 超时压到 STALL_SECS: 服务器收包卡住时 5 分钟就断开, 不再干等 1 小时
        d = json.loads(OP.open(req, timeout=STALL_SECS + 60).read().decode("utf-8", "replace"))
    sys.stdout.write("\n")
    if d.get("code") != 200:
        raise RuntimeError("上传失败: %s %s" % (d.get("code"), d.get("message")))
    return body.secs


def alist_remove(path, names):
    try:
        alist("POST", "/api/fs/remove", {"dir": path, "names": names})
        return True
    except Exception:
        return False


def probe_upload(dst_dir, mb=8, timeout=180):
    """上传通道探针: 往目标目录 PUT 一个小文件, 用后即删。

    卡死发生在"首个包之后就零进展"(≈0.4MB), 所以几 MB 的探针就能检出通道是否可用。
    """
    n = mb * 1024 * 1024
    p = os.path.join(WORK, "_probe.bin")
    name = "__probe_upload.bin"
    d, err = None, ""
    try:
        with open(p, "wb") as fh:
            fh.write(os.urandom(n))
        with open(p, "rb") as fh:
            body = Progress(fh, n, "probe")
            req = urllib.request.Request(
                ALIST + "/api/fs/put", data=body, method="PUT",
                headers={"Authorization": alist_token(),
                         "File-Path": urllib.parse.quote(dst_dir.rstrip("/") + "/" + name),
                         "Content-Type": "application/octet-stream",
                         "Content-Length": str(n)})
            d = json.loads(OP.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    except Exception as e:
        err = str(e)[:120]
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
        alist_remove(dst_dir, [name])     # 不管成功失败都清掉探针文件
    ok = bool(d and d.get("code") == 200)
    return ok, (err or (str(d)[:120] if d else ""))


def ty189_open():
    """用 ALIST_STORAGES_TY(Secret) 或本机 alist_storages_ty.json 建 189 直连客户端。

    目的网盘的 access_token/family_id 就在这份存储配置里, 不需要额外 Secret。
    返回 (client, folder_id) 或 (None, None)。
    """
    if ty189 is None:
        return None, None
    raw = os.environ.get("ALIST_STORAGES_TY", "").strip()
    if not raw:
        lp = os.path.join(HERE, "alist_storages_ty.json")
        if os.path.exists(lp):
            raw = io.open(lp, encoding="utf-8").read()
    if not raw:
        print("   (没拿到 ALIST_STORAGES_TY, 直连不可用)")
        return None, None
    try:
        cfg = json.loads(raw)
        sts = cfg if isinstance(cfg, list) else (cfg.get("storages") or [])
        per = None
        for s in sts:
            if (s.get("mount_path") or "").rstrip("/") == MOUNT.rstrip("/"):
                per = s
                break
        if per is None:
            for s in sts:
                if "CloudTV" in (s.get("driver") or "") and "个人" in (s.get("mount_path") or ""):
                    per = s
                    break
        if not per:
            print("   (存储配置里没有匹配 %s 的挂载, 直连不可用)" % MOUNT)
            return None, None
        add = per.get("addition") or "{}"
        if isinstance(add, str):
            add = json.loads(add)
        cli = ty189.Ty189(add.get("access_token") or "", add.get("family_id") or "", False)
        d = cli.login()
        fid = cli.resolve_dir(TY_DIR, mount=MOUNT)
        log("直连就绪: %s -> folderId=%s" % (d.get("loginName"), fid))
        return cli, fid
    except Exception as e:
        print("   !! 直连初始化失败: %s" % str(e)[:160])
        return None, None


def probe_ty189(cli, fid, dst_dir, mb=2):
    """用 189 直连做一个 2MB 上传探针(用后即删), 验证这条路是否通"""
    p = os.path.join(WORK, "_probe189.bin")
    name = "__probe_upload_ty.bin"
    try:
        with open(p, "wb") as fh:
            fh.write(os.urandom(mb * 1024 * 1024))
        ok, msg = cli.upload(p, fid, name=name)
    except Exception as e:
        ok, msg = False, str(e)[:120]
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
        alist_remove(dst_dir, [name])
    return ok, msg


def upload_and_verify(local_file, name, size, dst, ctx):
    """上传 + 校验。优先走 189 直连(带超时+断点续传), 失败再退回 OpenList PUT。

    返回 (ok, 说明) —— 说明直接进报告。
    """
    if ctx and ctx[0] is not None:
        cli, fid = ctx
        t0 = time.time()
        ok, msg = cli.upload(local_file, fid, name=name,
                             log=lambda m: print(m, flush=True))
        if ok:
            # 用 189 自己的列表校验(OpenList 侧可能有缓存, 不能用来判刚传完的文件)
            got = None
            try:
                for it in cli.list_files(fid):
                    if (not it["is_dir"]) and it["name"] == name:
                        got = it["size"]
                        break
            except Exception as e:
                print("   189 校验列目录失败: %s" % str(e)[:90])
            if got == size:
                return True, "上传云盘 OK (189直连 %.1fMB/s)" % (size / 1048576 / max(time.time() - t0, 1e-6))
            if got is None:
                return True, "上传云盘 OK (189直连 %s; 列表未刷新)" % msg
            return False, "189直连后校验不过: 目标 %s 期望 %s" % (got, size)
        print("   !! 189直连失败(%s) -> 退回 OpenList PUT" % msg[:110])

    t0 = time.time()
    secs = alist_put(dst, local_file, size, os.path.basename(name)[:26])
    got = alist_get(dst)
    if got != size:
        return False, "上传后校验不过: 目标 %s 期望 %s" % (got, size)
    return True, "上传云盘 OK (%.1f MB/s)" % (size / max(secs, 1e-6) / 1048576)


def dbjm_done(name, size, dbjm, thresh):
    """dbjm 里算不算齐了。

    gd-out2 会把 >阈值 的文件切成 N 片(每片 <=280MB, 段数不固定), 所以不能再只看
    part001/part002 两片。判据两条:
      ① 未切分: dbjm 里有同名同大小的文件;
      ② 切过  : dbjm 里从 part001 起有一组片, 且这些片的大小之和 ≈ 原件大小(±5%)
                —— 缺任意一片总和对不上, 所以能可靠挡住"缺片就当齐了去归档"。
    """
    hit = dbjm.get(name)
    if hit and not hit[1] and hit[0] == size:
        return True
    if size > thresh:
        stem, ext = os.path.splitext(name)
        pre = stem + ".part"
        segs = {k: v[0] for k, v in dbjm.items()
                if k.startswith(pre) and k.endswith(ext) and not v[1]}
        if not segs or (stem + ".part001" + ext) not in segs:
            return False
        return abs(sum(segs.values()) - size) <= max(size * 0.05, 1048576)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-ops", type=int, default=60, help="本轮最多处理几个文件")
    ap.add_argument("--max-total-gb", type=float, default=6.0, help="本轮上传总量上限(GB)")
    ap.add_argument("--thresh-mb", type=int, default=300, help="gd-out2 的切分阈值(判定分片用)")
    ap.add_argument("--budget-min", type=int, default=170)
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-local", action="store_true")
    ap.add_argument("--daily-limit-gb", type=float, default=180.0,
                    help="云盘当日上传总量上限(GB); 按北京自然日累计, 默认 180(留 20GB 余量)")
    a = ap.parse_args()
    thresh = a.thresh_mb * 1024 ** 2
    max_total = int(a.max_total_gb * GB)

    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)

    lines = ["# 源远端:out2 -> 目标网盘/out2 同步 + 归档 源远端:out3 报告  %s  [%s]"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), "执行" if a.apply else "演练")]

    # --- 云盘日配额保护: 该网盘按北京自然日重置, 这里累计封顶, 免得撞日配额 ---
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    d_date, d_gb = load_daily()
    if d_date != today:
        d_date, d_gb = today, 0.0
    if d_gb >= a.daily_limit_gb:
        lines.append("今日已传 %.1fGB >= 日上限 %.1fGB -> 本轮不传, 等次日 0 点重置"
                     % (d_gb, a.daily_limit_gb))
        print(lines[-1])
        finish(lines, a)
        return 0
    eff_gb = min(a.max_total_gb, a.daily_limit_gb - d_gb)
    max_total = int(eff_gb * GB)
    lines.append("日配额: 今日已用 %.1fGB / 上限 %.1fGB -> 本轮最多再传 %.1fGB"
                 % (d_gb, a.daily_limit_gb, eff_gb))

    # 刷 源远端 token
    try:
        import gd_token
        log("刷 源远端 token: %s" % ("OK" if gd_token.main() == 0 else "失败"))
    except Exception as e:
        log("!! gd_token 异常: %s" % str(e)[:120])

    # 列目录(源 / 判重目录们 / 归档区)。跨境链路抖动是常态(189 listFiles 超时 -> alist 回 500),
    # 列不动就本轮优雅跳过: 不处理任何文件、不动任何源文件, 正常退出, 交给下一轮自动重试。
    # "云盘已有" 判定 = TY_CHECK_DIRS 里**任一目录**有同名同大小(通常是 目标盘/out2 + 镜像盘/out2,
    # 两个都算已归档; 前面的目录优先)。这样把文件从目标盘挪到镜像盘后不会被重传。
    try:
        src = lsjson(SRC)
        ty, ty_parts = {}, []
        for d in TY_CHECK_DIRS:
            part = alist_list(d)
            ty_parts.append("%s %d" % (d, len(part)))
            for n, v in part.items():
                ty.setdefault(n, v)
        db = alist_list(DBJM_DIR)
    except Exception as e:
        lines.append("[跳过] 列目录失败(链路抖动?): %s" % str(e)[:200])
        lines.append("本轮不处理任何文件、不动任何源文件, 等下一轮自动重试。")
        print(lines[-2], flush=True)
        print(lines[-1], flush=True)
        finish(lines, a)
        return 0
    log("源 %s: %d 个文件 | 判重目录 %s -> 合并 %d | %s: %d"
        % (SRC, len(src), "; ".join(ty_parts), len(ty), DBJM_DIR, len(db)))
    lines.append("源 %s %d 个 | 判重目录 %s = %d 个 | %s %d 个"
                 % (SRC, len(src), "; ".join(ty_parts), len(ty), DBJM_DIR, len(db)))

    if a.apply:
        r = rclone(["mkdir", ARCH])
        log("确保 %s 存在: %s" % (ARCH, "OK" if r.returncode == 0 else (r.stderr or "")[:80]))

    todo = sorted(src.items(), key=lambda kv: kv[1])
    if a.only:
        todo = [(n, s) for n, s in todo if n == a.only]
        if not todo:
            lines.append("源里没有 %s" % a.only)
            finish(lines, a)
            return 1

    up_n = arch_n = skip_n = fail_n = 0
    total_up = 0
    put_streak = 0          # 连续"零进展"的 PUT 次数(成功即清零); 用于熔断
    PUT_BREAK = 9           # 连续这么多次零进展 -> 判定通道不可用, 本轮提前收工
    lines.append("---")

    # 189 直连(带超时+断点续传) —— 这是上传主路径; OpenList PUT 只作退路
    ctx = (None, None)
    if a.apply:
        ctx = ty189_open()

    # 0) 上传通道探针: 通道不通就整轮跳过, 别拿 3 小时预算去撞墙
    if a.apply and todo:
        if ctx[0] is not None:
            ok, info = probe_ty189(ctx[0], ctx[1], TY_DIR, mb=32)
            tag = "189直连探针"
        else:
            ok, info = probe_upload(TY_DIR, mb=8, timeout=180)
            tag = "OpenList探针"
        log("上传通道%s: %s" % (tag, "可用" if ok else "不通 -> %s" % info))
        lines.append("上传通道%s: %s" % (tag, "可用" if ok else "不通(跳过本轮)"))
        if not ok:
            lines.append("目标网盘上传通道当前不可用(探针失败), 本轮不做任何上传, 等下一轮")
            finish(lines, a)
            return 1

    def checkpoint():
        """增量落盘(报告 + 日账本): 运行被取消/超时/强杀也不丢已完成的账"""
        if not a.apply:
            return
        snap = list(lines)
        snap.append("---")
        snap.append("(进行中) 上传 %d 个 (%s) | 归档 %d | 已有 %d | 失败 %d | 已用 %.1f 分钟"
                    % (up_n, human(total_up), arch_n, skip_n, fail_n, (time.time() - t0) / 60))
        finish(snap, a, quiet=True)
        if total_up > 0:
            save_daily(today, d_gb + total_up / GB)

    for name, size in todo:
        if put_streak >= PUT_BREAK:
            lines.append("连续 %d 次上传零进展 -> 目标网盘上传通道疑似异常, 本轮提前收工"
                         % put_streak)
            print(lines[-1])
            break
        if up_n + arch_n + fail_n >= a.max_ops:
            lines.append("达本轮个数上限 %d, 收工" % a.max_ops)
            break
        if total_up >= max_total:
            lines.append("达本轮总量上限 %.1f GB, 收工" % a.max_total_gb)
            break
        if time.time() - t0 > a.budget_min * 60:
            lines.append("到时间预算, 收工")
            break
        head = "[%s] %s" % (human(size), name)

        # --- 1) 云盘侧 ---
        ty_hit = ty.get(name)
        ty_ok = bool(ty_hit and not ty_hit[1] and ty_hit[0] == size)
        note = ""
        if ty_ok:
            skip_n += 1
            note = "云盘已有(同名同大小)"
        elif not a.apply:
            note = "(演练) 将上传到 %s" % TY_DIR
            ty_ok = True                      # 演练: 假装成功, 好把后面归档逻辑也演出来
        else:
            lp = os.path.join(WORK, name)
            try:
                ft0 = time.time()
                r = rclone(["copyto", "%s/%s" % (SRC, name), lp, "--retries", "3",
                            "--low-level-retries", "10", "--transfers", "1",
                            "--multi-thread-streams", "1", "--timeout", "5m",
                            "--contimeout", "1m", "--stats", "15s",
                            "--stats-one-line"], timeout=1500)
                if r.returncode != 0 or os.path.getsize(lp) != size:
                    raise RuntimeError("下载失败/大小不符 :: %s" % (r.stderr or "")[:120])
                # 上传: 主路径 = 189 直连(自带 socket 超时 + 断点续传, 断了接着传);
                #   退路 = OpenList PUT。整条链路会间歇性抽风 -> 同一文件再补几次即可。
                dst = TY_DIR.rstrip("/") + "/" + name
                secs, last_err, note_up = 0.0, "", ""
                for att in range(1, 3):
                    try:
                        ok2, note_up = upload_and_verify(lp, name, size, dst, ctx)
                        if not ok2:
                            raise RuntimeError(note_up)
                        put_streak = 0
                        last_err = ""
                        break
                    except Exception as e:
                        last_err = str(e)[:120]
                        put_streak += 1
                        over = (time.time() - t0) / 60 > a.budget_min
                        if att >= 2 or over or put_streak >= PUT_BREAK:
                            raise RuntimeError("重试 %d 次仍失败(连续零进展 %d 次): %s"
                                               % (att, put_streak, last_err))
                        print("   ↻ 第 %d 次失败(%s) -> %d 秒后重试"
                              % (att, last_err, 20 * att))
                        time.sleep(20 * att)
                if last_err:
                    raise RuntimeError(last_err)
                ty_ok = True
                up_n += 1
                total_up += size
                ty[name] = (size, False)
                note = note_up or ("上传云盘 OK (%.1f MB/s)" % (size / max(secs, 1e-6) / 1048576))
            except Exception as e:
                fail_n += 1
                note = "上传失败(%.1fmin): %s" % ((time.time() - ft0) / 60, str(e)[:120])
                lines.append("%s | ❌ %s" % (head, note))
                print("%s ❌ %s" % (head, note))
            finally:
                if not a.keep_local and os.path.exists(lp):
                    try:
                        os.remove(lp)
                    except OSError:
                        pass

        # --- 2) 完成判定 -> 归档 out3 ---
        arch_note = ""
        if ty_ok:
            db_ok = dbjm_done(name, size, db, thresh)
            if db_ok:
                if a.apply:
                    r = rclone(["moveto", "%s/%s" % (SRC, name), "%s/%s" % (ARCH, name),
                                "--retries", "3", "--low-level-retries", "10"])
                    if r.returncode == 0:
                        arch_n += 1
                        arch_note = "| 云盘+dbjm 都有 -> 已归档到 %s" % ARCH
                    else:
                        arch_note = "| !! 归档失败: %s" % (r.stderr or "")[:90]
                else:
                    arch_note = "| (演练) 云盘+dbjm 都有 -> 将归档到 %s" % ARCH
            else:
                arch_note = "| dbjm 还没有(等 od2-dbjm)"
        print("%s %s %s" % (head, note, arch_note))
        lines.append("%s | %s %s" % (head, note, arch_note))
        checkpoint()

    lines.append("---")
    lines.append("本轮: 上传云盘 %d 个 (%s) | 归档 out3 %d 个 | 云盘已有 %d 个 | 失败 %d"
                 % (up_n, human(total_up), arch_n, skip_n, fail_n))
    lines.append("源目录剩余 %d 个" % max(len(src) - arch_n, 0))
    print("\n合计: 上传 %d (%s) | 归档 %d | 已在 %d | 失败 %d"
          % (up_n, human(total_up), arch_n, skip_n, fail_n))
    if a.apply and total_up > 0:
        save_daily(today, d_gb + total_up / GB)
    finish(lines, a)
    return 0


def load_daily():
    """读今日已上传量; 返回 (日期, GB)"""
    try:
        r = rclone(["cat", DAILY], timeout=90)
        if r.returncode != 0:
            return "", 0.0
        d = json.loads((r.stdout or "").strip() or "{}")
        return d.get("date", ""), float(d.get("gb", 0) or 0)
    except Exception:
        return "", 0.0


def save_daily(date, gb):
    """写回今日累计上传量(供下一轮继续累计)"""
    try:
        payload = json.dumps({"date": date, "gb": round(gb, 2),
                              "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        r = subprocess.run([RCLONE, "rcat", DAILY, "--config", CONF],
                           input=payload, capture_output=True, text=True,
                           errors="replace", timeout=120)
        log("日配额账本 %.1fGB -> %s: %s"
            % (gb, DAILY, "OK" if r.returncode == 0 else (r.stderr or "")[:80]))
    except Exception as e:
        log("!! 日配额账本写入失败: %s" % str(e)[:80])


def finish(lines, a, quiet=False):
    text = "\n".join(lines) + "\n"
    rp = os.path.join(WORK, "out2_ty_report.txt")
    try:
        with io.open(rp, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log("!! 报告落盘失败: %s" % str(e)[:80])
    r = rclone(["copyto", rp, REPORT, "--retries", "2"], timeout=600)
    log("报告 -> %s: %s" % (REPORT, "OK" if r.returncode == 0 else (r.stderr or "")[:80]))


if __name__ == "__main__":
    import logmask          # 日志脱敏: 文件名/路径 -> 短哈希(见 logmask.py)
    logmask.install()
    sys.exit(main())
