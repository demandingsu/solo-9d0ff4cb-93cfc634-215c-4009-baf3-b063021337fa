"""拨款分包：把同一批三份 CSV 拆成互不关联的包，供多个小组分别核对。

分包规则
--------
- 每笔拨款只进一包；
- 每包带上包内拨款在其拨款日命中的全部归属行，以及经这些归属行
  （同学院、拨款日有效）命中的全部归口行；
- 共用源行（归属行或归口行）的拨款必须同包，间接共用也算
  （即"共用"关系下的连通分量，用并查集传递）；
- 互不关联的拨款分开；
- 分包只按"学院 → 拨款日有效的归口行"聚合查询，**不展开完整匹配路径**
  （不构造归属 × 归口的笛卡尔积），复杂度与命中源行数成线性关系。

一致性论证
----------
某行是否被某笔拨款命中，只取决于"项目号 / 学院码 + 拨款日是否落入有效期
[start, end)"，与同一份 CSV 里还有哪些其他行无关。包内包含了每笔拨款在整批
中命中的全部源行；其他拨款带进来的额外源行，要么对该拨款的拨款日无效、
要么键不匹配，不会多命中。因此每包单独重跑时：

- 逐笔入表次数、漏计断层（归属层 / 归口层）、多计证据（单位分布与示例路径）
  都与整批一致；
- 各包的统计单位合计相加等于整批合计（拨款被划分，且逐笔贡献相同）。
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .reconcile import (
    ATTRIBUTION_COLUMNS,
    GRANT_COLUMNS,
    JURISDICTION_COLUMNS,
    AttributionRow,
    Grant,
    JurisdictionRow,
)

# ZIP 内文件名
GRANT_CSV_NAME = "拨款表.csv"
ATTRIBUTION_CSV_NAME = "归属表.csv"
JURISDICTION_CSV_NAME = "归口表.csv"
MANIFEST_NAME = "分包清单.csv"
MANIFEST_COLUMNS = ["包", "单号", "项目号", "拨款日", "金额_分"]


@dataclass
class Package:
    """一个分包：互相关联的拨款 + 它们在拨款日命中的全部源行。"""

    number: int                     # 包号，从 1 开始，按首笔拨款原始顺序分配
    folder: str                     # ZIP 内文件夹名，如 "第01包"（位数按包数对齐）
    grants: List[Grant]
    attributions: List[AttributionRow]
    jurisdictions: List[JurisdictionRow]


def package_folder(number: int, width: int) -> str:
    """包文件夹名，如 第01包 / 第108包（width 为包数的位数，至少 2 位）。"""
    return f"第{number:0{width}d}包"


class _UnionFind:
    """按拨款下标合并的并查集（路径压缩）。"""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        parent = self.parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _hit_rows(
    grants: List[Grant],
    attributions: List[AttributionRow],
    jurisdictions: List[JurisdictionRow],
) -> List[Tuple[List[AttributionRow], List[JurisdictionRow]]]:
    """逐笔计算命中的归属行与归口行。

    归口行按"命中归属行的学院"去重后查询，只列出源行本身，
    不构造 (归属行, 归口行) 的匹配路径组合。
    """
    attr_index: Dict[str, List[AttributionRow]] = {}
    for row in attributions:
        attr_index.setdefault(row.project, []).append(row)
    jur_index: Dict[str, List[JurisdictionRow]] = {}
    for row in jurisdictions:
        jur_index.setdefault(row.college, []).append(row)

    hits: List[Tuple[List[AttributionRow], List[JurisdictionRow]]] = []
    for grant in grants:
        day = grant.grant_date
        attr_hits = [
            row
            for row in attr_index.get(grant.project, [])
            if row.start <= day < row.end
        ]
        jur_hits: List[JurisdictionRow] = []
        seen_colleges = set()
        for attr in attr_hits:
            if attr.college in seen_colleges:
                continue
            seen_colleges.add(attr.college)
            jur_hits.extend(
                row
                for row in jur_index.get(attr.college, [])
                if row.start <= day < row.end
            )
        hits.append((attr_hits, jur_hits))
    return hits


def plan_packages(
    grants: List[Grant],
    attributions: List[AttributionRow],
    jurisdictions: List[JurisdictionRow],
) -> List[Package]:
    """把整批拨款切成互不关联的包（共用源行关系下的连通分量）。"""
    hits = _hit_rows(grants, attributions, jurisdictions)

    # 共用任一条源行（归属行或归口行）的拨款合并；间接共用经并查集传递
    uf = _UnionFind(len(grants))
    attr_owner: Dict[int, int] = {}   # 归属行号 -> 首个命中的拨款下标
    jur_owner: Dict[int, int] = {}    # 归口行号 -> 首个命中的拨款下标
    for idx, (attr_hits, jur_hits) in enumerate(hits):
        for row in attr_hits:
            owner = attr_owner.setdefault(row.line_no, idx)
            if owner != idx:
                uf.union(idx, owner)
        for row in jur_hits:
            owner = jur_owner.setdefault(row.line_no, idx)
            if owner != idx:
                uf.union(idx, owner)

    groups: Dict[int, List[int]] = {}
    for idx in range(len(grants)):
        groups.setdefault(uf.find(idx), []).append(idx)

    attr_by_line = {row.line_no: row for row in attributions}
    jur_by_line = {row.line_no: row for row in jurisdictions}
    attr_pos = {row.line_no: pos for pos, row in enumerate(attributions)}
    jur_pos = {row.line_no: pos for pos, row in enumerate(jurisdictions)}

    packages: List[Package] = []
    # 包号按组内首笔拨款的原始顺序分配，同一输入结果稳定
    ordered = sorted(groups.values(), key=lambda m: m[0])
    width = max(2, len(str(len(ordered))))
    for number, members in enumerate(ordered, start=1):
        attr_lines = sorted(
            {row.line_no for i in members for row in hits[i][0]},
            key=attr_pos.__getitem__,
        )
        jur_lines = sorted(
            {row.line_no for i in members for row in hits[i][1]},
            key=jur_pos.__getitem__,
        )
        packages.append(
            Package(
                number=number,
                folder=package_folder(number, width),
                grants=[grants[i] for i in members],
                attributions=[attr_by_line[line] for line in attr_lines],
                jurisdictions=[jur_by_line[line] for line in jur_lines],
            )
        )
    return packages


# --------------------------------------------------------------------------- #
# CSV / ZIP 输出
# --------------------------------------------------------------------------- #
def _csv_bytes(header: List[str], rows: List[List[object]]) -> bytes:
    """写一份 CSV：UTF-8 带 BOM（Excel 直接打开不乱码，核对页同样可读）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def grants_csv_bytes(grants: List[Grant]) -> bytes:
    return _csv_bytes(
        GRANT_COLUMNS,
        [[g.grant_no, g.project, g.grant_date.isoformat(), g.amount] for g in grants],
    )


def attributions_csv_bytes(rows: List[AttributionRow]) -> bytes:
    return _csv_bytes(
        ATTRIBUTION_COLUMNS,
        [
            [r.line_no, r.project, r.college, r.start.isoformat(), r.end.isoformat()]
            for r in rows
        ],
    )


def jurisdictions_csv_bytes(rows: List[JurisdictionRow]) -> bytes:
    return _csv_bytes(
        JURISDICTION_COLUMNS,
        [
            [r.line_no, r.college, r.unit, r.start.isoformat(), r.end.isoformat()]
            for r in rows
        ],
    )


def manifest_csv_bytes(packages: List[Package]) -> bytes:
    """分包清单：每笔拨款属于哪一包，便于科研处分派与核对。"""
    rows: List[List[object]] = []
    for pkg in packages:
        for g in pkg.grants:
            rows.append(
                [pkg.folder, g.grant_no, g.project, g.grant_date.isoformat(), g.amount]
            )
    return _csv_bytes(MANIFEST_COLUMNS, rows)


def build_packages_zip(packages: List[Package]) -> bytes:
    """打包 ZIP：每包一个文件夹，各含拨款表 / 归属表 / 归口表三份 CSV。

    某张底表没有源行时仍写出只有表头的 CSV，重跑后漏计断层判断与整批一致。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pkg in packages:
            folder = pkg.folder
            zf.writestr(f"{folder}/{GRANT_CSV_NAME}", grants_csv_bytes(pkg.grants))
            zf.writestr(
                f"{folder}/{ATTRIBUTION_CSV_NAME}",
                attributions_csv_bytes(pkg.attributions),
            )
            zf.writestr(
                f"{folder}/{JURISDICTION_CSV_NAME}",
                jurisdictions_csv_bytes(pkg.jurisdictions),
            )
        zf.writestr(MANIFEST_NAME, manifest_csv_bytes(packages))
    return buf.getvalue()
