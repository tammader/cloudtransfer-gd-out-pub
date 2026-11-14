# -*- coding: utf-8 -*-
"""Telegram 命令控制: 长轮询 getUpdates -> 解析命令 -> 调 GitHub Actions API

支持的命令:
  /help                 命令列表
  /list                 列出所有流程名
  /status               立即回一份当前状态(文字摘要, 走和状态图同一套扫描)
  /run <流程> [k=v ...] 触发某个流程(可带参数), 例: /run gd-out2 only=AGMX-050
  /stop <流程>          取消正在跑的那次
  /disable <流程>       停用   /enable <流程>  启用

只响应 TG_CHAT_ID 的消息; 其他人发的一律忽略且不回复。
用法: python3 tg_command.py --minutes 340     (跑够分钟数就优雅退出, 由下一个 job 接上)
环境: TG_TOKEN / TG_CHAT_ID / GH_TOKEN / GH_TOKEN_TAM(可选)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import flow_status as fs          # 复用: 监控清单 / 扫描 / 发消息
import tg_javdb_report as jr      # JavDB 评论区报告
import tg_offline_report as orpt  # JavDB 离线->gd6 流程报告

BJ = timezone(timedelta(hours=8))
API = "https://api.github.com"
GH_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def gh_api(method, path, tok, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "token " + tok, "User-Agent": "wb-tg-command",
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        with GH_OP.open(req, timeout=90) as x:
            t = x.read().decode("utf-8", "replace")
            return x.status, (json.loads(t) if t.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return 0, {"_err": str(e)[:200]}


def tokens():
    return {"A": os.environ.get("GH_TOKEN", ""), "B": os.environ.get("GH_TOKEN_TAM", "")}


def find_flow(name):
    """按流程名/文件名找 (repo, wf, tok_key, 显示名)"""
    q = (name or "").strip().lower()
    for repo, wf, disp, _period, tk in fs.WATCH:
        if q in (disp.lower(), wf.lower(), wf.lower().replace(".yml", "")):
            return repo, wf, tk, disp
    return None


def tg_send(text):
    chat = os.environ.get("TG_CHAT_ID", "")
    d = fs.tg_call("sendMessage", {"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"})
    if not d.get("ok"):
        log("!! 发消息失败: %s" % str(d.get("err"))[:150])
    return d


def cmd_help():
    return ("可用命令:\n"
            "/list  —— 列出所有流程名\n"
            "/status —— 立即查一遍当前状态\n"
            "/javdb —— JavDB 评论区 magnet/ed2k 报告（带重跑按钮）\n"
            "/offline —— JavDB 离线→下载→传gd6 流程进度（带触发按钮）\n"
            "/gdy2 —— gd6→ydy2 两条备份（字幕版 / TelegramVideos）进度（带触发按钮）\n"
            "/run <流程> [k=v ...] —— 触发, 例:\n"
            "    /run gd-out2\n"
            "    /run gd-out2 only=AGMX-050 apply=true\n"
            "    /run ydout-in3 apply=false   (只想演练时用)\n"
            "/stop <流程> —— 取消正在跑的那次\n"
            "/disable <流程>  /enable <流程> —— 停用 / 启用")


def cmd_list():
    lines = ["全部流程:"]
    for repo, wf, disp, period, _tk in fs.WATCH:
        lines.append("· %-14s %s%s" % (disp, fs.desc_of(disp),
                                       "" if period else "（手动）"))
    lines.append("\n用 /run <流程名> 触发; /status 看状态")
    return "\n".join(lines)


def cmd_status():
    tk = tokens()
    items = []
    for e in fs.WATCH:
        items.append(fs.scan(e, tk))
    ts = datetime.now(BJ).strftime("%m-%d %H:%M")
    return fs.build_caption(items, ts)


def cmd_run(args):
    if not args:
        return "用法: /run <流程> [k=v ...]    先 /list 看流程名"
    f = find_flow(args[0])
    if not f:
        return "没找到流程 %r —— 发 /list 看可用的名字" % args[0]
    repo, wf, tk_key, disp = f
    tok = tokens().get(tk_key) or ""
    if not tok:
        return "流程 %s 在私有仓库, 但没配 GH_TOKEN_TAM, 无法触发" % disp
    inputs = {}
    for a in args[1:]:
        if "=" in a:
            k, _, v = a.partition("=")
            inputs[k.strip()] = v.strip()
    st, d = gh_api("POST", "/repos/%s/actions/workflows/%s/dispatches" % (repo, wf), tok,
                   {"ref": "main", "inputs": inputs})
    if st in (204, 201, 200):
        time.sleep(6)                     # 等一下好把 run 链接捞出来
        st2, d2 = gh_api("GET", "/repos/%s/actions/workflows/%s/runs?per_page=1" % (repo, wf), tok)
        url = ""
        if st2 == 200 and (d2.get("workflow_runs") or []):
            url = d2["workflow_runs"][0].get("html_url") or ""
        return ("✅ 已触发 %s%s\n%s" % (disp, ("  参数 " + json.dumps(inputs, ensure_ascii=False))
                                       if inputs else "", url))
    return "❌ 触发失败 (%s)：%s" % (st, d.get("_err", "")[:220])


def cmd_stop(args):
    if not args:
        return "用法: /stop <流程>"
    f = find_flow(args[0])
    if not f:
        return "没找到流程 %r" % args[0]
    repo, wf, tk_key, disp = f
    tok = tokens().get(tk_key) or ""
    st, d = gh_api("GET", "/repos/%s/actions/workflows/%s/runs?status=in_progress&per_page=20"
                   % (repo, wf), tok)
    if st != 200:
        return "❌ 查不到运行 (%s)" % st
    runs = d.get("workflow_runs") or []
    if not runs:
        return "%s 当前没有正在跑的" % disp
    n = 0
    for r in runs:
        st2, _ = gh_api("POST", "/repos/%s/actions/runs/%s/cancel" % (repo, r["id"]), tok)
        if st2 in (202, 200):
            n += 1
    return "🛑 已取消 %s 的 %d 个运行" % (disp, n)


def cmd_toggle(args, enable):
    if not args:
        return "用法: /%s <流程>" % ("enable" if enable else "disable")
    f = find_flow(args[0])
    if not f:
        return "没找到流程 %r" % args[0]
    repo, wf, tk_key, disp = f
    tok = tokens().get(tk_key) or ""
    st, d = gh_api("GET", "/repos/%s/actions/workflows/%s" % (repo, wf), tok)
    if st != 200 or not d.get("id"):
        return "❌ 取 workflow id 失败 (%s)" % st
    act = "enable" if enable else "disable"
    st2, d2 = gh_api("PUT", "/repos/%s/actions/workflows/%s/%s" % (repo, d["id"], act), tok)
    if st2 in (204, 200):
        return "%s %s 已%s" % ("▶️" if enable else "⏸", disp, "启用" if enable else "停用")
    return "❌ 操作失败 (%s)：%s" % (st2, d2.get("_err", "")[:200])


def cmd_javdb():
    """生成并发送 JavDB 评论区报告，带 inline 按钮（重跑/刷新）"""
    try:
        text, n = jr.build_report()
    except Exception as e:
        return "❌ 读取报告失败：%s" % str(e)[:180]
    d = jr.send_report(text, with_button=True)
    if d.get("ok"):
        return None  # 已由 send_report 发出，不再重复回一条
    return "❌ 发送失败：%s" % str(d.get("err", ""))[:180]


def cmd_offline():
    """JavDB 离线->下载->gd6 流程报告（带按钮）"""
    try:
        text = orpt.build_report()
    except Exception as e:
        return "❌ 读取离线流程报告失败：%s" % str(e)[:180]
    d = orpt.send_report(text, with_button=True)
    if d.get("ok"):
        return None
    return "❌ 发送失败：%s" % str(d.get("err", ""))[:180]


def _gdy2_info(flow):
    """(显示名, workflow 文件, 说明)"""
    if flow == "tgv":
        return ("gd6:TelegramVideos → ydy2/TelegramVideos", "gd6-ydy2-tgvideo.yml",
                "Telegram 视频直投 gd6 后, 再单向备份到移动个人云")
    return ("gd6:out3/字幕版 → ydy2/ycl", "gd6-ydy2-backup.yml",
            "字幕版整片备份到移动个人云")


def cmd_gdy2(flow="subt"):
    """gd6 → ydy2 两条备份的进度(带快捷按钮)"""
    name, wf, why = _gdy2_info(flow)
    repo = os.environ.get("GDY2_REPO", "tammader/cloudtransfer-gd-out-pub")
    tok = tokens().get("A") or os.environ.get("GH_TOKEN", "")
    st, d = gh_api("GET", "/repos/%s/actions/workflows/%s/runs?per_page=1" % (repo, wf), tok)
    lines = ["📦 %s" % name, why]
    if st == 200 and (d.get("workflow_runs") or []):
        r = d["workflow_runs"][0]
        concl = r.get("conclusion") or r.get("status")
        lines.append("最近一轮: %s (%s)" % (concl, (r.get("created_at") or "")[:16].replace("T", " ")))
        lines.append(r.get("html_url") or "")
    else:
        lines.append("(还没跑过 / 查不到运行记录)")
    lines.append("\n点下面按钮可以就地触发或刷新")
    return "\n".join(lines)


def tg_answer_cb(cb_id, text=""):
    fs.tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:180]})


def handle_callback(cb):
    """处理 inline 按钮回调。返回要回给用户的话（None=已自行发送）。"""
    data = cb.get("data") or ""
    if data == "javdb_refresh":
        return cmd_javdb()
    if data == "offline_refresh":
        return cmd_offline()
    if data == "offline_rerun":
        repo = os.environ.get("OFFLINE_REPO", "tammader/javdb-offline-gd6")
        wf = "offline-download-gd6.yml"
        tok = tokens().get("A") or os.environ.get("GH_TOKEN", "")
        st, d = gh_api("POST", "/repos/%s/actions/workflows/%s/dispatches" % (repo, wf), tok,
                       {"ref": "main", "inputs": {}})
        if st in (204, 200, 201):
            time.sleep(6)
            st2, d2 = gh_api("GET", "/repos/%s/actions/workflows/%s/runs?per_page=1" % (repo, wf), tok)
            url = ""
            if st2 == 200 and (d2.get("workflow_runs") or []):
                url = d2["workflow_runs"][0].get("html_url") or ""
            return "⚡ 已触发离线+搬运流程（每天各盘30额度，自动跑批次）\n%s" % url
        return "❌ 触发失败(%s)：%s" % (st, str(d.get("_err", ""))[:180])
    if data in ("gdy2_subt_refresh", "gdy2_tgv_refresh"):
        flow = "subt" if "subt" in data else "tgv"
        return cmd_gdy2(flow)
    if data in ("gdy2_subt_rerun", "gdy2_tgv_rerun"):
        flow = "subt" if "subt" in data else "tgv"
        repo = os.environ.get("GDY2_REPO", "tammader/cloudtransfer-gd-out-pub")
        wf = "gd6-ydy2-backup.yml" if flow == "subt" else "gd6-ydy2-tgvideo.yml"
        tok = tokens().get("A") or os.environ.get("GH_TOKEN", "")
        st, d = gh_api("POST", "/repos/%s/actions/workflows/%s/dispatches" % (repo, wf), tok,
                       {"ref": "main", "inputs": {"apply": "true"}})
        if st in (204, 200, 201):
            time.sleep(6)
            st2, d2 = gh_api("GET", "/repos/%s/actions/workflows/%s/runs?per_page=1" % (repo, wf), tok)
            url = ""
            if st2 == 200 and (d2.get("workflow_runs") or []):
                url = d2["workflow_runs"][0].get("html_url") or ""
            return ("⚡ 已触发 %s 备份（真传一轮）\n%s"
                    % ("字幕版" if flow == "subt" else "TelegramVideos", url))
        return "❌ 触发失败(%s)：%s" % (st, str(d.get("_err", ""))[:180])
    if data == "javdb_rerun":
        # 触发 javdb-comment-links workflow（如果已在 WATCH，或按仓库/文件名直接触发）
        repo = os.environ.get("JAVDB_REPO", "tammader/javdb-comment-links")
        wf = os.environ.get("JAVDB_WF", "javdb-comment-links.yml")
        tok = tokens().get("A") or os.environ.get("GH_TOKEN", "")
        st, d = gh_api("POST", "/repos/%s/actions/workflows/%s/dispatches" % (repo, wf), tok,
                       {"ref": "main", "inputs": {}})
        if st in (204, 200, 201):
            time.sleep(6)
            st2, d2 = gh_api("GET", "/repos/%s/actions/workflows/%s/runs?per_page=1" % (repo, wf), tok)
            url = ""
            if st2 == 200 and (d2.get("workflow_runs") or []):
                url = d2["workflow_runs"][0].get("html_url") or ""
            return "⚡ 已触发 JavDB 抓取，跑完会自动发新报告\n%s" % url
        return "❌ 触发失败(%s)：%s" % (st, str(d.get("_err", ""))[:180])
    return "未知按钮 %r" % data


def handle(text):
    parts = text.strip().split()
    if not parts:
        return None
    c = parts[0].lower().split("@")[0]
    a = parts[1:]
    if c in ("/help", "/start", "/?"):
        return cmd_help()
    if c == "/list":
        return cmd_list()
    if c == "/status":
        tg_send("⏳ 正在查各流程状态…")
        return cmd_status()
    if c in ("/javdb", "/报告"):
        tg_send("⏳ 正在生成 JavDB 报告…")
        return cmd_javdb()
    if c in ("/gdy2", "/备份"):
        tg_send("⏳ 正在查 gd6→ydy2 备份进度…")
        return cmd_gdy2("subt")
    if c in ("/gdy2t", "/tgvideo"):
        tg_send("⏳ 正在查 TelegramVideos 备份进度…")
        return cmd_gdy2("tgv")
    if c in ("/offline", "/离线"):
        tg_send("⏳ 正在查离线流程进度…")
        return cmd_offline()
    if c == "/run":
        return cmd_run(a)
    if c == "/stop":
        return cmd_stop(a)
    if c == "/disable":
        return cmd_toggle(a, False)
    if c == "/enable":
        return cmd_toggle(a, True)
    return "不认识的命令 %s —— 发 /help 看用法" % c


def get_updates(offset, wait=50):
    tok = os.environ.get("TG_TOKEN", "")
    url = ("https://api.telegram.org/bot%s/getUpdates?timeout=%d&allowed_updates=%s"
           % (tok, wait, urllib.parse.quote('["message","callback_query"]')))
    if offset:
        url += "&offset=%d" % offset
    try:
        with urllib.request.urlopen(url, timeout=wait + 25) as x:
            return json.loads(x.read().decode("utf-8", "replace"))
    except Exception as e:
        log("getUpdates 异常: %s" % str(e)[:120])
        return {"ok": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=340, help="跑多少分钟后退出")
    a = ap.parse_args()
    chat = os.environ.get("TG_CHAT_ID", "")
    if not os.environ.get("TG_TOKEN") or not chat:
        log("!! 缺 TG_TOKEN / TG_CHAT_ID")
        return 1
    log("开始长轮询 (最多 %d 分钟, 只认 chat %s)" % (a.minutes, chat[:4] + "***"))
    t0 = time.time()
    offset = 0
    n_cmd = 0
    while (time.time() - t0) / 60.0 < a.minutes:
        d = get_updates(offset)
        if not d.get("ok"):
            time.sleep(5)
            continue
        for u in d.get("result") or []:
            offset = max(offset, int(u.get("update_id", 0)) + 1)
            # --- 按钮回调 ---
            cb = u.get("callback_query")
            if cb:
                cchat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
                if cchat != chat:
                    log("忽略非授权 callback (chat=%s)" % (cchat[:4] + "***"))
                    continue
                n_cmd += 1
                tg_answer_cb(cb.get("id"), "处理中…")
                try:
                    rep = handle_callback(cb)
                    if rep:
                        tg_send(rep)
                except Exception as e:
                    log("!! 回调异常: %s" % str(e)[:150])
                    tg_send("❌ 回调出错：%s" % str(e)[:200])
                continue
            msg = u.get("message") or {}
            ch = str((msg.get("chat") or {}).get("id") or "")
            text = (msg.get("text") or "").strip()
            if ch != chat:
                log("忽略非授权消息 (chat=%s)" % (ch[:4] + "***"))
                continue
            if not text:
                continue
            n_cmd += 1
            log("命令: %s" % text[:60])
            try:
                rep = handle(text)
                if rep:
                    tg_send(rep)
            except Exception as e:
                log("!! 处理命令异常: %s" % str(e)[:150])
                tg_send("❌ 执行出错：%s" % str(e)[:200])
    log("到达时长上限, 优雅退出 (本轮处理 %d 条命令)" % n_cmd)
    try:
        tg_send("ℹ️ 控制进程到期退出，下一个时段自动接上（约几分钟内恢复响应）。")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
