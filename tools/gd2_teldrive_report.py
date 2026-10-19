# -*- coding: utf-8 -*-
"""gd2-teldrive 备份报告图: 读统计 JSON -> 画 SVG -> 转 PNG -> 发 Telegram

跟 tools/flow_status.py 是同一套路(sendPhoto 发图), 只是数据源换成
gd2_teldrive_backup.py 落的 TD_STATS_JSON。

用法:
  python3 tools/gd2_teldrive_report.py                  # 出图 + 发 TG
  python3 tools/gd2_teldrive_report.py --no-send        # 只出图, 不发
  python3 tools/gd2_teldrive_report.py --text-only      # 不发图, 只发文字

需要的环境变量:
  TG_TOKEN / TG_CHAT_ID                —— 不发 TG 时可缺
  TD_STATS_JSON                        —— 统计落点(默认 /tmp/gd2teldrive/stats.json)

统计文件不存在怎么办:
  不报错退出 —— 会画一张「⚠️ 备份没跑出统计」的卡片照样发出来。
  这样即使备份步骤自己炸了(装 rclone 失败/鉴权 401), 也能收到通知。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TG = "https://api.telegram.org"
STATS = os.environ.get("TD_STATS_JSON", "/tmp/gd2teldrive/stats.json")

GB = 1024 ** 3
MB = 1024 ** 2

# 配色(浅色底, Telegram 里看得清)
BG = "#f8fafc"
CARD = "#ffffff"
LINE = "#e2e8f0"
TXT = "#0f172a"
SUB = "#64748b"
BLUE = "#0284c7"
GREEN = "#16a34a"
AMBER = "#ca8a04"
RED = "#dc2626"


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def fmt_size(b):
    b = float(b or 0)
    if b >= GB:
        return "%.2f GB" % (b / GB)
    if b >= MB:
        return "%.0f MB" % (b / MB)
    return "%.0f KB" % (b / 1024)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------- Telegram
def tg_call(method, payload, files=None):
    """Telegram API。runner 上直连即可(本机需要系统代理)。"""
    tok = os.environ.get("TG_TOKEN", "").strip()
    if not tok:
        return {"ok": False, "err": "缺 TG_TOKEN"}
    url = "%s/bot%s/%s" % (TG, tok, method)
    if files:
        boundary = "----wbtd%d" % int(time.time())
        body = b""
        for k, v in (payload or {}).items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                     % (boundary, k, v)).encode("utf-8")
        for k, (fn, data) in files.items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                     'Content-Type: image/png\r\n\r\n' % (boundary, k, fn)).encode("utf-8")
            body += data + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary})
    else:
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(payload or {}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=120) as x:
            return json.loads(x.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:160]}


# ---------------------------------------------------------------- 数据
def load_stats():
    """读统计 JSON; 读不到就返回 None(由调用方画「无统计」卡片)"""
    try:
        with io.open(STATS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception as e:
        log("!! 读统计失败(%s): %s" % (STATS, str(e)[:120]))
        return None


# ---------------------------------------------------------------- 画图
def render_svg(s):
    """s = 统计字典; s is None 时画「没跑出统计」卡片"""
    W = 720
    broken = s is None
    d = s or {}
    mode = d.get("mode", "未知")
    apply_ = bool(d.get("apply"))
    bad = broken or d.get("rc", 0) not in (0, 124, 130)

    # ---- 高度: 标题 + 卡片 + 进度 + 明细 + 底注 ----
    # 真跑看「未完成」, 演练看「入选计划」; 统计缺失(broken)时没有明细可列。
    # 注意兜住 None: 演练模式的 stats 里没有 left_detail, 直接 or 会拿到 None。
    detail = [] if broken else ((d.get("left_detail") if apply_ else d.get("plan_detail")) or [])
    n_row = min(len(detail), 6)
    H = 132 + (86 if not broken else 0) + (64 if (apply_ and not broken) else 0) \
        + (26 + n_row * 22 if n_row else 0) + 56

    L = []
    L.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'font-family="Noto Sans CJK SC,DejaVu Sans,sans-serif">' % (W, H))
    L.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, BG))

    # 标题
    L.append('<text x="20" y="34" font-size="19" font-weight="bold" fill="%s">'
             'gdrive2 → Teldrive 冷备份</text>' % TXT)
    ts = d.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S")
    # 触发来源(schedule / workflow_dispatch)打在副标题上, 一眼分清是定时还是手动
    ev = d.get("event") or ""
    tail = ("演练(没真传)" if not apply_ else mode) + (" · %s" % ev if ev else "")
    L.append('<text x="20" y="54" font-size="12" fill="%s">%s · %s</text>'
             % (SUB, esc(ts), esc(tail)))

    # 右上角状态徽章
    btxt, bcol = ("⚠ 异常", RED) if bad else (
        ("○ 演练", SUB) if not apply_ else (
            ("⏱ 预算到点", AMBER) if d.get("budget_hit") else ("✓ 完成", GREEN)))
    bw = 96
    L.append('<rect x="%d" y="18" width="%d" height="28" rx="14" fill="%s" '
             'opacity="0.12"/>' % (W - 20 - bw, bw, bcol))
    L.append('<text x="%d" y="37" font-size="13" font-weight="bold" fill="%s" '
             'text-anchor="middle">%s</text>' % (W - 20 - bw / 2, bcol, esc(btxt)))

    if broken:
        L.append('<rect x="20" y="70" width="%d" height="58" rx="10" fill="%s" '
                 'stroke="%s"/>' % (W - 40, "#fee2e2", "#fecaca"))
        L.append('<text x="36" y="94" font-size="14" font-weight="bold" fill="%s">'
                 '本轮没跑出统计 —— 备份步骤可能挂了</text>' % "#b91c1c")
        L.append('<text x="36" y="114" font-size="12" fill="%s">'
                 '常见原因: rclone 装不上 / Teldrive 401 / 源端列不出</text>' % "#dc2626")
    else:
        # ---- 四个数字卡片 ----
        cards = [
            ("本轮新增文件", "%d" % d.get("new_files", 0), BLUE),
            ("本轮新增量", fmt_size(d.get("new_bytes", 0)), BLUE),
            ("目标端累计", fmt_size(d.get("dst_bytes", 0)), GREEN),
            ("整目录完成", "%d / %d" % (d.get("done_dirs", 0), d.get("plan_dirs", 0)),
             GREEN if not d.get("left_dirs") else AMBER),
        ]
        cw = (W - 40 - 3 * 10) / 4.0
        for i, (k, v, col) in enumerate(cards):
            x = 20 + i * (cw + 10)
            L.append('<rect x="%.1f" y="70" width="%.1f" height="72" rx="10" '
                     'fill="%s" stroke="%s"/>' % (x, cw, CARD, LINE))
            L.append('<text x="%.1f" y="94" font-size="11" fill="%s">%s</text>'
                     % (x + 12, SUB, esc(k)))
            L.append('<text x="%.1f" y="120" font-size="19" font-weight="bold" '
                     'fill="%s">%s</text>' % (x + 12, col, esc(v)))

        y = 168
        if apply_:
            # ---- 本轮进度条 ----
            plan = float(d.get("plan_bytes", 0) or 0)
            done = float(d.get("new_bytes", 0) or 0)
            pct = min(1.0, done / plan) if plan else 0.0
            L.append('<text x="20" y="%d" font-size="12" fill="%s">'
                     '本轮进度  %s / %s</text>'
                     % (y, SUB, fmt_size(done), fmt_size(plan)))
            by = y + 10
            L.append('<rect x="20" y="%d" width="%d" height="12" rx="6" fill="%s"/>'
                     % (by, W - 40, LINE))
            if pct > 0:
                L.append('<rect x="20" y="%d" width="%.1f" height="12" rx="6" fill="%s"/>'
                         % (by, (W - 40) * pct, GREEN if pct >= 0.999 else BLUE))
            L.append('<text x="%d" y="%d" font-size="11" fill="%s" text-anchor="end">'
                     '%.0f%%</text>' % (W - 20, by + 10, SUB, pct * 100))
            y = by + 34

        # ---- 明细(残留 / 演练时的入选计划) ----
        if n_row:
            title = "未完成(下次重挑)" if apply_ else "本轮入选计划"
            L.append('<text x="20" y="%d" font-size="12" font-weight="bold" fill="%s">'
                     '%s</text>' % (y + 4, TXT, esc(title)))
            y += 24
            for i, it in enumerate(detail[:6]):
                nm = it.get("dir", "?")
                if len(nm) > 46:
                    nm = nm[:44] + "…"
                col = RED if apply_ else TXT
                L.append('<text x="24" y="%d" font-size="11.5" fill="%s">%s</text>'
                         % (y, col, esc(nm)))
                L.append('<text x="%d" y="%d" font-size="11.5" fill="%s" '
                         'text-anchor="end">%d 个 · %s</text>'
                         % (W - 24, y, SUB, it.get("n", 0), fmt_size(it.get("bytes", 0))))
                y += 22

    # ---- 底注 ----
    fy = H - 20
    if not broken:
        note = "并发 %s · 分片 %sMi · 预算 %s 分钟 · 耗时 %.1f 分钟" % (
            d.get("transfers", "?"), d.get("chunk_mb", "?"),
            d.get("budget_min", "?"), d.get("minutes", 0))
        if d.get("note"):
            note += " · %s" % d["note"]
        if len(note) > 118:
            note = note[:116] + "…"
        L.append('<text x="20" y="%d" font-size="10.5" fill="%s">%s</text>'
                 % (fy, SUB, esc(note)))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    rid = os.environ.get("GITHUB_RUN_ID", "")
    if repo and rid:
        L.append('<text x="%d" y="%d" font-size="10.5" fill="%s" text-anchor="end">'
                 '%s #%s</text>' % (W - 20, fy, SUB, esc(repo), esc(rid)))
    L.append("</svg>")
    return "\n".join(L)


def build_caption(s):
    if s is None:
        return ("⚠️ gd2 → Teldrive 备份：本轮没跑出统计，备份步骤可能挂了\n"
                "（装 rclone / Teldrive 401 / 源端列不出）—— 去看 Actions 日志")
    d = s
    apply_ = bool(d.get("apply"))
    head = ("✅" if d.get("rc", 0) in (0, 124, 130) else "⚠️")
    if not apply_:
        return ("%s gd2 → Teldrive 备份（演练）\n计划 %d 个目录 / %s，未真传\n"
                "源端 %d 文件 · 含视频目录 %d 个"
                % (head, d.get("plan_dirs", 0), fmt_size(d.get("plan_bytes", 0)),
                   d.get("src_files", 0), d.get("video_dirs", 0)))
    txt = ("%s gd2 → Teldrive 备份 · %s\n本轮新增 %d 个 / %s\n"
           "目标端累计 %d 个 / %s"
           % (head, d.get("ts", ""), d.get("new_files", 0),
              fmt_size(d.get("new_bytes", 0)),
              d.get("dst_files", 0), fmt_size(d.get("dst_bytes", 0))))
    if d.get("budget_hit"):
        txt += "\n⏱ 时间预算到点，下次接着传"
    if d.get("left_dirs"):
        txt += "\n仍有 %d 个目录未传完" % d["left_dirs"]
        for it in (d.get("left_detail") or [])[:2]:
            txt += "\n· %s（缺 %d 个 / %s）" % (
                it.get("dir", "?"), it.get("n", 0), fmt_size(it.get("bytes", 0)))
    if d.get("rc", 0) not in (0, 124, 130):
        txt += "\n⚠ rclone 退出码 %s" % d.get("rc")
    return txt[:1000]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true", help="只出图, 不发 TG")
    ap.add_argument("--text-only", action="store_true", help="不发图, 只发文字")
    ap.add_argument("--out", default=os.path.join(HERE, "gd2_td_report"))
    a = ap.parse_args()

    s = load_stats()
    log("统计: %s" % ("读到" if s else "缺失 -> 画异常卡片"))

    svg = render_svg(s)
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

    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not chat:
        log("!! 缺 TG_CHAT_ID")
        return 1
    cap = build_caption(s)
    if png_p:
        d = tg_call("sendPhoto", {"chat_id": chat, "caption": cap},
                    files={"photo": ("gd2_td_report.png", io.open(png_p, "rb").read())})
    else:
        d = tg_call("sendMessage", {"chat_id": chat, "text": cap})
    log("发 TG -> %s %s" % (d.get("ok"), d.get("err", "")[:120]))
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
