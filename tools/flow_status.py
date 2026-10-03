# -*- coding: utf-8 -*-
"""云流程状态总览 -> Telegram

扫描各仓库的 Actions 运行情况, 判定每个流程的状态(正常 / 超期 / 进行中 / 失败),
失败的自动去抓那次的日志、提取错误行, 画成一张状态图(SVG -> PNG)发到 Telegram。

用法:
  python3 flow_status.py               # 扫描 + 出图 + 发 TG
  python3 flow_status.py --no-send     # 只扫描, 把 svg/png 落在本地看
  python3 flow_status.py --text-only   # 不发图, 只发文字摘要

环境变量: TG_TOKEN / TG_CHAT_ID / GH_TOKEN / GH_TOKEN_TAM(可选, 私有仓库用)

安全: 只回应/发送给 TG_CHAT_ID; token 不进日志。
"""
import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import pathcfg          # 文案/路径真值: CI=Secret PATHS_JSON, 本机=paths.local.json

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
TG = "https://api.telegram.org"
BJ = timezone(timedelta(hours=8))
GH_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # GitHub 直连(不走代理)

# ---- 监控清单: (仓库, workflow 文件, 显示名, 周期小时(0=手动), token 键) ----
# ⚠️ 这里**只放中性信息**(仓库/文件名/显示名/周期)。带盘名的**功能说明、分组标题、失败建议**
# 一律不进源码 —— 它们放在 PATHS_JSON(CI) / paths.local.json(本机) 的
# FS_DESC / FS_GROUPS / FS_ADVICE 三个键里(JSON 字符串), 见 _meta()。
WATCH = [
    ("tammader/cloudtransfer-gd-out-pub", "in-pipeline.yml", "in-pipeline", 3, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "gen-manifest.yml", "gen-manifest", 3, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "in2-ydout.yml", "in2-ydout", 6, "A"),
    ("tammader/ty-transfer-pub", "ty-transfer.yml", "ty-transfer", 12, "A"),
    ("tammader/ty-transfer-pub", "ydy2-transfer.yml", "ydy2-transfer", 12, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "ydout-in3.yml", "ydout-in3", 0, "A"),
    ("tamd258/cloudtransfer-speedtest", "out-sync.yml", "out-sync", 12, "B"),
    ("tammader/cloudtransfer-gd-out-pub", "gd-out2.yml", "gd-out2", 3, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "od2-dbjm.yml", "od2-dbjm", 6, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "out2-ty-sync.yml", "out2-ty-sync", 3, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "od2-tg-gd2.yml", "od2-tg-gd2", 6, "A"),
    ("tammader/cloudtransfer-gd-out-pub", "gd-backup.yml", "gd-backup", 12, "A"),
]

NAMES = [e[2] for e in WATCH]


def _meta(key, default):
    """从配置里取一段 JSON 文案; 取不到/格式不对就用中性兜底(源码里不留盘名)"""
    raw = pathcfg.get("FS_" + key, "")
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, type(default)):
                return v
        except Exception:
            pass
    return default


# 图上分四列(标题真值在配置; 兜底标题保持中性 —— 流程名本身是仓库里的文件名, 遮不掉)
GROUPS = _meta("GROUPS", [
    ("① 上游 / 清单", ["in-pipeline", "gen-manifest"]),
    ("② 分卷 / 中继", ["in2-ydout", "ty-transfer", "ydy2-transfer"]),
    ("③ 出口 / 搬运", ["ydout-in3", "out-sync", "gd-out2", "od2-tg-gd2"]),
    ("④ 落地 / 备份", ["od2-dbjm", "out2-ty-sync", "gd-backup"]),
])

# 每个流程的功能说明(卡片里那一行)
DESC = _meta("DESC", {})
DESC_FALLBACK = "（未配置说明）"


def desc_of(name):
    return DESC.get(name) or DESC_FALLBACK


# 失败时的「影响 / 处置」建议
ADVICE = _meta("ADVICE", {})
ADVICE_FALLBACK = ("该环节停摆，下游会缺料", "点开该 run 看完整日志")

COL_X = [16, 186, 356, 526]
ROW_Y = [108, 172, 236, 300]
NODE_W, NODE_H = 150, 56
STATE_STYLE = {
    "ok":      ("#dcfce7", "#16a34a", "#14532d", "#15803d"),
    "late":    ("#fef9c3", "#ca8a04", "#713f12", "#a16207"),
    "running": ("#fef9c3", "#ca8a04", "#713f12", "#a16207"),
    "fail":    ("#fee2e2", "#dc2626", "#7f1d1d", "#b91c1c"),
    "unknown": ("#f1f5f9", "#94a3b8", "#334155", "#64748b"),
}


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def gh(path, tok):
    """GitHub API (不走代理)"""
    r = urllib.request.Request(API + path, headers={
        "Authorization": "token " + tok, "User-Agent": "wb-flow-status",
        "Accept": "application/vnd.github+json"})
    try:
        with GH_OP.open(r, timeout=90) as x:
            t = x.read().decode("utf-8", "replace")
            return x.status, (json.loads(t) if t.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return 0, {"_err": str(e)[:200]}


def tg_call(method, payload, files=None):
    """Telegram API (走系统代理 —— 本机需要, runner 上无代理也能直连)"""
    tok = os.environ.get("TG_TOKEN", "")
    if not tok:
        return {"ok": False, "err": "缺 TG_TOKEN"}
    url = "%s/bot%s/%s" % (TG, tok, method)
    if files:
        boundary = "----wbflow%d" % int(time.time())
        body = b""
        for k, v in (payload or {}).items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, k, v)).encode("utf-8")
        for k, (fn, data) in files.items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                     "Content-Type: image/png\r\n\r\n" % (boundary, k, fn)).encode("utf-8")
            body += data + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(payload or {}).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=120) as x:
            return json.loads(x.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:160]}


def bjstr(iso):
    try:
        return (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                .astimezone(BJ)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_error_lines(repo, run_id, tok, max_lines=3):
    """抓失败 run 的日志, 提取关键错误行(抓不到就返回空 —— 不因为这个把整个扫描搞挂)"""
    url = API + "/repos/%s/actions/runs/%s/logs" % (repo, run_id)
    req = urllib.request.Request(url, headers={"Authorization": "token " + tok,
                                               "User-Agent": "wb-flow-status"})
    loc = None
    try:                                   # 先只取 302 的 Location(不跟随)
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as x:
            loc = x.headers.get("Location")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location")
        else:
            return []
    except Exception:
        return []
    if not loc:
        return []
    try:                                   # blob 下载**不能带认证头**, 否则 403
        with urllib.request.urlopen(loc, timeout=300) as x:
            blob = x.read()
    except Exception:
        return []
    pat = re.compile(r"(ERROR|CRITICAL|panic:|Traceback|##\[error\]|not found|"
                     r"No such file|denied|invalid|failed to|Timed out)", re.I)
    hits = []
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        name = max(z.namelist(), key=lambda n: z.getinfo(n).file_size)   # 最大的通常是失败 job
        text = z.read(name).decode("utf-8", "replace")
        for ln in text.splitlines():
            s = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", ln).strip()   # 去 GitHub 的时间戳
            if len(s) < 8:
                continue
            if pat.search(s):
                hits.append(s[:150])
    except Exception:
        return []
    return hits[-max_lines:] if hits else []


def scan(entry, tokens):
    repo, wf, name, period, tk = entry
    base = {"name": name, "desc": desc_of(name), "repo": repo, "wf": wf, "period": period,
            "state": "unknown", "note": "", "time": "", "run_id": None,
            "errors": [], "age_h": None}
    tok = tokens.get(tk) or ""
    if not tok:
        base["note"] = "未配置 token"
        return base
    st, d = gh("/repos/%s/actions/workflows/%s/runs?per_page=5" % (repo, wf), tok)
    if st == 404:
        base["note"] = "workflow 不存在/已删"
        return base
    if st != 200:
        base["note"] = "API %s" % st
        return base
    runs = d.get("workflow_runs") or []
    if not runs:
        base["note"] = "还没跑过"
        return base
    r = runs[0]
    base["run_id"] = r["id"]
    base["time"] = bjstr(r["created_at"])
    try:
        created = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        base["age_h"] = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
    except Exception:
        pass
    if r["status"] != "completed":
        base["state"] = "running"
        if base["age_h"] is not None:
            base["note"] = "已跑 %.0f 分钟" % (base["age_h"] * 60)
        return base
    if r.get("conclusion") == "success":
        base["state"] = "ok"
        if period and base["age_h"] is not None and base["age_h"] > period * 2.5:
            base["state"] = "late"
        # 最近有没有连续失败(只在最后 3 次里看)
        return base
    # 失败/取消等
    if r.get("conclusion") == "failure":
        base["state"] = "fail"
        base["errors"] = fetch_error_lines(repo, r["id"], tok)
    else:
        base["state"] = "unknown"
        base["note"] = r.get("conclusion") or "?"
    return base


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_svg(items, ts):
    by = {it["name"]: it for it in items}
    L = []
    L.append('<svg viewBox="0 0 680 %d" xmlns="http://www.w3.org/2000/svg" '
             'font-family="Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Zen Hei, sans-serif">'
             % 556)
    L.append('<rect x="0" y="0" width="680" height="556" fill="#ffffff"/>')
    L.append('<text x="16" y="26" font-size="15" font-weight="700" fill="#1f2937">'
             '云流程状态总览 · 每 6 小时刷新</text>')
    n_fail = sum(1 for i in items if i["state"] == "fail")
    n_late = sum(1 for i in items if i["state"] in ("late", "running"))
    n_ok = sum(1 for i in items if i["state"] == "ok")
    L.append('<text x="16" y="45" font-size="10.5" fill="#64748b">共 %d 个流程：'
             '%d 正常 · %d 超期/进行中 · %d 失败　　扫描 %s（北京时间）</text>'
             % (len(items), n_ok, n_late, n_fail, esc(ts)))
    for i, (c, txt) in enumerate([("#16a34a", "正常"), ("#ca8a04", "超期 / 进行中"),
                                  ("#dc2626", "失败"), ("#94a3b8", "未知/未监控")]):
        x = 24 + i * 106
        L.append('<circle cx="%d" cy="66" r="5" fill="%s"/>'
                 '<text x="%d" y="70" font-size="10" fill="#475569">%s</text>'
                 % (x, c, x + 10, txt))
    for ci, (title, names) in enumerate(GROUPS):
        x = COL_X[ci]
        L.append('<text x="%d" y="96" font-size="11" fill="#334155" text-anchor="middle">%s</text>'
                 % (x + NODE_W // 2, esc(title)))
        for ri, nm in enumerate(names):
            it = by.get(nm)
            if not it:
                continue
            y = ROW_Y[ri]
            fill, stroke, tcol, scol = STATE_STYLE.get(it["state"], STATE_STYLE["unknown"])
            L.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s" stroke="%s"/>'
                     % (x, y, NODE_W, NODE_H, fill, stroke))
            cx = x + NODE_W // 2
            L.append('<text x="%d" y="%d" font-size="11.5" font-weight="700" fill="%s" '
                     'text-anchor="middle">%s</text>' % (cx, y + 19, tcol, esc(nm)))
            L.append('<text x="%d" y="%d" font-size="8.5" fill="#64748b" text-anchor="middle">%s</text>'
                     % (cx, y + 33, esc(it["desc"])))
            st_txt = {"ok": "正常 · %s", "late": "超期 · 上次 %s", "running": "进行中 · %s",
                      "fail": "失败 · %s", "unknown": "%s"}.get(it["state"], "%s")
            arg = it["time"] or (it["note"] or "—")
            if it["state"] == "running" and it["note"]:
                arg = it["note"]
            elif it["state"] == "unknown":
                arg = it["note"] or "未监控"
            L.append('<text x="%d" y="%d" font-size="9.5" fill="%s" text-anchor="middle">%s</text>'
                     % (cx, y + 47, scol, esc(st_txt % arg)))
    # 失败详情卡
    fails = [i for i in items if i["state"] == "fail"]
    if fails:
        f = fails[0]
        imp, fix = ADVICE.get(f["name"], ADVICE_FALLBACK)
        L.append('<rect x="16" y="376" width="648" height="156" rx="10" fill="#fef2f2" '
                 'stroke="#fca5a5" stroke-width="1.5"/>')
        L.append('<text x="32" y="402" font-size="12.5" font-weight="700" fill="#991b1b">'
                 '【失败】%s · %s · run %s%s</text>'
                 % (esc(f["name"]), esc(f["time"]), f["run_id"],
                    "" if len(fails) == 1 else "（另有 %d 个失败）" % (len(fails) - 1)))
        L.append('<line x1="32" y1="412" x2="648" y2="412" stroke="#fecaca"/>')
        y = 430
        L.append('<text x="32" y="%d" font-size="10.5" fill="#7f1d1d">'
                 '原因（自动抓日志里的错误行）：</text>' % y)
        y += 18
        errs = f["errors"] or ["（没抓到典型错误行，请点开 run 看完整日志）"]
        for e in errs[:3]:
            L.append('<text x="32" y="%d" font-size="9.5" fill="#b91c1c" '
                     'font-family="monospace">%s</text>' % (y, esc(e[:96])))
            y += 17
        y += 4
        L.append('<text x="32" y="%d" font-size="10.5" fill="#7f1d1d">影响：%s</text>'
                 % (y, esc(imp)))
        y += 19
        L.append('<text x="32" y="%d" font-size="10.5" fill="#7f1d1d">处置：%s</text>'
                 % (y, esc(fix)))
    else:
        L.append('<rect x="16" y="376" width="648" height="60" rx="10" fill="#f0fdf4" '
                 'stroke="#bbf7d0"/>')
        L.append('<text x="32" y="404" font-size="12.5" font-weight="700" fill="#166534">'
                 '【全部正常】没有失败流程</text>')
        L.append('<text x="32" y="424" font-size="10.5" fill="#15803d">'
                 '（黄色的只是超过预期周期没跑或正在跑 —— GitHub 定时任务常有几小时漂移，属正常）</text>')
    L.append("</svg>")
    return "\n".join(L)


def build_caption(items, ts):
    n_fail = sum(1 for i in items if i["state"] == "fail")
    n_late = sum(1 for i in items if i["state"] in ("late", "running"))
    n_ok = sum(1 for i in items if i["state"] == "ok")
    head = "✅" if not n_fail else "⚠️"
    s = "%s 云流程状态 · %s\n%d 个流程：%d 正常 · %d 超期/进行中 · %d 失败" % (
        head, ts, len(items), n_ok, n_late, n_fail)
    if n_fail:
        for f in [i for i in items if i["state"] == "fail"][:3]:
            err = (f["errors"] or ["(见日志)"])[0]
            s += "\n\n✕ %s（%s）\n%s" % (f["name"], f["time"], err[:120])
    return s[:1000]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true", help="只扫描/出图, 不发 TG")
    ap.add_argument("--text-only", action="store_true", help="不发图, 只发文字")
    ap.add_argument("--out", default=os.path.join(HERE, "flow_status"))
    a = ap.parse_args()

    tokens = {"A": os.environ.get("GH_TOKEN", ""), "B": os.environ.get("GH_TOKEN_TAM", "")}
    if not tokens["A"]:
        log("!! 缺 GH_TOKEN")
        return 1
    log("文案: 说明 %d 条 | 失败建议 %d 条 | 分组 %d 组  (来自 PATHS_JSON; 0 = Secret 里没配)"
        % (len(DESC), len(ADVICE), len(GROUPS)))
    items = []
    for e in WATCH:
        it = scan(e, tokens)
        log("%-14s %-8s %s" % (it["name"], it["state"], it["note"] or it["time"]))
        items.append(it)
    ts = datetime.now(BJ).strftime("%m-%d %H:%M")
    svg = render_svg(items, ts)
    svg_p = a.out + ".svg"
    io.open(svg_p, "w", encoding="utf-8", newline="\n").write(svg)
    log("SVG -> %s (%d 字节)" % (svg_p, len(svg)))
    png_p = ""
    if not a.text_only:
        try:
            subprocess.run(["rsvg-convert", "-z", "2", "-o", a.out + ".png", svg_p],
                           check=True, capture_output=True, timeout=120)
            png_p = a.out + ".png"
            log("PNG -> %s (%d 字节)" % (png_p, os.path.getsize(png_p)))
        except Exception as e:
            log("!! 转 PNG 失败(退回文字): %s" % str(e)[:120])
    if a.no_send:
        log("(--no-send, 不发 TG)")
        return 0
    chat = os.environ.get("TG_CHAT_ID", "")
    if not chat:
        log("!! 缺 TG_CHAT_ID")
        return 1
    cap = build_caption(items, ts)
    if png_p:
        d = tg_call("sendPhoto", {"chat_id": chat, "caption": cap},
                    files={"photo": ("flow_status.png", io.open(png_p, "rb").read())})
    else:
        d = tg_call("sendMessage", {"chat_id": chat, "text": cap})
    log("发 TG -> %s %s" % (d.get("ok"), d.get("err", "")[:120]))
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
