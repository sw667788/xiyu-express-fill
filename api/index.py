# -*- coding: utf-8 -*-
"""寄样快递费用回填 — Vercel (Flask) 后端

数据源：飞书 Bitable「稀榆快递费用登记」app 下「寄样快递费用表」。

Vercel Python(runtime) 对 Flask 的路径转发不可靠（rewrite 会改写发给
后端的 PATH_INFO，原路径可能丢失）。因此本项目采用「单一入口 + 参数分发」：

  GET  /            （rewrites → /api/index） 默认渲染页面
  GET  /?r=records  拉取全表记录（含待回填标记）
  GET  /?r=img&token=<file_token>   图片代理
  POST /  body={record_id,重量kg,费用元}   回填

前端全部用相对 URL（?r=...），浏览器地址栏始终是 /，query 不会被 rewrite 改动。

凭据从环境变量读取（Vercel Environment Variables）：
  FEISHU_APP_ID / FEISHU_APP_SECRET
"""
import os
import json
import urllib.parse
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, render_template, Response

APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "NrscbM8spadEPgsIE5VcntTHn5b")
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "tblcQoxrvAmzeqts")
FEISHU_BASE = "https://open.feishu.cn/open-apis"
_HERE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=os.path.join(_HERE, "templates"))

IMAGE_FIELDS = ["面单图片", "寄件图片"]


# ---------- 飞书底层 ----------
def _feishu_creds():
    aid = os.environ.get("FEISHU_APP_ID")
    sec = os.environ.get("FEISHU_APP_SECRET")
    if aid and sec:
        return aid, sec
    try:  # 本地开发回退
        cfg = json.load(open(os.path.expanduser("~/.openclaw/openclaw.json"), encoding="utf-8"))
        f = cfg["channels"]["feishu"]
        return f["appId"], f["appSecret"]
    except Exception:
        return None, None


def _http(method, url, token=None, payload=None, timeout=20):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except Exception:
            return {"code": e.code, "msg": body[:200]}


def _raw(url, token=None, timeout=20):
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "image/jpeg")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "text/plain"


def _tenant_token():
    aid, sec = _feishu_creds()
    if not aid or not sec:
        raise RuntimeError("缺少飞书凭据（FEISHU_APP_ID/SECRET 环境变量）")
    out = _http("POST", f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                payload={"app_id": aid, "app_secret": sec}, timeout=10)
    tok = out.get("tenant_access_token")
    if not tok:
        raise RuntimeError("获取 tenant_access_token 失败: " + json.dumps(out, ensure_ascii=False)[:200])
    return tok


def _text(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or "")
    return str(v)


def _num(v):
    try:
        if v is None or v == "":
            return None
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _images(v):
    out = []
    if isinstance(v, list):
        for it in v:
            if isinstance(it, dict) and it.get("file_token"):
                out.append({"token": it["file_token"], "name": it.get("name", "")})
    return out


def _read_all_records(token):
    records = []
    page_token = None
    while True:
        url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records?page_size=100"
        if page_token:
            url += "&page_token=" + urllib.parse.quote(page_token)
        out = _http("GET", url, token=token, timeout=20)
        if out.get("code") != 0:
            raise RuntimeError("拉取记录失败: " + json.dumps(out, ensure_ascii=False)[:200])
        for item in out.get("data", {}).get("items", []):
            if item.get("deleted"):
                continue
            fld = item.get("fields", {})
            weight = _num(fld.get("重量kg"))
            cost = _num(fld.get("费用元"))
            rec = {
                "record_id": item.get("record_id") or item.get("id"),
                "面单号": _text(fld.get("面单号")),
                "日期": fld.get("日期"),
                "收件人": _text(fld.get("收件人")),
                "收件电话": _text(fld.get("收件电话")),
                "收件公司": _text(fld.get("收件公司")),
                "收件地址": _text(fld.get("收件地址")),
                "备注": _text(fld.get("备注")),
                "安排人": _text(fld.get("安排人")),
                "时效": _text(fld.get("时效")),
                "寄件样品": _text(fld.get("寄件样品")),
                "重量kg": weight,
                "费用元": cost,
                "待回填": weight is None or cost is None,
            }
            for img_field in IMAGE_FIELDS:
                rec[img_field] = _images(fld.get(img_field))
            records.append(rec)
        if out.get("data", {}).get("has_more"):
            page_token = out["data"].get("page_token")
        else:
            break
    return records


# ---------- 统一入口 ----------
@app.route("/", methods=["GET", "POST"])
@app.route("/api/index", methods=["GET", "POST"])
def entry():
    try:
        if request.method == "POST":
            return _do_backfill()
        r = request.args.get("r", "page")
        if r == "records":
            return jsonify({"ok": True, "records": _read_all_records(_tenant_token())})
        if r == "img":
            return _do_img(request.args.get("token", ""))
        return render_template("index.html")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _do_backfill():
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    record_id = str(body.get("record_id") or "").strip()
    if not record_id:
        return jsonify({"ok": False, "error": "缺少 record_id"}), 400

    fields = {}
    w = body.get("重量kg")
    if w is not None and w != "":
        n = _num(w)
        if n is None or n < 0:
            return jsonify({"ok": False, "error": "重量kg 格式不对"}), 400
        fields["重量kg"] = n
    c = body.get("费用元")
    if c is not None and c != "":
        n = _num(c)
        if n is None or n < 0:
            return jsonify({"ok": False, "error": "费用元 格式不对"}), 400
        fields["费用元"] = n
    s = (body.get("时效") or "").strip()
    if s:
        if s not in ("普通", "加急"):
            return jsonify({"ok": False, "error": "时效只能是 普通 或 加急"}), 400
        fields["时效"] = s
    if not fields:
        return jsonify({"ok": False, "error": "请填写重量kg / 费用元 / 时效 至少一项"}), 400

    out = _http("PUT",
                f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/{record_id}",
                token=_tenant_token(), payload={"fields": fields}, timeout=20)
    if out.get("code") != 0:
        return jsonify({"ok": False, "error": "飞书更新失败: " + json.dumps(out, ensure_ascii=False)[:300]}), 500
    return jsonify({"ok": True, "updated": fields})


def _do_img(file_token):
    if not file_token:
        return jsonify({"ok": False, "error": "缺少 token"}), 400
    status, data, ctype = _raw(
        f"{FEISHU_BASE}/drive/v1/medias/{file_token}/download", token=_tenant_token(), timeout=20)
    if status != 200:
        return jsonify({"ok": False, "error": f"图片下载失败 http={status}"}), status
    return Response(data, content_type=ctype, headers={"Cache-Control": "public, max-age=3600"})


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (被模块级使用，但本地 __main__ 下保证)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8081")), debug=False)
