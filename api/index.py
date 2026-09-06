# -*- coding: utf-8 -*-
"""寄样快递费用回填 — Vercel (Flask) 后端

数据源：飞书 Bitable「稀榆快递费用登记」app 下「寄样快递费用表」。
功能：
  GET  /                页面（回填 UI）
  GET  /api/records     拉取全表记录（含待回填标记）
  POST /api/backfill    按 record_id 回填 重量kg / 费用元
  GET  /api/img/<token> 图片代理（飞书附件下载需带鉴权头，前端无法直连）

凭据从环境变量读取（Vercel Environment Variables）：
  FEISHU_APP_ID / FEISHU_APP_SECRET   （必填；本地缺失时回退读 ~/.openclaw/openclaw.json）
  FEISHU_APP_TOKEN / FEISHU_TABLE_ID  （可选，默认已指向寄样快递费用表）
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

# 待回填判定依据的两个字段
WEIGHT_FIELD = "重量kg"
COST_FIELD = "费用元"

# 展示字段（从飞书记录 fields 取出转文本）
SHOW_FIELDS = [
    "面单号", "日期", "收件人", "收件电话", "收件公司", "收件地址",
    "备注", "安排人", "时效", "寄件样品",
]
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
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body), e.headers
        except Exception:
            return e.code, {"code": e.code, "msg": body[:200]}, e.headers


def _raw(url, token=None, timeout=20):
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _tenant_token():
    aid, sec = _feishu_creds()
    if not aid or not sec:
        raise RuntimeError("缺少飞书凭据（FEISHU_APP_ID/SECRET 环境变量）")
    _, out, _ = _http("POST", f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
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
        f = float(v)
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def _images(v):
    """附件字段 -> [{token,name}]"""
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
        _, out, _ = _http("GET", url, token=token, timeout=20)
        if out.get("code") != 0:
            raise RuntimeError("拉取记录失败: " + json.dumps(out, ensure_ascii=False)[:200])
        for item in out.get("data", {}).get("items", []):
            if item.get("deleted"):
                continue
            fld = item.get("fields", {})
            rid = item.get("record_id") or item.get("id")
            weight = _num(fld.get(WEIGHT_FIELD))
            cost = _num(fld.get(COST_FIELD))
            rec = {
                "record_id": rid,
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


# ---------- 页面 ----------
@app.route("/")
def index():
    return render_template("index.html")


# ---------- API ----------
@app.route("/api/records")
def api_records():
    try:
        records = _read_all_records(_tenant_token())
        return jsonify({"ok": True, "records": records})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backfill", methods=["POST"])
def api_backfill():
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
        fields[WEIGHT_FIELD] = n
    c = body.get("费用元")
    if c is not None and c != "":
        n = _num(c)
        if n is None or n < 0:
            return jsonify({"ok": False, "error": "费用元 格式不对"}), 400
        fields[COST_FIELD] = n

    if not fields:
        return jsonify({"ok": False, "error": "请填写重量kg 或 费用元 至少一项"}), 400

    try:
        token = _tenant_token()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/{record_id}"
    _, out, _ = _http("PUT", url, token=token, payload={"fields": fields}, timeout=20)
    if out.get("code") != 0:
        return jsonify({"ok": False, "error": "飞书更新失败: " + json.dumps(out, ensure_ascii=False)[:300]}), 500
    return jsonify({"ok": True, "updated": fields})


@app.route("/api/img/<path:file_token>")
def api_img(file_token):
    """代理飞书附件下载（前端 img 无法带 Authorization 头）。"""
    try:
        token = _tenant_token()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    url = f"{FEISHU_BASE}/drive/v1/medias/{file_token}/download"
    status, data, headers = _raw(url, token=token, timeout=20)
    if status != 200:
        return jsonify({"ok": False, "error": f"图片下载失败 http={status}"}), status
    ctype = headers.get("Content-Type", "image/jpeg") if headers else "image/jpeg"
    return Response(data, content_type=ctype, headers={
        "Cache-Control": "public, max-age=3600",
    })


if __name__ == "__main__":
    # 本地开发：python api/index.py （默认 0.0.0.0:8081）
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8081")), debug=False)
