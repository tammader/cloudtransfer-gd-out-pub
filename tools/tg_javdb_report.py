# -*- coding: utf-8 -*-
"""JavDB 评论区 magnet/ed2k 报告 -> Telegram

读取 javdb-crawler 的输出（或本机 javdb 目录的 CSV），拼成一条 Telegram 报告，
并可带一个「立即重跑」的 inline 按钮（callback -> 触发 GitHub workflow_dispatch）。

用法:
  python tg_javdb_report.py --send                 # 发到 TG (需 TG_TOKEN/TG_CHAT_ID)
  python tg_javdb_report.py --text                 # 只打印文案
  python tg_javdb_report.py --csv 路径.csv --send

报告内容:
  · 各目录：视频文件数 / 番号数 / 映射成功数 / 链接数(magnet+ed2k) / 时间范围
  · 最近新增链接（最新 8 条，番号 + 类型 + 链接前缀）
  · 更新时间
"""
import argparse
import csv
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))


def _load_rows(csv_path):
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def _scan_dirs(base_glob):
    """找所有形如 <prefix>_magnet_ed2k_*.csv 的结果文件"""
    out = []
    for p in glob.glob(base_glob):
        name = os.path.basename(p)
        prefix = name.split('_magnet_ed2k_')[0]
        out.append((prefix, p))
    return sorted(out)


def _load_progress():
    """读取 state + movie_codes，算出累计进度：已处理 / 总番号 / 剩余"""
    base = os.environ.get('JAVDB_OUT_DIR', '.')
    dist_dir = os.environ.get('JAVDB_DIST_DIR', base)  # movie_codes.json 所在（workflow 里=仓库根）
    total = None
    for d in (dist_dir, base, '.'):
        p = os.path.join(d, 'movie_codes.json')
        if os.path.exists(p):
            try:
                total = len(json.load(open(p, encoding='utf-8')).get('codes', []))
                break
            except Exception:
                pass
    st = None
    for d in (base, dist_dir, '.'):
        p = os.path.join(d, os.environ.get('STATE_FILE', 'state_javdb.json'))
        if os.path.exists(p):
            try:
                st = json.load(open(p, encoding='utf-8'))
                break
            except Exception:
                pass
    if st is None:
        return None
    done = len(st.get('resolved', {})) + len(st.get('no_match', []))
    resolved = len(st.get('resolved', {}))
    no_match = len(st.get('no_match', []))
    return {'done': done, 'total': total, 'resolved': resolved, 'no_match': no_match,
            'remaining': (total - done) if total is not None else None}


def build_report(csv_paths=None, state_paths=None):
    """返回 (text, total_links)"""
    if csv_paths is None:
        base = os.environ.get('JAVDB_OUT_DIR', '.')
        csv_paths = [path for _prefix, path in _scan_dirs(os.path.join(base, '*_magnet_ed2k_*.csv'))]

    lines = []
    total_mag = total_ed2k = total_rows = 0
    all_rows = []
    folder_counter = {}
    for cp in csv_paths:
        rows = _load_rows(cp)
        if not rows and not os.path.exists(cp):
            continue
        name = os.path.basename(cp).split('_magnet_ed2k_')[0]
        mag = sum(1 for r in rows if r.get('link_type') == 'magnet')
        ed2k = sum(1 for r in rows if r.get('link_type') == 'ed2k')
        dates = [r.get('review_created_at', '')[:10] for r in rows if r.get('review_created_at')]
        rng = ('%s ~ %s' % (min(dates), max(dates))) if dates else '无'
        movies = len({r.get('movie_id') for r in rows if r.get('movie_id')})
        lines.append('· %s：%d 部片有链 · %d 链接（🧲%d / 🔗%d） · %s'
                     % (name, movies, len(rows), mag, ed2k, rng))
        total_mag += mag
        total_ed2k += ed2k
        total_rows += len(rows)
        all_rows.extend(rows)
        # 按 folders 列细分目录来源
        for r in rows:
            for fo in (r.get('folders') or '').split('、'):
                fo = fo.strip()
                if fo:
                    folder_counter[fo] = folder_counter.get(fo, 0) + 1

    ts = datetime.now(BJ).strftime('%Y-%m-%d %H:%M')
    head = '📊 JavDB 评论区 magnet/ed2k 报告 · %s' % ts
    summary = '合计 %d 个去重链接（🧲 magnet %d / 🔗 ed2k %d）' % (total_rows, total_mag, total_ed2k)
    body = head + '\n' + summary

    # 进度：累计已处理 / 总番号 / 剩余（分多轮慢速补齐，这里看整体进度）
    prog = _load_progress()
    if prog:
        if prog['total']:
            pct = int(round(prog['done'] * 100.0 / prog['total'])) if prog['total'] else 0
            body += '\n📈 进度：已处理 %d/%d 个番号（%d%%）· 剩余 %d · 其中命中 %d / 无匹配 %d' % (
                prog['done'], prog['total'], pct, prog['remaining'], prog['resolved'], prog['no_match'])
        else:
            body += '\n📈 进度：已处理 %d 个番号（命中 %d / 无匹配 %d）' % (
                prog['done'], prog['resolved'], prog['no_match'])

    body += '\n\n' + ('\n'.join(lines) if lines else '（还没有结果）')

    # 按目录来源细分（多个目录合并时才有意义）
    if folder_counter and len(folder_counter) > 1:
        body += '\n\n📁 按目录：' + ' · '.join('%s %d' % (k, v) for k, v in sorted(folder_counter.items()))

    # 最新 8 条
    all_rows.sort(key=lambda r: r.get('review_created_at', ''), reverse=True)
    if all_rows:
        body += '\n\n🆕 最新评论链接：'
        for r in all_rows[:8]:
            link = r.get('link', '')
            short = link[:48] + ('…' if len(link) > 48 else '')
            body += '\n· [%s] %s %s' % (r.get('code', ''), '🧲' if r.get('link_type') == 'magnet' else '🔗', short)
    return body[:3900], total_rows


def send_report(text, with_button=True, token=None, chat=None):
    token = token or os.environ.get('TG_TOKEN', '')
    chat = chat or os.environ.get('TG_CHAT_ID', '')
    if not token or not chat:
        return {'ok': False, 'err': '缺 TG_TOKEN / TG_CHAT_ID'}
    import urllib.parse
    import urllib.request
    payload = {'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true'}
    if with_button:
        kb = {'inline_keyboard': [[
            {'text': '⚡ 立即重跑 JavDB 抓取', 'callback_data': 'javdb_rerun'},
            {'text': '🔄 刷新报告', 'callback_data': 'javdb_refresh'},
        ]]}
        payload['reply_markup'] = json.dumps(kb)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request('https://api.telegram.org/bot%s/sendMessage' % token,
                                 data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            return json.loads(x.read().decode('utf-8', 'replace'))
    except Exception as e:
        return {'ok': False, 'err': str(e)[:200]}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', nargs='*', help='CSV 文件（默认自动扫）')
    ap.add_argument('--text', action='store_true', help='只打印，不发')
    ap.add_argument('--send', action='store_true', help='发到 TG')
    ap.add_argument('--no-button', action='store_true')
    a = ap.parse_args()
    txt, n = build_report(a.csv)
    print(txt)
    if a.send:
        d = send_report(txt, with_button=not a.no_button)
        print('\n[TG]', d.get('ok'), d.get('err', ''))
