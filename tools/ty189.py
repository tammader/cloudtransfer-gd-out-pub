# -*- coding: utf-8 -*-
"""目标网盘(189 协议) TV 版 API 客户端 —— 自研上传通道, 关键在"超时 + 断点续传"。

复刻自 OpenList v4.2.6 的 drivers/189_tv（help.go / utils.go / driver.go）。
与 OpenList 的**唯一关键差异**：每次 PUT 都带 socket 超时。
OpenList 那条 PUT 走 base.HttpClient 没有超时 —— 189 侧一挂就是几分钟且不报错,
驱动的"续传"只在请求出错时才触发, 于是整轮卡死。
这里改成: PUT 超时/断开 -> 查 getUploadFileStatus 拿已传偏移 -> 从断点继续。
于是"每传一会儿就断"不再是灾难, 只是变慢。

协议要点（个人云 personal）:
  createUploadFile.action  (POST form: parentFolderId/fileName/size/md5/opertype=3/flag=1/resumePolicy=1/isLog=0)
    -> uploadFileId / fileUploadUrl / fileCommitUrl / fileDataExists
  PUT fileUploadUrl        (header: ResumePolicy=1 / Edrive-UploadFileId=<id>; body = 从偏移到末尾)
  getUploadFileStatus.action?uploadFileId=<id>&resumePolicy=1 -> dataSize + size(=已存)
  POST fileCommitUrl       (form: opertype=3 / resumePolicy=1 / uploadFileId=<id> / isLog=0)

家庭云 family (与个人云**不是同一套接口**, 只改 is_family 不够):
  listFiles        -> /family/file/listFiles.action       (query: familyId + folderId + orderBy/descending)
  createUploadFile -> /family/file/createFamilyFile.action(query: familyId/parentId/fileName/fileSize/fileMd5/resumePolicy)
  getStatus        -> /family/file/getFamilyFileStatus.action(query: familyId/uploadFileId/resumePolicy)
  PUT              -> header UploadFileId + FamilyId (个人云是 Edrive-UploadFileId)
  ⚠️ 家庭云的**根 folderId 是空串**, 个人云是 -11 —— 混用会列到错的目录(实测 2026-09-30)。

鉴权: getQrCode -> loginFamilyMerge 拿 sessionKey/sessionSecret, 之后所有请求走 HMAC-SHA1 会话签名;
      家庭云的请求要用返回里的 familySessionKey/familySessionSecret 签名。

命令行自测:
  python ty189.py <本地文件> <远端目录, 如 /<挂载名>/out2>
"""
import email.utils
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

API = "https://api.cloud.189.cn"
APP_KEY = "600100885"
APP_SEC = "fe5734c74c2f96a38157f420b32dc995"
TV_VERSION = "6.5.5"
CLIENT_TYPE = "FAMILY_TV"
TV_CHANNEL = "home02"
UA = "go-resty/2.16 (" + "https://github.com/go-resty/resty)"
PERSONAL_ROOT_ID = "-11"      # 个人云根目录 id
FAMILY_ROOT_ID = ""           # 家庭云根目录 id —— **空串**(不是 -11), 实测 2026-09-30
ROOT_ID = PERSONAL_ROOT_ID    # 兼容旧引用

# 单次 PUT 的 socket 超时(秒)。189 侧"静默挂住"通常几分钟 -> 到这个点就断开重来。
PUT_TIMEOUT = 60
# 单个文件上传的总时长上限(秒), 到点认输(避免把整轮预算吃光)
UPLOAD_MAX_SECS = 900
# 连续多少次 PUT "零进展"就认输
MAX_STRIKES = 4


def _urlpath(url):
    m = re.search(r"://[^/]+((/[^/\s?#]+)*)", url)
    return m.group(1) if m else ""


def _hmac_sha1_upper(secret, data):
    return hmac.new(secret.encode(), data.encode(), hashlib.sha1).hexdigest().upper()


def _json_or_xml(raw):
    """189 有的接口返回 JSON, 有的返回 XML; 都收。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        root = ET.fromstring(raw)
    except Exception:
        return {"_raw": raw[:300]}
    out = {}

    def walk(node):
        for ch in node:
            if list(ch):
                walk(ch)
            else:
                out[ch.tag] = (ch.text or "").strip()
    walk(root)
    return out


class _ProgressReader(object):
    """把本地文件当作请求体, 顺带记录已发字节(用于日志)。"""

    def __init__(self, fh, n):
        self.fh, self.n, self.sent = fh, n, 0

    def read(self, k=-1):
        b = self.fh.read(k)
        self.sent += len(b)
        return b


class Ty189(object):
    def __init__(self, access_token, family_id="", is_family=False, root_id=None):
        self.access_token = (access_token or "").strip()
        self.family_id = str(family_id or "")
        self.is_family = bool(is_family)
        # 家庭云根 = 空串, 个人云根 = -11; 不显式给就按模式取默认
        if root_id is None:
            root_id = FAMILY_ROOT_ID if self.is_family else PERSONAL_ROOT_ID
        self.root_id = str(root_id)
        self.session_key = ""
        self.session_secret = ""
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------- 签名 ----------
    def suffix(self):
        return {"clientType": CLIENT_TYPE, "version": TV_VERSION, "channelId": TV_CHANNEL,
                "clientSn": "unknown", "model": "PJX110", "osFamily": "Android",
                "osVersion": "35", "networkAccessMode": "WIFI", "telecomsOperator": "46011"}

    def appkey_headers(self, full_url, method):
        ts = int(time.time() * 1000)
        data = "AppKey=%s&Operate=%s&RequestURI=%s&Timestamp=%d" % (
            APP_KEY, method, _urlpath(full_url), ts)
        return {"Timestamp": str(ts), "X-Request-ID": str(uuid.uuid4()),
                "AppKey": APP_KEY, "AppSignature": _hmac_sha1_upper(APP_SEC, data)}

    def sess_headers(self, full_url, method):
        sk = self.session_key
        ss = self.session_secret
        if self.is_family:
            sk = getattr(self, "family_session_key", "") or sk
            ss = getattr(self, "family_session_secret", "") or ss
        date_gmt = email.utils.formatdate(time.time(), usegmt=True)
        data = "SessionKey=%s&Operate=%s&RequestURI=%s&Date=%s" % (
            sk, method, _urlpath(full_url), date_gmt)
        return {"Date": date_gmt, "SessionKey": sk, "X-Request-ID": str(uuid.uuid4()),
                "Signature": _hmac_sha1_upper(ss, data)}

    # ---------- 低层请求 ----------
    def _raw(self, method, url, headers, body, timeout, content_type=None):
        h = {"User-Agent": UA}
        h.update(headers or {})
        if content_type:
            h["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        with self.op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def _sess_url(self, url, params=None):
        q = dict(self.suffix())
        if params:
            q.update(params)
        sep = "&" if "?" in url else "?"
        return url + sep + urllib.parse.urlencode(q)

    def sess_request_raw(self, method, url, params=None, form=None, timeout=90, retry_login=True):
        """走会话签名的原始请求, 返回原始文本(可能是 JSON 也可能是 XML)。"""
        full = self._sess_url(url, params)
        headers = self.sess_headers(url, method)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        ct = "application/x-www-form-urlencoded" if body is not None else None
        raw = self._raw(method, full, headers, body, timeout, ct)
        low = raw.lower()
        if ("usersessionbo is null" in low or "invalidsessionkey" in low) and retry_login:
            self.login()
            return self.sess_request_raw(method, url, params, form, timeout, retry_login=False)
        return raw

    def request(self, method, url, params=None, form=None, timeout=90, retry_login=True):
        raw = self.sess_request_raw(method, url, params, form, timeout, retry_login)
        d = _json_or_xml(raw)
        if isinstance(d, dict) and ("res_code" in d):
            try:
                if int(d.get("res_code") or 0) != 0:
                    raise RuntimeError("189 接口错误 res_code=%s res_message=%s"
                                       % (d.get("res_code"), d.get("res_message")))
            except ValueError:
                pass
        return d

    # ---------- 登录 ----------
    def login(self):
        url = API + "/family/manage/loginFamilyMerge.action"
        full = self._sess_url(url, {"e189AccessToken": self.access_token})
        raw = self._raw("GET", full, self.appkey_headers(url, "GET"), None, 60)
        d = _json_or_xml(raw)
        if not isinstance(d, dict) or not d.get("sessionKey"):
            raise RuntimeError("189 登录失败: %s" % str(d)[:220])
        self.session_key = d.get("sessionKey") or ""
        self.session_secret = d.get("sessionSecret") or ""
        self.family_session_key = d.get("familySessionKey") or ""
        self.family_session_secret = d.get("familySessionSecret") or ""
        self.login_name = d.get("loginName") or ""
        return d

    # ---------- 目录 ----------
    def list_files(self, folder_id=None):
        """列目录。注意: 这个接口回的是 **XML**(<listFiles><fileList><folder>..<file>..), 不是 JSON。
        folder_id 省略时用 self.root_id(个人云 -11 / 家庭云 空串)。"""
        if folder_id is None:
            folder_id = self.root_id
        url = API + ("/family/file/listFiles.action" if self.is_family else "/listFiles.action")
        out, page = [], 1
        while page <= 200:
            p = {"folderId": str(folder_id), "fileType": "0", "mediaAttr": "0",
                 "iconOption": "5", "pageNum": str(page), "pageSize": "130"}
            if self.is_family:
                p.update({"familyId": self.family_id, "orderBy": "1", "descending": "0"})
            else:
                p.update({"recursive": "0", "orderBy": "filename", "descending": "0"})
            raw = self.sess_request_raw("GET", url, params=p)
            try:
                root = ET.fromstring(raw)
            except Exception:
                raise RuntimeError("listFiles 返回无法解析: %s" % (raw or "")[:200])
            cnt = int(root.findtext("./fileList/count") or 0)
            if cnt == 0:
                break
            folders = root.findall("./fileList/folder")
            files = root.findall("./fileList/file")
            for fo in folders:
                out.append({"id": (fo.findtext("id") or "").strip(),
                            "name": (fo.findtext("name") or "").strip(),
                            "is_dir": True, "size": 0})
            for fi in files:
                out.append({"id": (fi.findtext("id") or "").strip(),
                            "name": (fi.findtext("name") or "").strip(),
                            "is_dir": False,
                            "size": int(fi.findtext("size") or 0),
                            "md5": (fi.findtext("md5") or "").strip()})
            if len(folders) + len(files) < 130:
                break
            page += 1
        return out

    def resolve_dir(self, path, root_id=None, mount=None):
        """把 /<挂载名>/out2 这样的路径解析成 folderId(个人云从 -11 / 家庭云从 "" 逐级找)。

        路径第一段通常是挂载名, 端侧没有它 -> 传 mount 剥掉;
        没传 mount 时, 若第一段在根下找不到, 也当作挂载名跳过。
        """
        parts = [p for p in (path or "").split("/") if p]
        if mount and parts and parts[0] == str(mount).strip("/"):
            parts = parts[1:]
        cur = self.root_id if root_id is None else str(root_id)
        for i, name in enumerate(parts):
            hit = None
            for it in self.list_files(cur):
                if it["is_dir"] and it["name"] == name:
                    hit = it
                    break
            if not hit:
                if i == 0:
                    continue          # 第一段可能是挂载名, 端侧没有 -> 跳过
                raise RuntimeError("目录不存在: %s (在 %s 下没找到)" % (name, cur))
            cur = hit["id"]
        return cur

    # ---------- 上传 ----------
    def create_upload(self, parent_id, name, size, md5):
        if self.is_family:
            url = API + "/family/file/createFamilyFile.action"
            params = {"familyId": self.family_id, "parentId": str(parent_id),
                      "fileMd5": md5, "fileName": name, "fileSize": str(size),
                      "resumePolicy": "1"}
            return self.request("POST", url, params=params)
        url = API + "/createUploadFile.action"
        form = {"parentFolderId": str(parent_id), "fileName": name, "size": str(size),
                "md5": md5, "opertype": "3", "flag": "1", "resumePolicy": "1", "isLog": "0"}
        return self.request("POST", url, form=form)

    def upload_status(self, upload_file_id):
        if self.is_family:
            url = API + "/family/file/getFamilyFileStatus.action"
            params = {"uploadFileId": str(upload_file_id), "resumePolicy": "1",
                      "familyId": self.family_id}
        else:
            url = API + "/getUploadFileStatus.action"
            params = {"uploadFileId": str(upload_file_id), "resumePolicy": "1"}
        return self.request("GET", url, params=params, timeout=90)

    def commit(self, commit_url, upload_file_id):
        form = {"opertype": "3", "resumePolicy": "1",
                "uploadFileId": str(upload_file_id), "isLog": "0"}
        hdrs = {}
        if self.is_family:
            form = None
            hdrs = {"ResumePolicy": "1", "UploadFileId": str(upload_file_id),
                    "FamilyId": self.family_id}
        full = self._sess_url(commit_url)
        h = self.sess_headers(commit_url, "POST")
        h.update(hdrs)
        body = urllib.parse.urlencode(form).encode() if form else None
        raw = self._raw("POST", full, h, body, 120,
                        "application/x-www-form-urlencoded" if body else None)
        return _json_or_xml(raw)

    def put_range(self, put_url, local_path, offset, total, upload_file_id, timeout=PUT_TIMEOUT):
        """PUT [offset, total) 到 put_url；返回 (OK?, 已发字节, 错误文本)"""
        remain = total - offset
        fh = open(local_path, "rb")
        try:
            fh.seek(offset)
            body = _ProgressReader(fh, remain)
            full = self._sess_url(put_url)
            h = self.sess_headers(put_url, "PUT")
            h["ResumePolicy"] = "1"
            h["Expect"] = "100-continue"
            h["Content-Type"] = "application/octet-stream"
            h["Content-Length"] = str(remain)
            if self.is_family:
                h["UploadFileId"] = str(upload_file_id)
                h["FamilyId"] = self.family_id
            else:
                h["Edrive-UploadFileId"] = str(upload_file_id)
            req = urllib.request.Request(full, data=body, method="PUT", headers=h)
            try:
                with self.op.open(req, timeout=timeout) as r:
                    txt = r.read().decode("utf-8", "replace")
                return True, body.sent, txt[:160]
            except Exception as e:
                return False, body.sent, "%s: %s" % (type(e).__name__, str(e)[:110])
        finally:
            fh.close()

    def upload(self, local_path, parent_id, name=None, log=print, max_secs=UPLOAD_MAX_SECS):
        """带断点续传的上传。返回 (ok, 说明)。"""
        name = name or os.path.basename(local_path)
        size = os.path.getsize(local_path)
        log("   计算 md5 ...")
        md5 = ""
        h5 = hashlib.md5()
        with open(local_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h5.update(chunk)
        md5 = h5.hexdigest()

        st = self.create_upload(parent_id, name, size, md5)
        up_id = st.get("uploadFileId")
        put_url = st.get("fileUploadUrl")
        commit_url = st.get("fileCommitUrl")
        if not put_url or not up_id:
            return False, "createUploadFile 失败: %s" % str(st)[:200]
        if str(st.get("fileDataExists") or "0") == "1":
            self.commit(commit_url, up_id)
            return True, "秒传(fileDataExists=1)"

        pos, t0, strikes, attempts = 0, time.time(), 0, 0
        while pos < size:
            if time.time() - t0 > max_secs:
                return False, "总时长超 %.0f 分钟(已传 %d/%d)" % (max_secs / 60, pos, size)
            attempts += 1
            if attempts > 80:
                return False, "尝试次数过多(已传 %d/%d)" % (pos, size)
            ok, sent, err = self.put_range(put_url, local_path, pos, size, up_id)
            try:
                st2 = self.upload_status(up_id) or {}
            except Exception as e:
                st2 = {}
                log("   查状态失败: %s" % str(e)[:90])
            newpos = int(st2.get("dataSize") or 0) + int(st2.get("size") or 0)
            if newpos <= pos:
                # 上传这次没成功、且服务端记录也没前进 -> 记一次"零进展"
                newpos = max(newpos, pos + (sent if ok else 0))
            moved = newpos - pos
            pos = max(pos, newpos)
            pct = 100.0 * pos / max(size, 1)
            log("   %.1f%% (%s/%s) 本次发 %s%s%s"
                % (pct, _human(pos), _human(size), _human(sent),
                   "" if ok else " [断]", ("  错误:" + err) if err else ""))
            if moved <= 0:
                strikes += 1
                if strikes >= MAX_STRIKES:
                    return False, "连续 %d 次零进展, 放弃(已传 %d/%d)" % (strikes, pos, size)
                time.sleep(min(2 * strikes, 10))
            else:
                strikes = 0
        self.commit(commit_url, up_id)
        return True, "完成 %s" % _human(size)


def _human(b):
    b = int(b or 0)
    return ("%.0f MB" % (b / 1048576)) if b < 1024 ** 3 else ("%.2f GB" % (b / 1024 ** 3))


def open_account(storage_addition, is_family=False):
    """从 alist/openlist 存储的 addition(JSON 字符串或 dict) 造客户端"""
    a = storage_addition
    if isinstance(a, str):
        a = json.loads(a)
    return Ty189(a.get("access_token") or "", a.get("family_id") or "", is_family)


def _main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("用法: python ty189.py <本地文件> <远端目录>")
        return 1
    src = sys.argv[1]
    dst = sys.argv[2]
    if not os.path.exists(src):
        print("本地文件不存在:", src)
        return 1
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(here, "alist_storages_ty.json"), encoding="utf-8"))
    sts = cfg if isinstance(cfg, list) else (cfg.get("storages") or [])
    mount = "/" + dst.strip("/").split("/")[0]
    per = next((s for s in sts
                if (s.get("mount_path") or "").rstrip("/") == mount.rstrip("/")), None)
    if per is None:
        per = next((s for s in sts if "CloudTV" in (s.get("driver") or "")), None)
    if per is None:
        print("在 alist_storages_ty.json 里找不到对应存储")
        return 1
    c = open_account(per.get("addition"), is_family=False)
    print("登录 ...")
    d = c.login()
    print("  登录 OK: loginName=%s sessionKey=%s..." % (d.get("loginName"), (c.session_key or "")[:8]))
    fid = c.resolve_dir(dst, mount=mount)
    print("  目标目录 %s -> folderId=%s" % (dst, fid))
    ok, msg = c.upload(src, fid, log=lambda m: print(m, flush=True))
    print("上传结果: %s | %s" % ("成功" if ok else "失败", msg))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
