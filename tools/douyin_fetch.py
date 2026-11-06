# -*- coding: utf-8 -*-
"""抖音收藏 -> 视频直链清单 (Playwright 拦截 XHR)

设计理由（为什么不走纯 API）:
  抖音 web 收藏接口 `/aweme/v1/web/aweme/favorite/` 需要 a_bogus / X-Bogus 签名,
  这两个算法周周变, 纯 API 方案几乎必坏。真实 Chromium 打开页面、由页面自己的 JS
  生成签名并发出请求, 我们只拦截它的 XHR 响应 —— 天然绕过签名维护问题, 长期最稳。

用法:
  python douyin_fetch.py --out favorites.json [--max 200] [--headful]

输出 favorites.json:
  {"ts": "...", "count": N, "items": [
      {"aweme_id": "...", "desc": "...", "author": "...", "url": "https://...",
       "size": 1234567, "create_time": 1700000000}, ...]}

环境变量:
  DOUYIN_COOKIE   : 必填(本机或 Secrets)。整段 Cookie 字符串, 至少含 sessionid / ttwid。
  PW_CHROME_PATH  : 可选, 指定 Chromium 可执行文件(容器内一般不用)。
"""
import argparse
import json
import os
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except Exception as e:  # pragma: no cover
    print("!! 需要 playwright: pip install playwright && playwright install chromium")
    raise


FAV_API_MARK = "/aweme/v1/web/aweme/favorite/"
FAV_PAGE = "https://www.douyin.com/user/self?showTab=favorite_collection"


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def parse_cookie(s):
    """把 'k1=v1; k2=v2' 转成 playwright 的 cookie 列表(仅 name/value, domain 由浏览器补)"""
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out.append({"name": k, "value": v})
    return out


def extract_video(item):
    """从一个 aweme 结构里挑出**无水印** MP4 直链 + 元数据"""
    aweme_id = str(item.get("aweme_id") or "")
    if not aweme_id:
        return None
    desc = (item.get("desc") or "").strip()
    author = ((item.get("author") or {}).get("nickname") or "").strip()
    ct = item.get("create_time") or 0
    video = item.get("video") or {}

    # 优先 play_addr(无水印), 其次 download_addr; 取第一条 url_list
    cands = []
    for key in ("play_addr", "play_addr_h264", "download_addr"):
        addr = video.get(key) or {}
        for u in (addr.get("url_list") or []):
            if u and "http" in u:
                cands.append(u)
    # 有些版本把 url 放在 bit_rate 列表里
    for br in (video.get("bit_rate") or []):
        pa = (br.get("play_addr") or {})
        for u in (pa.get("url_list") or []):
            if u and "http" in u:
                cands.append(u)
    if not cands:
        return None

    # 预估值: 用 play_addr 的 data_size, 拎不到就算 0(主脚本会以实际下载字节为准)
    size = 0
    for key in ("play_addr", "download_addr"):
        size = size or int((video.get(key) or {}).get("data_size") or 0)
    return {
        "aweme_id": aweme_id,
        "desc": desc,
        "author": author,
        "url": cands[0],
        "url_list": cands[:4],
        "size": size,
        "create_time": int(ct),
    }


def run(out_path, max_n, headful, cookie, timeout_s=90):
    found = {}
    stop = {"n": 0}

    def on_response(resp):
        try:
            if FAV_API_MARK not in resp.url:
                return
            data = resp.json()
        except Exception:
            return
        lst = (data or {}).get("aweme_list") or []
        for it in lst:
            v = extract_video(it)
            if v and v["aweme_id"] not in found:
                found[v["aweme_id"]] = v
        log("  拦截到收藏接口: 本页 %d 条, 累计去重 %d 条" % (len(lst), len(found)))

    with sync_playwright() as p:
        launch_kw = {"headless": not headful}
        chrome_path = os.environ.get("PW_CHROME_PATH") or ""
        if chrome_path:
            launch_kw["executable_path"] = chrome_path
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/157.0.0.0 Safari/537.36"),
        )
        cookies = parse_cookie(cookie)
        if cookies:
            # 补 domain, 否则 playwright 不认
            for c in cookies:
                c["domain"] = ".douyin.com"
                c["path"] = "/"
            try:
                ctx.add_cookies(cookies)
                log("注入 Cookie %d 条" % len(cookies))
            except Exception as e:
                log("!! Cookie 注入失败: %s" % str(e)[:120])

        page = ctx.new_page()
        page.on("response", on_response)
        log("打开收藏页: %s" % FAV_PAGE)
        try:
            page.goto(FAV_PAGE, timeout=timeout_s * 1000, wait_until="domcontentloaded")
        except Exception as e:
            log("!! 打开页面异常(继续尝试滚动): %s" % str(e)[:150])

        # 登录检测(粗判: 看有没有登录弹窗/跳 login)
        time.sleep(4)
        cur = page.url or ""
        if "login" in cur.lower():
            log("!! 疑似未登录(URL=%s) —— Cookie 可能已过期" % cur)

        # 滚动加载, 触发分页收藏接口
        last = 0
        stagnant = 0
        for i in range(60):
            try:
                page.mouse.wheel(0, 4000)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(1.6)
            n = len(found)
            log("  滚动 %d: 累计 %d" % (i + 1, n))
            if n >= max_n:
                log("已达上限 %d, 停止" % max_n)
                break
            if n == last:
                stagnant += 1
                if stagnant >= 6:
                    log("连续 %d 次无新增, 判定到底" % stagnant)
                    break
            else:
                stagnant = 0
            last = n

        browser.close()

    items = list(found.values())
    items.sort(key=lambda x: x.get("create_time") or 0, reverse=True)
    if max_n:
        items = items[:max_n]
    payload = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "count": len(items), "items": items}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    log("写出 %s: %d 条" % (out_path, len(items)))
    if not items:
        log("!! 收藏清单为空: Cookie 过期 / 真没收藏 / 被风控。看上面的 URL 与滚动日志判断。")
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="favorites.json")
    ap.add_argument("--max", type=int, default=200, help="最多取多少条(0=不限)")
    ap.add_argument("--headful", action="store_true", help="本机排障用, 显示浏览器")
    ap.add_argument("--timeout", type=int, default=90)
    a = ap.parse_args()
    cookie = (os.environ.get("DOUYIN_COOKIE") or "").strip()
    if not cookie:
        log("!! 缺 DOUYIN_COOKIE 环境变量")
        sys.exit(3)
    sys.exit(run(a.out, a.max, a.headful, cookie, a.timeout))


if __name__ == "__main__":
    main()
