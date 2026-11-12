# -*- coding: utf-8 -*-
"""JavDB 离线→下载→传gd6 流程报告 -> Telegram。
读 tammader/javdb-offline-gd6 仓库的 state.json + 最近 Actions 运行状态。
"""
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse

OWNER = os.environ.get('OFFLINE_REPO_OWNER', 'tammader')
REPO = os.environ.get('OFFLINE_REPO', 'javdb-offline-gd6')
API = 'https://api.github.com'
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _gh(path, tok):
    req = urllib.request.Request(API + path, headers={'Authorization': 'token ' + tok, 'User-Agent': 'x', 'Accept': 'application/vnd.github+json'})
    try:
        return json.loads(OP.open(req, timeout=30).read().decode())
    except Exception:
        return None


def _read_state(tok):
    d = _gh('/repos/%s/%s/contents/state.json' % (OWNER, REPO), tok)
    if not d:
        return {}
    import base64
    try:
        return json.loads(base64.b64decode(d['content']).decode('utf-8'))
    except Exception:
        return {}


def _latest_run(tok):
    d = _gh('/repos/%s/%s/actions/workflows/offline-download-gd6.yml/runs?per_page=1' % (OWNER, REPO), tok)
    runs = (d or {}).get('workflow_runs') or []
    return runs[0] if runs else None


def build_report(token=None):
    tok = token or os.environ.get('GH_TOKEN', '')
    st = _read_state(tok)
    offlined = st.get('offlined', {})
    done = st.get('done', [])
    run = _latest_run(tok)

    total_links = os.environ.get('OFFLINE_TOTAL_LINKS', '')  # 可选，从 secret 环境注入总数
    lines = ['📦 JavDB 离线→gd6 流程 · ' + time.strftime('%Y-%m-%d %H:%M', time.localtime())]
    if run:
        status = run.get('status'); concl = run.get('conclusion')
        icon = {'success': '✅', 'failure': '❌', 'cancelled': '⏹'}.get(concl, '⏳' if status == 'in_progress' else '•')
        lines.append('%s 最近运行：%s %s' % (icon, status, ('/' + concl) if concl else ''))
        lines.append('   %s' % run.get('html_url', ''))
    lines.append('🔄 已提交离线：%d 个' % len(offlined))
    lines.append('📥 已下载并传 gd6：%d 个' % len(done))
    if total_links:
        try:
            remain = int(total_links) - len(done)
            lines.append('📊 进度：%d/%s（剩 %d）' % (len(done), total_links, max(0, remain)))
        except Exception:
            pass
    body = '\n'.join(lines)
    return body[:3900]


def send_report(text, with_button=True, token=None, chat=None):
    token = token or os.environ.get('TG_TOKEN', '')
    chat = chat or os.environ.get('TG_CHAT_ID', '')
    if not token or not chat:
        return {'ok': False, 'err': '缺 TG_TOKEN/TG_CHAT_ID'}
    payload = {'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true'}
    if with_button:
        kb = {'inline_keyboard': [[
            {'text': '⚡ 触发离线+搬运', 'callback_data': 'offline_rerun'},
            {'text': '🔄 刷新进度', 'callback_data': 'offline_refresh'},
        ]]}
        payload['reply_markup'] = json.dumps(kb)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request('https://api.telegram.org/bot%s/sendMessage' % token, data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except Exception as e:
        return {'ok': False, 'err': str(e)[:160]}


if __name__ == '__main__':
    print(build_report())
