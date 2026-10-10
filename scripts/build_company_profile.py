#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""建立并回填公司档案（company_profile + company_metrics）。

为什么需要
----------
行业/板块数据早就在库里了 —— 但塞在 ``fundamental_snapshot.payload`` 这个不透明
JSON blob 里，于是无法查询、无法 join、无法按行业筛股或把资讯按行业归类。
实测 21/21 行都有 ``belong_boards``（每只 14~44 个板块，平均 23.3）。

关键设计：板块必须分类，不能照抄
--------------------------------
``belong_boards`` 里大部分是噪音而不是业务分类。贵州茅台的 26 个板块里：

    有业务含义  白酒Ⅲ / 白酒Ⅱ / 食品饮料（申万三级层级）· 贵州板块 · 酿酒概念 · 超级品牌
    纯噪音      沪股通 / 融资融券 / 大盘股 / 权重股 / MSCI中国 / 富时罗素 /
                央视50_ / 上证50_ / HS300_ / 东方财富热股

直接 dump 会让「按板块选股」变成按指数成分选股。因此申万层级单独成列，
概念只保留过滤后的业务概念，**原始列表整份保留在 boards_json 里**以便回溯 ——
过滤规则改错了还能重算，不需要重新抓数据。

关键设计：company_metrics 按日累积，绝不覆盖
------------------------------------------
``as_of`` 进主键。估值每天变，用「最新值覆盖」会让回测看到未来数据
（前视偏差）—— 用户的目标里明确禁止这一点。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("build_company_profile")

# 纯指数/交易属性类板块 —— 与"这家公司做什么"无关，参与筛选只会误导。
NOISE_PATTERNS = [
    r"沪股通", r"深股通", r"港股通", r"融资融券", r"融券", r"沪港通",
    r"大盘股", r"小盘股", r"中盘股", r"权重股", r"百元股", r"低价股",
    r"MSCI", r"富时", r"标普", r"FTSE", r"罗素",
    r"央视", r"上证\d", r"深证\d", r"HS\d", r"沪深\d", r"中证\d", r"科创\d",
    r"东方财富", r"热股", r"昨日", r"涨停", r"跌停", r"次新",
    r"标准普尔", r"机构重仓", r"基金重仓", r"QFII", r"社保重仓", r"北向",
    r"预盈预增", r"预亏预减", r"年报", r"季报", r"高送转", r"解禁",
]
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)

# 申万行业层级用罗马数字后缀标注（白酒Ⅲ / 白酒Ⅱ）。
# **但一级行业没有标记** —— 「食品饮料」就是一级，不带任何后缀；
# 而「酿酒概念」「味蕾经济」「超级品牌」同样是没标记的，却是概念。
# 因此一级不能靠形状判断，只能用申万一级的标准名单来认。
_LEVEL_RE = re.compile(r"[ⅠⅡⅢⅣ]$")

SW_LEVEL1 = {
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰",
    "电力设备", "国防军工", "计算机", "传媒", "通信", "银行", "非银金融",
    "美容护理", "石油石化", "煤炭", "环保", "机械设备",
}


def _classify_boards(boards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把 belong_boards 拆成 行业层级 / 地域 / 业务概念 / 噪音。"""
    industry: Dict[int, str] = {}
    region: Optional[str] = None
    concepts: List[str] = []
    noise: List[str] = []

    for item in boards:
        name = str((item or {}).get("name") or "").strip()
        if not name:
            continue
        bare = name.rstrip("_")

        if _NOISE_RE.search(bare) or name.endswith("_"):
            noise.append(name)
            continue
        if name.endswith("板块"):
            region = region or bare
            continue
        if _LEVEL_RE.search(bare):
            # 白酒Ⅲ → 3 级；白酒Ⅱ → 2 级
            level = "ⅠⅡⅢⅣ".index(bare[-1]) + 1
            industry[level] = bare[:-1]
            continue
        if bare in SW_LEVEL1:
            # 一级行业没有层级标记，只能靠名单识别
            industry[1] = bare
            continue
        concepts.append(bare)

    return {
        # 一级缺失时用二级顶替：宁可粗一档，也不要整列为空 ——
        # 「按行业筛选」拿不到结果比拿到的行业略粗更没用。
        "industry_l1": industry.get(1) or industry.get(2),
        "industry_l2": industry.get(2),
        "industry_l3": industry.get(3),
        "region": region,
        "concepts": concepts,
        "noise": noise,
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS company_profile (
    canonical_id   VARCHAR(32) PRIMARY KEY,
    code           VARCHAR(16) NOT NULL,
    name           VARCHAR(64),
    market         VARCHAR(8),
    industry_l1    VARCHAR(32),
    industry_l2    VARCHAR(32),
    industry_l3    VARCHAR(32),
    region         VARCHAR(32),
    concepts_json  TEXT,
    boards_json    TEXT,
    updated_at     DATETIME,
    created_at     DATETIME
);

CREATE TABLE IF NOT EXISTS company_metrics (
    canonical_id  VARCHAR(32) NOT NULL,
    as_of         DATE        NOT NULL,
    pe_ttm        REAL,
    pb            REAL,
    total_mv      REAL,
    circ_mv       REAL,
    payload       TEXT,
    created_at    DATETIME,
    PRIMARY KEY (canonical_id, as_of)
);

CREATE INDEX IF NOT EXISTS ix_company_profile_industry
    ON company_profile(industry_l1, industry_l2);
CREATE INDEX IF NOT EXISTS ix_company_profile_code
    ON company_profile(code);
CREATE INDEX IF NOT EXISTS ix_company_metrics_as_of
    ON company_metrics(as_of);
"""


def ensure_schema(conn: sqlite3.Connection, *, dry_run: bool) -> None:
    if dry_run:
        print("  [dry-run] 建表 company_profile / company_metrics（+3 索引）")
        return
    conn.executescript(SCHEMA)
    conn.commit()


def build_from_snapshots(conn: sqlite3.Connection, *, dry_run: bool) -> Tuple[int, int]:
    """从 fundamental_snapshot 回填档案与指标。幂等：按 canonical_id / (cid, as_of) upsert。"""
    rows = list(
        conn.execute(
            "SELECT code, canonical_id, payload, created_at FROM fundamental_snapshot "
            "WHERE canonical_id IS NOT NULL ORDER BY id"
        )
    )
    profiles = metrics = 0
    skipped = 0

    for code, cid, payload, created_at in rows:
        try:
            data = json.loads(payload)
        except Exception:
            skipped += 1
            continue

        boards = data.get("belong_boards") or []
        cls = _classify_boards(boards)
        name = (
            data.get("stock_name")
            or data.get("name")
            or (data.get("quote") or {}).get("data", {}).get("stock_name")
        )
        market = data.get("market") or (cid[:2] if cid else None)
        now = datetime.now(timezone.utc).isoformat()

        if not dry_run:
            conn.execute(
                """
                INSERT INTO company_profile
                    (canonical_id, code, name, market, industry_l1, industry_l2,
                     industry_l3, region, concepts_json, boards_json, updated_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    code=excluded.code, name=excluded.name, market=excluded.market,
                    industry_l1=excluded.industry_l1, industry_l2=excluded.industry_l2,
                    industry_l3=excluded.industry_l3, region=excluded.region,
                    concepts_json=excluded.concepts_json, boards_json=excluded.boards_json,
                    updated_at=excluded.updated_at
                """,
                (
                    cid, code, name, market,
                    cls["industry_l1"], cls["industry_l2"], cls["industry_l3"],
                    cls["region"],
                    json.dumps(cls["concepts"], ensure_ascii=False),
                    json.dumps(boards, ensure_ascii=False),
                    now, now,
                ),
            )
        profiles += 1

        # 估值：as_of 取快照创建日（没有更细的时间戳时，日粒度足够，
        # 且保证同一天多次抓取只留一份，不制造重复行的假历史）
        val = (data.get("valuation") or {}).get("data") or {}
        if isinstance(val, dict) and val:
            as_of = str(created_at or now)[:10]
            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO company_metrics
                        (canonical_id, as_of, pe_ttm, pb, total_mv, circ_mv, payload, created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(canonical_id, as_of) DO UPDATE SET
                        pe_ttm=excluded.pe_ttm, pb=excluded.pb,
                        total_mv=excluded.total_mv, circ_mv=excluded.circ_mv,
                        payload=excluded.payload
                    """,
                    (
                        cid, as_of,
                        val.get("pe_ratio"), val.get("pb_ratio"),
                        val.get("total_mv"), val.get("circ_mv"),
                        json.dumps(val, ensure_ascii=False), now,
                    ),
                )
            metrics += 1

    if not dry_run:
        conn.commit()
    return profiles, metrics


def report(conn: sqlite3.Connection) -> None:
    print()
    print("═══ company_profile ═══")
    total = conn.execute("SELECT COUNT(*) FROM company_profile").fetchone()[0]
    print(f"  行数: {total}")
    for r in conn.execute(
        "SELECT canonical_id, name, industry_l1, industry_l2, industry_l3, region "
        "FROM company_profile ORDER BY canonical_id LIMIT 12"
    ):
        print("    %-11s %-8s %-8s %-8s %-8s %s" % tuple("" if v is None else v for v in r))
    print()
    print("  按申万一级行业分组（这就是之前查不出来的东西）:")
    for ind, n in conn.execute(
        "SELECT COALESCE(industry_l1,'(未分类)'), COUNT(*) FROM company_profile "
        "GROUP BY industry_l1 ORDER BY COUNT(*) DESC"
    ):
        print(f"    {ind:<12} {n} 只")

    print()
    print("═══ company_metrics ═══")
    n = conn.execute("SELECT COUNT(*) FROM company_metrics").fetchone()[0]
    d = conn.execute(
        "SELECT COUNT(DISTINCT canonical_id), COUNT(DISTINCT as_of) FROM company_metrics"
    ).fetchone()
    print(f"  行数 {n} · 覆盖 {d[0]} 只 · {d[1]} 个不同日期")
    if n:
        print("  样本:")
        for r in conn.execute(
            "SELECT canonical_id, as_of, pe_ttm, pb, total_mv FROM company_metrics "
            "ORDER BY canonical_id LIMIT 5"
        ):
            print("    %-11s %s  PE=%-8s PB=%-6s MV=%s" % tuple("" if v is None else v for v in r))

    print()
    print("═══ 板块分类效果（贵州茅台）═══")
    row = conn.execute(
        "SELECT industry_l1, industry_l2, industry_l3, region, concepts_json, boards_json "
        "FROM company_profile WHERE code='600519'"
    ).fetchone()
    if row:
        l1, l2, l3, region, cj, bj = row
        print(f"  申万: {l1} / {l2} / {l3}")
        print(f"  地域: {region}")
        concepts = json.loads(cj or "[]")
        print(f"  业务概念({len(concepts)}): {concepts[:8]}")
        print(f"  原始板块 {len(json.loads(bj or '[]'))} 个（噪音已剔除出概念，但完整保留以便回溯）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parents[1] / "data" / "stock_analysis.db"),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    print(f"  数据库: {args.db}")
    print(f"  模式  : {'DRY-RUN' if args.dry_run else '实际执行'}")

    conn = sqlite3.connect(args.db)
    try:
        ensure_schema(conn, dry_run=args.dry_run)
        profiles, metrics = build_from_snapshots(conn, dry_run=args.dry_run)
        print(f"  档案 {profiles} 行 · 指标 {metrics} 行")
        if not args.dry_run:
            report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
