"""核对引擎与网页层自测脚本。

运行方式（隔离模式，避免从数据目录加载到同名模块）::

    python3 -I tests/test_reconcile.py

覆盖：
  1. 基础两层内连接 + 半开区间边界（含起日、不含止日）
  2. 漏计：归属层断裂 / 归口层断裂
  3. 多计：重叠归属、重叠归口、菱形连接（同一统计单位不同路径分别计入）
  4. 大量重复匹配：路径数爆炸（30 万级）下的线性性能与不枚举校验
  5. 输入校验：表头、列数、日期、金额、唯一性、有效期、空文件、NUL 字节
  6. Flask 端到端：上传成功、整次退回并带文件与行号、缺文件
  7. 分包：跨日期共用源行、间接共用同包、互不关联分开、空底表、
     各包重跑与整批一致（逐笔次数 / 多计证据 / 单位合计）
  8. 空底表解析：归属 / 归口表只有表头可解析，漏计断层判断保留
  9. 分包不展开完整匹配路径（48 万路径规模下线性性能）
 10. /split 端到端：ZIP 结构、清单、各包 CSV 可回传核对页重跑、出错整批不出包
"""

from __future__ import annotations

import io
import os
import sys
import time
import traceback
import zipfile
from datetime import date
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.packaging import (  # noqa: E402
    ATTRIBUTION_CSV_NAME,
    GRANT_CSV_NAME,
    JURISDICTION_CSV_NAME,
    MANIFEST_NAME,
    build_packages_zip,
    plan_packages,
)
from app.reconcile import (  # noqa: E402
    ATTRIBUTION_FILE,
    GRANT_FILE,
    InputError,
    JURISDICTION_FILE,
    parse_all,
    parse_attributions,
    parse_grants,
    parse_jurisdictions,
    reconcile,
)

FAILURES = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  PASS  {msg}")
    else:
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")


def csv_bytes(rows):
    buf = io.StringIO()
    for row in rows:
        buf.write(",".join(str(c) for c in row) + "\n")
    return buf.getvalue()


GRANT_HEADER = ["单号", "项目号", "拨款日", "金额_分"]
ATTR_HEADER = ["行号", "项目号", "学院码", "起日", "止日"]
JUR_HEADER = ["行号", "学院码", "统计单位", "起日", "止日"]


# --------------------------------------------------------------------------- #
# 1. 基础连接 + 边界 + 漏计 + 多计 + 菱形
# --------------------------------------------------------------------------- #
def test_basic():
    print("[1] 基础两层连接 / 漏计 / 多计 / 菱形")
    grants = csv_bytes([
        GRANT_HEADER,
        ["G1", "P1", "2026-03-15", 1000],          # 正常单路径 -> U1
        ["G2", "P2", "2026-03-15", 2000],          # 归口层断裂（C2 无归口，漏计）
        ["G3", "P3", "2026-03-15", 3000],          # 归属层断裂（无归属，漏计）
        ["G5", "P5", "2026-03-15", 5000],          # 跨学院：C4→U2、C5→U3，共 2 路径
        ["G6", "P6", "2026-03-15", 6000],          # 菱形但两条路径都到同一单位 U9
        ["G7", "P7", "2026-03-15", 7000],          # 1 归属 × 2 归口 = 2 路径，U4/U5
    ])
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [2, "P2", "C2", "2026-01-01", "2027-01-01"],   # C2 在归口表不存在
        [5, "P5", "C4", "2026-01-01", "2027-01-01"],
        [6, "P5", "C5", "2026-01-01", "2027-01-01"],
        [7, "P6", "C6", "2026-01-01", "2027-01-01"],
        [8, "P6", "C7", "2026-01-01", "2027-01-01"],
        [9, "P7", "C8", "2026-01-01", "2027-01-01"],
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [12, "C4", "U2", "2026-01-01", "2027-01-01"],
        [13, "C5", "U3", "2026-01-01", "2027-01-01"],
        [14, "C6", "U9", "2026-01-01", "2027-01-01"],
        [15, "C7", "U9", "2026-01-01", "2027-01-01"],
        [16, "C8", "U4", "2026-01-01", "2027-01-01"],
        [17, "C8", "U5", "2026-01-01", "2027-01-01"],
    ])
    report = reconcile(grants.encode(), attrs.encode(), jurs.encode())

    check(report.total_grants == 6, "拨款总数 6")
    check(report.normal_grants == 1, "正常笔数 1（G1）")

    missed = {m.grant_no: m for m in report.missed}
    check(set(missed) == {"G2", "G3"}, "漏计单号为 G2/G3")
    check(missed["G2"].broken_layer == "归口层" and missed["G2"].attribution_hits == 1,
          "G2 断在归口层，且命中 1 条归属")
    check(missed["G3"].broken_layer == "归属层", "G3 断在归属层")

    over = {o.grant_no: o for o in report.overcounted}
    check(set(over) == {"G5", "G6", "G7"}, "多计单号为 G5/G6/G7")
    check(over["G5"].path_count == 2, "G5 实际入表次数 2（跨学院各一条）")
    check(over["G5"].unit_counts == {"U2": 1, "U3": 1},
          "G5 单位分布 U2×1/U3×1（不同路径分别计入）")
    check(len(over["G5"].sample_paths) == 2, "G5 给出 2 条示例路径")
    sp = over["G5"].sample_paths
    check(all(p.attribution_line in (5, 6) and p.jurisdiction_line in (12, 13)
              for p in sp), "示例路径带归属/归口行号")
    check(over["G6"].path_count == 2 and over["G6"].unit_counts == {"U9": 2},
          "G6 两条不同路径汇入同一单位 U9，分别计入（2 次）")
    check(over["G7"].path_count == 2 and over["G7"].unit_counts == {"U4": 1, "U5": 1},
          "G7 一条归属对两条归口，2 条路径分别入 U4/U5")

    # 合计：G1=1000→U1×1；G5=5000→U2/U3 各 1；G6=6000→U9×2；G7=7000→U4/U5 各 1
    check(report.unit_path_counts == {"U1": 1, "U2": 1, "U3": 1, "U4": 1, "U5": 1, "U9": 2},
          "各单位实际入表次数正确")
    check(report.unit_totals ==
          {"U1": 1000, "U2": 5000, "U3": 5000, "U4": 7000, "U5": 7000, "U9": 12000},
          "各单位实际合计正确（多路径按路径倍乘）")
    check(report.grand_total == 37000, "总合计 37000 分")

    entry = {e.grant_no: e for e in report.entries}
    check([entry[g].path_count for g in ("G1", "G2", "G3", "G5", "G6", "G7")]
          == [1, 0, 0, 2, 2, 2], "每笔拨款实际入表次数与状态齐全")
    check([entry[g].status for g in ("G1", "G2", "G3", "G5", "G6", "G7")]
          == ["正常", "漏计", "漏计", "多计", "多计", "多计"], "每笔拨款状态正确")


# --------------------------------------------------------------------------- #
# 2. 半开区间边界（两层各自验证含起日、不含止日）
# --------------------------------------------------------------------------- #
def test_half_open_boundary():
    print("[1b] 半开区间 [起日, 止日)：起日当天命中、止日当天失效")
    # 第一层边界走 P1/C1；第二层边界走 P2/C2（归属始终有效，归口窗口短）
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-10", "2026-01-20"],
        [2, "P2", "C2", "2026-01-01", "2027-01-01"],
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [9, "C1", "U0", "2026-01-01", "2027-01-01"],   # C1 归口长期有效，不干扰第一层边界
        [10, "C2", "U1", "2026-01-10", "2026-01-20"],
    ])

    def run(day, project="P1"):
        g = csv_bytes([GRANT_HEADER, ["G1", project, day, 100]]).encode()
        return reconcile(g, attrs.encode(), jurs.encode())

    # 第一层：起日当天有效；止日当天失效
    check(run("2026-01-10").entries[0].path_count == 1, "归属起日当天（1-10）命中")
    check(run("2026-01-19").entries[0].path_count == 1, "归属止日前一天（1-19）命中")
    r = run("2026-01-20")
    check(r.entries[0].status == "漏计" and r.missed[0].broken_layer == "归属层",
          "归属止日当天（1-20）失效，断在归属层")

    # 第二层：起日当天有效；止日当天失效（归属始终有效）
    check(run("2026-01-10", "P2").entries[0].path_count == 1, "归口起日当天（1-10）命中")
    check(run("2026-01-19", "P2").entries[0].path_count == 1, "归口止日前一天（1-19）命中")
    r = run("2026-01-20", "P2")
    check(r.entries[0].status == "漏计" and r.missed[0].broken_layer == "归口层",
          "归口止日当天（1-20）失效，断在归口层")


# --------------------------------------------------------------------------- #
# 3. 重叠归属（同一项目多条并行有效归属行）
# --------------------------------------------------------------------------- #
def test_overlapping_attribution():
    print("[2] 重叠归属：同项目同时期多条归属行")
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-05-01", 100]])
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [2, "P1", "C1", "2026-03-01", "2027-01-01"],   # 与行 1 重叠、同学院
        [3, "P1", "C2", "2026-04-01", "2027-01-01"],   # 重叠、跨学院
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [10, "C1", "U1", "2026-01-01", "2027-01-01"],
        [20, "C2", "U2", "2026-01-01", "2027-01-01"],
    ])
    report = reconcile(grants.encode(), attrs.encode(), jurs.encode())
    over = report.overcounted[0]
    check(over.path_count == 3, "3 条并行归属 → 3 条路径")
    check(over.unit_counts == {"U1": 2, "U2": 1}, "同学院两条归属各成路径：U1×2, U2×1")
    check(report.unit_totals == {"U1": 200, "U2": 100}, "重叠归属按路径倍乘金额")
    check({p.attribution_line for p in over.sample_paths} == {1, 2},
          "多计两条示例路径标注不同归属行号")


# --------------------------------------------------------------------------- #
# 3. 大量重复匹配（路径数爆炸，验证不枚举 + 线性性能）
# --------------------------------------------------------------------------- #
def test_massive():
    print("[3] 大量重复匹配：600 归属 × 800 归口 = 480,000 条路径")
    n_attr, n_jur = 600, 800
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-06-01", 1000]])
    attr_rows = [ATTR_HEADER]
    for i in range(1, n_attr + 1):
        attr_rows.append([i, "P1", "C1", "2026-01-01", "2027-01-01"])
    jur_rows = [JUR_HEADER]
    for j in range(1, n_jur + 1):
        jur_rows.append([1000 + j, "C1", "U1", "2026-01-01", "2027-01-01"])

    started = time.perf_counter()
    report = reconcile(
        grants.encode(),
        csv_bytes(attr_rows).encode(),
        csv_bytes(jur_rows).encode(),
    )
    elapsed = time.perf_counter() - started

    expected = n_attr * n_jur
    check(report.overcounted[0].path_count == expected,
          f"实际入表次数 = {expected}（未枚举也能算出）")
    check(report.unit_path_counts == {"U1": expected}, "单位次数 48 万")
    check(report.unit_totals == {"U1": expected * 1000}, "单位合计 = 次数 × 金额")
    check(len(report.overcounted[0].sample_paths) == 2, "示例路径仍只取 2 条（有界）")
    check(elapsed < 2.0, f"耗时 {elapsed*1000:.1f}ms < 2s（与路径总数无关的线性复杂度）")
    print(f"       （480,000 路径，引擎实际构造的路径对象仅 2 个，用时 {elapsed*1000:.1f}ms）")


# --------------------------------------------------------------------------- #
# 4. 输入校验：错误必须带文件与行号，且整次退回
# --------------------------------------------------------------------------- #
def expect_error(grants, attrs, jurs, needle):
    try:
        reconcile(grants, attrs, jurs)
    except InputError as exc:
        text = " | ".join(InputError.format(e) for e in exc.errors)
        check(needle in text, f"报错包含 [{needle}]；实际：{text}")
        return exc
    FAILURES.append(f"应报错但通过了：{needle}")
    print(f"  FAIL  应报错但通过了：{needle}")
    return None


def test_validation():
    print("[4] 输入校验（文件 + 行号定位，整次退回）")
    good_g = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 100]]).encode()
    good_a = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]]).encode()
    good_j = csv_bytes([JUR_HEADER, [1, "C1", "U1", "2026-01-01", "2027-01-01"]]).encode()

    # 表头错误（定位到第 1 行）
    bad_header = csv_bytes([["单号", "项目号", "拨款日", "金额"]]).encode()
    expect_error(bad_header, good_a, good_j, f"{GRANT_FILE} 第1行")

    # 列数错误（定位物理行号 3）
    bad_cols = csv_bytes([
        GRANT_HEADER,
        ["G1", "P1", "2026-03-15", 100],
        ["G2", "P2", "2026-03-15"],
    ]).encode()
    expect_error(bad_cols, good_a, good_j, f"{GRANT_FILE} 第3行")

    # 日期格式
    bad_date = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026/03/15", 100]]).encode()
    expect_error(bad_date, good_a, good_j, f"{GRANT_FILE} 第2行")

    # 金额非整数 / 负数
    bad_amount = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", "12.5"]]).encode()
    expect_error(bad_amount, good_a, good_j, f"{GRANT_FILE} 第2行")
    neg_amount = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", -1]]).encode()
    expect_error(neg_amount, good_a, good_j, f"{GRANT_FILE} 第2行")

    # 单号重复（第二次出现的物理行号）
    dup_grant = csv_bytes([
        GRANT_HEADER,
        ["G1", "P1", "2026-03-15", 100],
        ["G1", "P2", "2026-03-15", 200],
    ]).encode()
    expect_error(dup_grant, good_a, good_j, f"{GRANT_FILE} 第3行")

    # 归属表行号重复
    dup_attr = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2027-01-01"],
        [1, "P2", "C2", "2026-01-01", "2027-01-01"],
    ]).encode()
    expect_error(good_g, dup_attr, good_j, f"{ATTRIBUTION_FILE} 第3行")

    # 归口表有效期非法（起日 >= 止日）
    bad_interval = csv_bytes([
        JUR_HEADER, [1, "C1", "U1", "2026-01-01", "2026-01-01"],
    ]).encode()
    expect_error(good_g, good_a, bad_interval, f"{JURISDICTION_FILE} 第2行")

    # 空字段
    empty_field = csv_bytes([GRANT_HEADER, ["G1", "", "2026-03-15", 100]]).encode()
    expect_error(empty_field, good_a, good_j, f"{GRANT_FILE} 第2行")

    # 空文件 / NUL 字节（只定位到文件）
    expect_error(b"", good_a, good_j, GRANT_FILE)
    expect_error(b"header\x00\n", good_a, good_j, GRANT_FILE)

    # 多文件同时有错：错误一次性全部返回，且都带文件名
    exc = expect_error(bad_date, dup_attr, bad_interval, GRANT_FILE)
    if exc is not None:
        labels = {e[0] for e in exc.errors}
        check(labels == {GRANT_FILE, ATTRIBUTION_FILE, JURISDICTION_FILE},
              "三个文件的错误一次性全部返回（整次退回，不留部分结果）")

    # 外键不存在不算输入错误，按漏计处理
    orphan = csv_bytes([GRANT_HEADER, ["G1", "PX", "2026-03-15", 100]]).encode()
    report = reconcile(orphan, good_a, good_j)
    check(report.missed[0].broken_layer == "归属层", "项目号在归属表不存在 → 归属层漏计而非报错")


# --------------------------------------------------------------------------- #
# 5. Flask 端到端
# --------------------------------------------------------------------------- #
def test_web():
    print("[5] Flask 端到端（上传成功 / 整次退回 / 缺文件）")
    try:
        from app.web import app
    except ImportError:
        print("  SKIP 未安装 Flask，跳过网页层测试")
        return

    client = app.test_client()
    sample_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "app", "samples")
    with open(os.path.join(sample_dir, "grants.csv"), "rb") as f1, \
         open(os.path.join(sample_dir, "attributions.csv"), "rb") as f2, \
         open(os.path.join(sample_dir, "jurisdictions.csv"), "rb") as f3:
        resp = client.post(
            "/check",
            data={
                "grants": (io.BytesIO(f1.read()), "grants.csv"),
                "attributions": (io.BytesIO(f2.read()), "attributions.csv"),
                "jurisdictions": (io.BytesIO(f3.read()), "jurisdictions.csv"),
            },
            content_type="multipart/form-data",
        )
    check(resp.status_code == 200, "样例三表上传返回 200")
    body = resp.get_data(as_text=True)
    for needle in ("各统计单位实际合计", "漏计单号", "多计单号", "G003", "G001", "行号 201"):
        check(needle in body, f"结果页包含 {needle}")

    # 错误输入：HTTP 400 + 文件行号定位 + 退回提示
    bad = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", "abc"]]).encode()
    with open(os.path.join(sample_dir, "attributions.csv"), "rb") as f2, \
         open(os.path.join(sample_dir, "jurisdictions.csv"), "rb") as f3:
        resp = client.post(
            "/check",
            data={
                "grants": (io.BytesIO(bad), "bad.csv"),
                "attributions": (io.BytesIO(f2.read()), "attributions.csv"),
                "jurisdictions": (io.BytesIO(f3.read()), "jurisdictions.csv"),
            },
            content_type="multipart/form-data",
        )
    check(resp.status_code == 400, "错误输入返回 400")
    text = resp.get_data(as_text=True)
    check("本次核对已整次退回" in text and "拨款表" in text and "第 2 行" in text,
          "错误页提示整次退回并定位到拨款表第 2 行")

    # 缺文件
    resp = client.post("/check", data={}, content_type="multipart/form-data")
    check(resp.status_code == 400 and "未上传文件" in resp.get_data(as_text=True),
          "缺少上传文件时 400 并提示")

    # 首页与样例下载
    check(client.get("/").status_code == 200, "首页 200")
    check(client.get("/samples/grants.csv").status_code == 200, "样例下载 200")


# --------------------------------------------------------------------------- #
# 7. 分包：跨日期共用源行 / 间接共用 / 互不关联 / 空底表 / 各包重跑一致
# --------------------------------------------------------------------------- #
def _packaging_fixture():
    """构造覆盖全部分包规则的一批数据（返回三份 CSV 文本）。"""
    grants = csv_bytes([
        GRANT_HEADER,
        ["GA1", "P1", "2026-02-01", 100],   # 与 GA2 跨日期共用归属行 1、归口行 11
        ["GA2", "P1", "2026-06-01", 200],   # 额外命中归口行 16 → 多计 ×2
        ["GB1", "P2", "2026-03-01", 300],   # 与 GB2 跨日期共用归属行 2（归口行不同）
        ["GB2", "P2", "2026-09-01", 400],   # 命中归口行 13（与 GB1 的 12 不重叠）
        ["GB3", "P3", "2026-03-01", 500],   # 与 GB1 共用归口行 12 → 间接与 GB2 同包
        ["GB4", "P4", "2026-03-01", 600],   # 与 GB 组互不关联 → 单独一包
        ["GC1", "PX", "2026-03-01", 700],   # 归属层断裂 → 空底表包
        ["GD1", "P5", "2026-03-01", 800],   # 归口层断裂 → 归口表只有表头
        ["GE1", "P6", "2026-03-01", 900],   # 独立正常
    ])
    attrs = csv_bytes([
        ATTR_HEADER,
        [1, "P1", "C1", "2026-01-01", "2026-12-31"],
        [2, "P2", "C2", "2026-01-01", "2026-12-31"],
        [3, "P3", "C2", "2026-01-01", "2026-12-31"],
        [4, "P4", "C3", "2026-01-01", "2026-12-31"],
        [5, "P5", "C9", "2026-01-01", "2026-12-31"],   # C9 无归口行
        [6, "P6", "C5", "2026-01-01", "2026-12-31"],
    ])
    jurs = csv_bytes([
        JUR_HEADER,
        [11, "C1", "U1", "2026-01-01", "2026-12-31"],
        [16, "C1", "U1x", "2026-03-01", "2026-09-01"],  # 只覆盖 GA2 的拨款日
        [12, "C2", "U2", "2026-01-01", "2026-07-01"],
        [13, "C2", "U2", "2026-07-01", "2027-01-01"],
        [14, "C3", "U3", "2026-01-01", "2026-12-31"],
        [15, "C5", "U5", "2026-01-01", "2026-12-31"],
    ])
    return grants.encode(), attrs.encode(), jurs.encode()


def _summarize(report):
    """提取报告的可比部分：逐笔 (次数, 状态)、漏计断层、多计证据、单位合计。"""
    entries = {e.grant_no: (e.path_count, e.status) for e in report.entries}
    missed = {m.grant_no: (m.broken_layer, m.attribution_hits) for m in report.missed}
    over = {
        o.grant_no: (
            o.path_count,
            dict(o.unit_counts),
            sorted((p.attribution_line, p.jurisdiction_line) for p in o.sample_paths),
        )
        for o in report.overcounted
    }
    return entries, missed, over, dict(report.unit_totals), report.grand_total


def test_packaging_plan():
    print("[7] 分包：跨日期共用 / 间接共用 / 互不关联 / 空底表 / 重跑一致")
    grants_raw, attrs_raw, jurs_raw = _packaging_fixture()
    grants, attributions, jurisdictions = parse_all(grants_raw, attrs_raw, jurs_raw)
    packages = plan_packages(grants, attributions, jurisdictions)

    by_grant = {}
    for pkg in packages:
        for g in pkg.grants:
            by_grant[g.grant_no] = pkg
    check(len(packages) == 6, f"共分 6 包（实际 {len(packages)}）")

    # 每笔拨款只进一包
    all_grants = [g.grant_no for pkg in packages for g in pkg.grants]
    check(sorted(all_grants) == sorted(by_grant), "每笔拨款只进一包（无遗漏无重复）")

    # 跨日期共用源行：GA1(02-01) 与 GA2(06-01) 共用归属行 1 / 归口行 11
    check(by_grant["GA1"] is by_grant["GA2"], "跨日期共用源行的拨款同包（GA1/GA2）")
    # GB1(03-01) 与 GB2(09-01) 共用归属行 2，但命中不同归口行（12/13 互不重叠）
    check(by_grant["GB1"] is by_grant["GB2"], "跨日期共用归属行、归口行不重叠也同包（GB1/GB2）")
    # 间接共用：GB2 与 GB3 不直接共用任何源行，经 GB1 传递同包
    pkg_b = by_grant["GB1"]
    check(by_grant["GB3"] is pkg_b and by_grant["GB2"] is pkg_b,
          "间接共用（GB2—GB1—GB3）传递同包")
    check({g.grant_no for g in pkg_b.grants} == {"GB1", "GB2", "GB3"},
          "GB 包恰好含 GB1/GB2/GB3")
    # 互不关联分开
    check(by_grant["GB4"] is not pkg_b, "互不关联的拨款分开（GB4 不在 GB 包）")
    check(by_grant["GE1"] is not pkg_b and by_grant["GB4"] is not by_grant["GE1"],
          "GE1/GB4 各自独立成包")

    # 包内容：带上拨款日命中的全部源行
    pkg_a = by_grant["GA1"]
    check({r.line_no for r in pkg_a.attributions} == {1}, "GA 包含归属行 1")
    check({r.line_no for r in pkg_a.jurisdictions} == {11, 16},
          "GA 包含归口行 11 和 16（16 只有 GA2 命中，也带入包内）")
    check({r.line_no for r in pkg_b.attributions} == {2, 3}, "GB 包含归属行 2、3")
    check({r.line_no for r in pkg_b.jurisdictions} == {12, 13}, "GB 包含归口行 12、13")

    # 空底表：GC1 归属层断裂 → 两张底表都空；GD1 归口层断裂 → 归口表空
    pkg_c = by_grant["GC1"]
    check(not pkg_c.attributions and not pkg_c.jurisdictions,
          "GC1 自成一包，归属表 / 归口表都只有表头")
    pkg_d = by_grant["GD1"]
    check([r.line_no for r in pkg_d.attributions] == [5] and not pkg_d.jurisdictions,
          "GD1 包带归属行 5，归口表只有表头")

    # 各包重跑与整批一致：逐笔次数 / 漏计断层 / 多计证据 / 单位合计
    full = _summarize(reconcile(grants_raw, attrs_raw, jurs_raw))
    sum_entries, sum_missed, sum_over = {}, {}, {}
    sum_unit: Dict[str, int] = {}
    sum_grand = 0
    for pkg in packages:
        sub = reconcile(
            pkg_grants_bytes(pkg), pkg_attrs_bytes(pkg), pkg_jurs_bytes(pkg)
        )
        entries, missed, over, unit_totals, grand = _summarize(sub)
        sum_entries.update(entries)
        sum_missed.update(missed)
        sum_over.update(over)
        for unit, total in unit_totals.items():
            sum_unit[unit] = sum_unit.get(unit, 0) + total
        sum_grand += grand
    check(sum_entries == full[0], "各包重跑：逐笔次数与状态与整批一致")
    check(sum_missed == full[1], "各包重跑：漏计断层（归属层/归口层）与整批一致")
    check(sum_over == full[2], "各包重跑：多计证据（次数/单位分布/示例路径）与整批一致")
    check(sum_unit == full[3], "各包单位金额相加与整批一致")
    check(sum_grand == full[4] == 3200, f"各包总合计相加 = 整批 = 3200 分（实际 {sum_grand}）")
    # GA2 多计证据具体值
    check(full[2]["GA2"][0] == 2 and full[2]["GA2"][1] == {"U1": 1, "U1x": 1},
          "GA2 多计 ×2，单位分布 U1×1 / U1x×1")
    # GA1 不受 GA2 带入的归口行 16 影响（16 对 GA1 的拨款日无效）
    check(full[0]["GA1"] == (1, "正常"), "GA1 仍恰好 1 条路径（包内其他拨款的源行不外溢）")


def pkg_grants_bytes(pkg):
    from app.packaging import grants_csv_bytes
    return grants_csv_bytes(pkg.grants)


def pkg_attrs_bytes(pkg):
    from app.packaging import attributions_csv_bytes
    return attributions_csv_bytes(pkg.attributions)


def pkg_jurs_bytes(pkg):
    from app.packaging import jurisdictions_csv_bytes
    return jurisdictions_csv_bytes(pkg.jurisdictions)


# --------------------------------------------------------------------------- #
# 8. 空底表：只有表头的归属 / 归口表可解析，漏计断层判断保留
# --------------------------------------------------------------------------- #
def test_empty_base_tables():
    print("[8] 空底表（只有表头）解析与漏计断层")
    header_only_attr = csv_bytes([ATTR_HEADER]).encode()
    header_only_jur = csv_bytes([JUR_HEADER]).encode()
    header_only_grant = csv_bytes([GRANT_HEADER]).encode()

    check(parse_attributions(header_only_attr) == [], "归属表只有表头 → 空列表")
    check(parse_jurisdictions(header_only_jur) == [], "归口表只有表头 → 空列表")
    try:
        parse_grants(header_only_grant)
        check(False, "拨款表只有表头应报错")
    except InputError as exc:
        check("只有表头" in str(exc), "拨款表只有表头仍是输入错误")

    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", 100]]).encode()
    attrs = csv_bytes([ATTR_HEADER, [1, "P1", "C1", "2026-01-01", "2027-01-01"]]).encode()

    # 两张底表都空 → 归属层断裂
    report = reconcile(grants, header_only_attr, header_only_jur)
    check(report.missed[0].broken_layer == "归属层", "空归属表 + 空归口表 → 归属层漏计")
    check(report.entries[0].path_count == 0 and not report.unit_totals,
          "空底表无任何入表次数与单位合计")

    # 归属有行、归口空 → 归口层断裂，且注明命中归属行数
    report = reconcile(grants, attrs, header_only_jur)
    check(report.missed[0].broken_layer == "归口层"
          and report.missed[0].attribution_hits == 1,
          "空归口表 → 归口层漏计，命中归属行数保留")

    # 完全空文件（连表头都没有）对底表仍是错误
    expect_error(grants, b"", header_only_jur, ATTRIBUTION_FILE)
    expect_error(grants, header_only_attr, b"", JURISDICTION_FILE)


# --------------------------------------------------------------------------- #
# 9. 分包不展开完整匹配路径（路径数爆炸下的线性性能）
# --------------------------------------------------------------------------- #
def test_packaging_no_path_expansion():
    print("[9] 分包不展开完整匹配路径：600 归属 × 800 归口 = 480,000 条路径")
    n_attr, n_jur = 600, 800
    grants = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-06-01", 1000]])
    attr_rows = [ATTR_HEADER]
    for i in range(1, n_attr + 1):
        attr_rows.append([i, "P1", "C1", "2026-01-01", "2027-01-01"])
    jur_rows = [JUR_HEADER]
    for j in range(1, n_jur + 1):
        jur_rows.append([1000 + j, "C1", "U1", "2026-01-01", "2027-01-01"])

    started = time.perf_counter()
    parsed = parse_all(grants.encode(), csv_bytes(attr_rows).encode(),
                       csv_bytes(jur_rows).encode())
    packages = plan_packages(*parsed)
    zip_bytes = build_packages_zip(packages)
    elapsed = time.perf_counter() - started

    check(len(packages) == 1, "全部源行被同一笔拨款命中 → 1 包")
    pkg = packages[0]
    check(len(pkg.attributions) == n_attr and len(pkg.jurisdictions) == n_jur,
          f"包内为 {n_attr} 归属行 + {n_jur} 归口行（源行本身，非 48 万条路径）")
    check(elapsed < 2.0, f"分包 + 打 ZIP 耗时 {elapsed*1000:.1f}ms < 2s（线性复杂度）")
    # ZIP 内归口表 CSV 行数 = 表头 + 800，而不是路径数
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        jur_csv = zf.read(f"{pkg.folder}/{JURISDICTION_CSV_NAME}").decode("utf-8-sig")
    check(len(jur_csv.strip().splitlines()) == n_jur + 1,
          "ZIP 内归口表只有源行行数（未展开路径）")
    print(f"       （480,000 条潜在路径，分包仅处理 {n_attr + n_jur} 条源行，"
          f"用时 {elapsed*1000:.1f}ms）")


# --------------------------------------------------------------------------- #
# 10. /split 端到端：ZIP 结构、清单、各包回传核对页重跑、出错整批不出包
# --------------------------------------------------------------------------- #
def test_split_web():
    print("[10] /split 端到端（ZIP 结构 / 各包重跑一致 / 整批不出包）")
    try:
        from app.web import app
    except ImportError:
        print("  SKIP 未安装 Flask，跳过网页层测试")
        return

    client = app.test_client()
    sample_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "app", "samples")
    with open(os.path.join(sample_dir, "grants.csv"), "rb") as f1, \
         open(os.path.join(sample_dir, "attributions.csv"), "rb") as f2, \
         open(os.path.join(sample_dir, "jurisdictions.csv"), "rb") as f3:
        raw = (f1.read(), f2.read(), f3.read())

    resp = client.post(
        "/split",
        data={
            "grants": (io.BytesIO(raw[0]), "grants.csv"),
            "attributions": (io.BytesIO(raw[1]), "attributions.csv"),
            "jurisdictions": (io.BytesIO(raw[2]), "jurisdictions.csv"),
        },
        content_type="multipart/form-data",
    )
    check(resp.status_code == 200, "样例三表分包返回 200")
    check(resp.mimetype == "application/zip", "响应为 application/zip")
    disposition = resp.headers.get("Content-Disposition", "")
    check("attachment" in disposition and "filename" in disposition,
          "响应以附件形式下载（分包结果.zip）")

    with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
        names = zf.namelist()
        folders = sorted(n.split("/")[0] for n in names if "/" in n)
        check(folders == ["第01包"] * 3 + ["第02包"] * 3 + ["第03包"] * 3 + ["第04包"] * 3,
              f"样例拆成 4 包，每包一个文件夹（实际 {sorted(set(folders))}）")
        check(MANIFEST_NAME in names, "ZIP 根目录含分包清单.csv")
        per_pkg = {}
        for folder in sorted(set(folders)):
            trio = {n.split("/")[1] for n in names if n.startswith(folder + "/")}
            check(trio == {GRANT_CSV_NAME, ATTRIBUTION_CSV_NAME, JURISDICTION_CSV_NAME},
                  f"{folder} 各含拨款表 / 归属表 / 归口表三份 CSV")
            per_pkg[folder] = (
                zf.read(f"{folder}/{GRANT_CSV_NAME}"),
                zf.read(f"{folder}/{ATTRIBUTION_CSV_NAME}"),
                zf.read(f"{folder}/{JURISDICTION_CSV_NAME}"),
            )
        manifest = zf.read(MANIFEST_NAME).decode("utf-8-sig")

    # CSV 带 BOM（Excel 直开不乱码），且核对页解析兼容
    check(per_pkg["第01包"][0].startswith(b"\xef\xbb\xbf"), "包内 CSV 为 UTF-8 带 BOM")

    # 分包归属：G001/G002/G003/G006 共用归口行 901/903 → 第01包；
    # G004、G005 归属层断裂各自空底表单列；G007 归口层断裂单列
    def pkg_grant_nos(folder):
        grants, _, _ = parse_all(*per_pkg[folder])
        return sorted(g.grant_no for g in grants)

    check(pkg_grant_nos("第01包") == ["G001", "G002", "G003", "G006"],
          "第01包为共用源行的 G001/G002/G003/G006")
    check(pkg_grant_nos("第02包") == ["G004"], "第02包为 G004（归属层断裂单列）")
    check(pkg_grant_nos("第03包") == ["G005"], "第03包为 G005（归属层断裂单列）")
    check(pkg_grant_nos("第04包") == ["G007"], "第04包为 G007（归口层断裂单列）")

    # 空底表保留：G004 包两张底表只有表头；G007 包归口表只有表头
    _, attr2, jur2 = per_pkg["第02包"]
    check(len(attr2.decode("utf-8-sig").strip().splitlines()) == 1
          and len(jur2.decode("utf-8-sig").strip().splitlines()) == 1,
          "G004 包的归属表 / 归口表只有表头（空底表保留）")
    _, _, jur4 = per_pkg["第04包"]
    check(len(jur4.decode("utf-8-sig").strip().splitlines()) == 1,
          "G007 包的归口表只有表头")

    # 各包重跑（直接走引擎，模拟各小组在核对页单独上传）与整批一致
    full = _summarize(reconcile(*raw))
    sum_entries, sum_missed, sum_over = {}, {}, {}
    sum_unit: Dict[str, int] = {}
    sum_grand = 0
    for folder in sorted(per_pkg):
        entries, missed, over, unit_totals, grand = _summarize(
            reconcile(*per_pkg[folder])
        )
        sum_entries.update(entries)
        sum_missed.update(missed)
        sum_over.update(over)
        for unit, total in unit_totals.items():
            sum_unit[unit] = sum_unit.get(unit, 0) + total
        sum_grand += grand
    check(sum_entries == full[0], "样例各包重跑：逐笔次数与整批一致")
    check(sum_missed == full[1], "样例各包重跑：漏计断层与整批一致")
    check(sum_over == full[2], "样例各包重跑：多计证据与整批一致")
    check(sum_unit == full[3], "样例各包单位金额相加与整批一致")
    check(sum_grand == 3800000, f"样例各包总合计相加 = 3,800,000 分（实际 {sum_grand}）")

    # 分包清单：每笔拨款一行，包名与文件夹一致
    lines = manifest.strip().splitlines()
    check(lines[0] == "包,单号,项目号,拨款日,金额_分", "分包清单表头正确")
    check(len(lines) == 8, "分包清单 7 笔拨款各一行")
    check(any(line.startswith("第01包,G001,") for line in lines)
          and any(line.startswith("第04包,G007,") for line in lines),
          "分包清单包名与文件夹对应")

    # 各包 CSV 回传核对页（真实 HTTP 上传）也能跑
    resp2 = client.post(
        "/check",
        data={
            "grants": (io.BytesIO(per_pkg["第01包"][0]), "拨款表.csv"),
            "attributions": (io.BytesIO(per_pkg["第01包"][1]), "归属表.csv"),
            "jurisdictions": (io.BytesIO(per_pkg["第01包"][2]), "归口表.csv"),
        },
        content_type="multipart/form-data",
    )
    check(resp2.status_code == 200 and "G001" in resp2.get_data(as_text=True),
          "第01包三份 CSV 可直接在核对页重跑")

    # 任一上传文件有误 → 整批不出包
    bad = csv_bytes([GRANT_HEADER, ["G1", "P1", "2026-03-15", "abc"]]).encode()
    resp = client.post(
        "/split",
        data={
            "grants": (io.BytesIO(bad), "bad.csv"),
            "attributions": (io.BytesIO(raw[1]), "attributions.csv"),
            "jurisdictions": (io.BytesIO(raw[2]), "jurisdictions.csv"),
        },
        content_type="multipart/form-data",
    )
    text = resp.get_data(as_text=True)
    check(resp.status_code == 400 and "整批未出包" in text
          and "拨款表" in text and "第 2 行" in text,
          "文件有误：400 + 整批不出包 + 定位拨款表第 2 行")

    # 缺文件 → 整批不出包
    resp = client.post("/split", data={}, content_type="multipart/form-data")
    check(resp.status_code == 400 and "整批未出包" in resp.get_data(as_text=True),
          "缺少上传文件：400 且整批不出包")


def main():
    for fn in (test_basic, test_half_open_boundary, test_overlapping_attribution,
               test_massive, test_validation, test_web,
               test_packaging_plan, test_empty_base_tables,
               test_packaging_no_path_expansion, test_split_web):
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILURES.append(f"{fn.__name__} 抛异常")
            traceback.print_exc()
    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for msg in FAILURES:
            print(f"  - {msg}")
        sys.exit(1)
    print("全部自测通过。")


if __name__ == "__main__":
    main()
