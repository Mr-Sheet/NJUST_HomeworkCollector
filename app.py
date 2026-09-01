# -*- coding: utf-8 -*-
"""
快点交作业 —— 基于 Flask 的班级作业收集系统
================================================
教师端（首页 /）：发布作业、查看作业列表、上传名单、提交进度、
                 下载全部作业 ZIP、切换开启/关闭状态、删除作业。
学生端（/upload/<作业ID>）：按正则规范提交文件，重复提交覆盖旧文件。
数据存储：tasks.json 保存作业信息，uploads/<作业ID>/ 保存提交的文件。

本地运行：python app.py
Vercel 部署：vercel.json 已将所有请求路由到本文件的 Flask 实例 `app`。
             Vercel 文件系统只读（仅 /tmp 可写），数据目录自动切换到 /tmp。
"""

import os
import re
import io
import json
import uuid
import shutil
import zipfile
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort, jsonify, session,
)

# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------

# 单文件大小上限（Vercel Serverless 对请求体还有约 4.5MB 的硬限制，见 README）
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB

# Vercel 环境下文件系统只读，只有 /tmp 可写；本地则直接写在项目根目录
IS_VERCEL = bool(os.environ.get("VERCEL"))
DATA_DIR = os.environ.get(
    "DATA_DIR",
    "/tmp/homework_collector" if IS_VERCEL else os.path.dirname(os.path.abspath(__file__)),
)
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")

app = Flask(__name__)
# SECRET_KEY 用于签名 session cookie（教师端登录状态）。
# 注意：必须是固定值——Vercel 多实例下随机值会导致登录状态随机失效。
# 生产环境请通过环境变量 SECRET_KEY 覆盖。
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "kuai-dian-jiao-zuo-ye-fixed-secret")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

PROJECT_NAME = "快点交作业"  # 站点名，模板统一引用

# 教师端访问密码（学生端 /upload/<id> 不需要密码）。
# 修改方式：设置环境变量 TEACHER_PASSWORD（Vercel 后台也支持），或直接改这里。
TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD", "123456")

# Vercel Serverless 对请求体约 4.5MB 硬限制；本地无此限制
MAX_UPLOAD_MB = 4 if IS_VERCEL else 100

# 无需登录即可访问的端点：学生上传页、静态资源、登录页本身
PUBLIC_ENDPOINTS = {"upload_page", "static", "login"}


# ---------------------------------------------------------------------------
# 数据读写：tasks.json
# ---------------------------------------------------------------------------

def load_tasks():
    """读取全部作业任务，文件不存在或损坏时返回空列表。"""
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_tasks(tasks):
    """保存全部作业任务到 tasks.json。"""
    os.makedirs(os.path.dirname(TASKS_FILE) or ".", exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def get_task(task_id):
    """按 ID 查找作业，找不到返回 None。"""
    for t in load_tasks():
        if t["id"] == task_id:
            return t
    return None


def task_upload_dir(task_id):
    """某个作业的上传目录：uploads/<作业ID>/"""
    return os.path.join(UPLOADS_DIR, task_id)


def list_submitted_filenames(task_id):
    """返回某作业已提交的全部文件名（不含路径），目录不存在则空列表。"""
    d = task_upload_dir(task_id)
    if not os.path.isdir(d):
        return []
    return [n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n))]


def count_submissions(task_id):
    """统计某作业已收到的文件数量（用于教师端展示）。"""
    return len(list_submitted_filenames(task_id))


def is_deadline_passed(task):
    """当前时间是否已超过截止时间（统一使用系统本地时间比较）。"""
    try:
        deadline = datetime.fromisoformat(task["deadline"])
    except (KeyError, ValueError):
        return False
    return datetime.now() > deadline


# ---------------------------------------------------------------------------
# 名单 → 提交进度：基于文件名子串匹配
# ---------------------------------------------------------------------------

def compute_roster_progress(task):
    """
    根据名单和已提交文件名，计算提交进度。
    匹配规则：名单条目按空白符（Tab/空格）拆成多个关键词（如"学号 姓名"），
    当所有关键词都出现在同一个已提交文件名中（大小写不敏感）时视为已提交。
    单关键词条目退化为子串匹配。
    返回 dict: total / submitted / missing(list) / submitted_names(list)
    没有名单时返回 None。
    """
    roster = task.get("roster") or []
    if not roster:
        return None
    files = list_submitted_filenames(task["id"])
    files_lower = [f.lower() for f in files]
    missing, submitted_names = [], []
    for entry in roster:
        # 拆分关键词：Tab、空格、全角空格
        tokens = [t for t in re.split(r"[\t ]+", entry.strip()) if t]
        if not tokens:
            continue
        tokens_lower = [t.lower() for t in tokens]
        # 该条目的所有关键词都出现在同一个文件名中 → 已提交
        hit = any(all(tok in fl for tok in tokens_lower) for fl in files_lower)
        (submitted_names if hit else missing).append(entry.strip())
    total = len([e for e in roster if e.strip()])
    return {
        "total": total,
        "submitted": len(submitted_names),
        "missing": missing,
        "submitted_names": submitted_names,
    }


# ---------------------------------------------------------------------------
# 教师端登录（学生端 /upload/<id> 完全公开，不受影响）
# ---------------------------------------------------------------------------

@app.before_request
def require_teacher_login():
    """教师端全站拦截：未登录时除白名单外的所有请求都跳转到登录页。"""
    # endpoint 为 None 通常是 404，交给错误页处理
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if session.get("teacher"):
        return None
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password") or ""
        if password == TEACHER_PASSWORD:
            session["teacher"] = True
            flash("欢迎回来～", "success")
            return redirect(url_for("index"))
        flash("密码不正确，请重试", "danger")
        return redirect(url_for("login"))
    return render_template("login.html", project_name=PROJECT_NAME)


@app.route("/logout")
def logout():
    session.clear()
    flash("已退出教师端", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# 教师端：首页（发布作业 + 作业列表）
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        pattern = (request.form.get("regex") or "").strip()
        deadline = (request.form.get("deadline") or "").strip()
        note = (request.form.get("note") or "").strip()

        # ---- 表单校验 ----
        if not name:
            flash("请填写作业名称", "danger")
            return redirect(url_for("index"))
        if not pattern:
            flash("请填写命名规范（正则表达式）", "danger")
            return redirect(url_for("index"))
        try:
            re.compile(pattern)
        except re.error as e:
            flash(f"正则表达式语法错误：{e}", "danger")
            return redirect(url_for("index"))
        if not deadline:
            flash("请选择截止时间", "danger")
            return redirect(url_for("index"))
        try:
            datetime.fromisoformat(deadline)
        except ValueError:
            flash("截止时间格式不正确", "danger")
            return redirect(url_for("index"))

        # ---- 创建作业 ----
        task = {
            "id": uuid.uuid4().hex[:12],            # 唯一作业 ID
            "name": name,
            "regex": pattern,
            "deadline": deadline,                    # ISO 格式：YYYY-MM-DDTHH:MM
            "status": "open",                        # open / closed
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,                            # 可选：给学生的示例/说明
            "roster": [],                            # 名单，默认空
        }
        tasks = load_tasks()
        tasks.append(task)
        save_tasks(tasks)

        upload_url = url_for("upload_page", task_id=task["id"], _external=True)
        flash(f"作业发布成功！学生端链接：{upload_url}", "success")
        return redirect(url_for("index"))

    # GET：渲染作业列表
    tasks = load_tasks()
    for t in tasks:
        t["expired"] = is_deadline_passed(t)
        t["file_count"] = count_submissions(t["id"])
        t["progress"] = compute_roster_progress(t)
    # 最新发布的排在最前
    tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return render_template("index.html", tasks=tasks, project_name=PROJECT_NAME)


@app.route("/task/<task_id>/roster", methods=["POST"])
def upload_roster(task_id):
    """为某作业上传/替换名单。支持直接粘贴文本或上传 .txt 文件。"""
    task = get_task(task_id)
    if task is None:
        abort(404)

    roster_text = ""
    # 优先读取上传的 txt 文件
    roster_file = request.files.get("roster_file")
    if roster_file and roster_file.filename:
        raw = roster_file.read()
        # 尝试 utf-8，失败回退 gbk（Windows 记事本常见）
        try:
            roster_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            roster_text = raw.decode("gbk", errors="replace")
    else:
        roster_text = request.form.get("roster") or ""

    # 解析：每行一条，去空、去重、去空行
    lines = []
    seen = set()
    for line in roster_text.splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        lines.append(s)

    if not lines:
        flash("名单为空，请粘贴或上传至少一条学生标识", "danger")
        return redirect(url_for("index") + f"#task-{task_id}")

    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["roster"] = lines
            save_tasks(tasks)
            flash(f"名单已更新（{len(lines)} 人）", "success")
            return redirect(url_for("index") + f"#task-{task_id}")
    abort(404)


@app.route("/task/<task_id>/progress")
def task_progress(task_id):
    """返回某作业的提交进度 JSON（供前端 AJAX 刷新或弹窗调用）。"""
    task = get_task(task_id)
    if task is None:
        return jsonify({"error": "not found"}), 404
    p = compute_roster_progress(task)
    if p is None:
        return jsonify({"has_roster": False, "file_count": count_submissions(task_id)})
    return jsonify({
        "has_roster": True,
        "total": p["total"],
        "submitted": p["submitted"],
        "missing": p["missing"],
        "submitted_names": p["submitted_names"],
        "file_count": count_submissions(task_id),
    })


@app.route("/task/<task_id>/toggle", methods=["POST"])
def toggle_task(task_id):
    """切换作业开启/关闭状态。"""
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "closed" if t["status"] == "open" else "open"
            save_tasks(tasks)
            state = "开启" if t["status"] == "open" else "关闭"
            flash(f"作业「{t['name']}」已切换为{state}状态", "success")
            return redirect(url_for("index") + f"#task-{task_id}")
    abort(404)


@app.route("/task/<task_id>/delete", methods=["POST"])
def delete_task(task_id):
    """删除作业及其全部上传文件（前端已有二次确认）。"""
    tasks = load_tasks()
    remaining = [t for t in tasks if t["id"] != task_id]
    if len(remaining) == len(tasks):
        abort(404)
    save_tasks(remaining)
    # 同步删除该作业的上传文件夹
    d = task_upload_dir(task_id)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    flash("作业已删除", "success")
    return redirect(url_for("index"))


@app.route("/task/<task_id>/download")
def download_task(task_id):
    """将该作业 uploads/<作业ID>/ 下的全部文件打包为 ZIP 下载。"""
    task = get_task(task_id)
    if task is None:
        abort(404)

    src_dir = task_upload_dir(task_id)
    if not os.path.isdir(src_dir) or not os.listdir(src_dir):
        flash("该作业还没有收到任何文件，无法下载", "warning")
        return redirect(url_for("index") + f"#task-{task_id}")

    # 在内存中打包为 ZIP（班级作业量级足够；避免临时文件在响应前被删除的问题）
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in sorted(os.listdir(src_dir)):
            filepath = os.path.join(src_dir, filename)
            if os.path.isfile(filepath):
                zf.write(filepath, arcname=filename)
    zip_buffer.seek(0)

    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{task['name']}_{task_id}.zip",
    )


# ---------------------------------------------------------------------------
# 学生端：上传页面
# ---------------------------------------------------------------------------

@app.route("/upload/<task_id>", methods=["GET", "POST"])
def upload_page(task_id):
    task = get_task(task_id)
    if task is None:
        abort(404)

    # 状态检查：关闭 → 已停止收集；超时 → 已截止
    if task["status"] != "open":
        return render_template("message.html", task=task,
                               message="该作业已停止收集",
                               extra="如有疑问，请联系学委",
                               reason="closed", project_name=PROJECT_NAME), 403
    if is_deadline_passed(task):
        return render_template("message.html", task=task,
                               message="该作业已截止，无法提交",
                               extra="如有疑问，请联系学委",
                               reason="expired", project_name=PROJECT_NAME), 403

    if request.method == "GET":
        return render_template("upload.html", task=task,
                               project_name=PROJECT_NAME, max_mb=MAX_UPLOAD_MB)

    # ---- POST：处理文件上传 ----
    # AJAX 模式：前端用 fetch + X-Requested-With 头，后端返回 JSON
    is_ajax = request.headers.get("X-Requested-With") == "fetch"
    file = request.files.get("file")

    def fail(msg):
        if is_ajax:
            return jsonify({"success": False, "message": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("upload_page", task_id=task_id))

    if file is None or not file.filename:
        return fail("请选择要上传的文件")

    # 去掉可能的路径部分，只保留文件名本身
    filename = os.path.basename(file.filename.replace("\\", "/"))

    # 检查文件是否为空（大小为 0）
    file.stream.seek(0, io.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return fail("文件为空，请选择有效文件")

    # 检查文件名是否完全匹配命名规范（fullmatch：从 ^ 到 $ 完全匹配）
    try:
        pattern = re.compile(task["regex"])
    except re.error:
        return fail("服务器端命名规范配置有误，请联系老师")

    if not pattern.fullmatch(filename):
        return fail("文件名不符合规范，请按格式重命名后提交")

    # 保存文件（同名直接覆盖，允许重复提交，以最后一次为准）
    dest_dir = task_upload_dir(task_id)
    os.makedirs(dest_dir, exist_ok=True)
    file.save(os.path.join(dest_dir, filename))

    if is_ajax:
        return jsonify({"success": True, "message": "提交成功！重复提交时以最后一次为准"})
    flash("提交成功！重复提交时以最后一次为准", "success")
    return redirect(url_for("upload_page", task_id=task_id))


# ---------------------------------------------------------------------------
# 错误页
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def page_404(e):
    return render_template("404.html", project_name=PROJECT_NAME), 404


@app.errorhandler(413)
def page_413(e):
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"success": False, "message": "文件太大，超过了服务器允许的大小"}), 400
    flash("文件太大，超过了服务器允许的大小", "danger")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    # 本地开发运行；Vercel 上由 Serverless 入口调用 `app` 对象，不走这里
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
