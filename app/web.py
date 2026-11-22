"""Flask 网页：上传三份 CSV 核对，或按关联关系分包下载 ZIP。

输入有误时整次退回（核对不出结果、分包不出 ZIP），并定位文件与行号。
"""

from __future__ import annotations

import io
import os

from flask import Flask, render_template, request, send_file, send_from_directory

from .packaging import build_packages_zip, plan_packages
from .reconcile import InputError, parse_all, reconcile

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

UPLOAD_FIELDS = (
    ("grants", "拨款表"),
    ("attributions", "归属表"),
    ("jurisdictions", "归口表"),
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


@app.template_filter("fen")
def format_fen(value: int) -> str:
    """分 -> 元，千分位，保留两位小数。"""
    return f"{value / 100:,.2f}"


@app.errorhandler(413)
def too_large(_exc):
    return render_template(
        "error.html",
        errors=[(None, None, f"上传内容超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")],
    ), 413


def _read_uploads():
    """读取三份上传文件，返回 (files, missing)；files[field] = (文件名, 内容)。"""
    files = {}
    missing = []
    for field, label in UPLOAD_FIELDS:
        upload = request.files.get(field)
        if upload is None or upload.filename == "":
            missing.append((label, None, "未上传文件"))
            continue
        files[field] = (upload.filename, upload.read())
    return files, missing


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/check")
def check():
    files, missing = _read_uploads()
    if missing:
        return render_template("error.html", errors=missing), 400

    try:
        report = reconcile(
            files["grants"][1],
            files["attributions"][1],
            files["jurisdictions"][1],
        )
    except InputError as exc:
        # 本次核对整次退回，不落库、不产生部分结果
        return render_template("error.html", errors=exc.errors), 400

    return render_template(
        "report.html",
        report=report,
        file_names={
            "拨款表": files["grants"][0],
            "归属表": files["attributions"][0],
            "归口表": files["jurisdictions"][0],
        },
    )


@app.post("/split")
def split():
    """分包入口：同一批三份 CSV 拆成互不关联的包，下载 ZIP。"""
    files, missing = _read_uploads()
    if missing:
        return render_template(
            "error.html",
            errors=missing,
            heading="缺少上传文件，整批未出包",
            note="分包需要同时上传拨款表、归属表、归口表三份 CSV。请补齐后重新上传：",
        ), 400

    try:
        grants, attributions, jurisdictions = parse_all(
            files["grants"][1],
            files["attributions"][1],
            files["jurisdictions"][1],
        )
    except InputError as exc:
        # 任一文件有误：整批不出包，不产生任何部分结果
        return render_template(
            "error.html",
            errors=exc.errors,
            heading="输入有误，整批未出包",
            note="未生成任何分包文件。请修正下列问题后重新上传三份文件：",
        ), 400

    packages = plan_packages(grants, attributions, jurisdictions)
    zip_bytes = build_packages_zip(packages)
    return send_file(
        io.BytesIO(zip_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name="分包结果.zip",
    )


@app.get("/samples/<path:filename>")
def sample(filename: str):
    return send_from_directory(SAMPLE_DIR, filename, as_attachment=True)
