# -*- coding: utf-8 -*-
"""公司档案的同步：从 fundamental_snapshot 派生 company_profile / company_metrics。

两条写入路径共用这一个函数：

* **按需**—— ``DatabaseManager.save_fundamental_snapshot`` 写完快照后立即同步，
  这样只要某只股票被分析过，它的档案立刻可查。
* **定时**—— runtime_scheduler 的 ``company_profile_refresh`` 任务周期性全量重算，
  兜住历史数据、以及分类规则改动后的重算需求。

为什么不让公司档案"只由脚本回填"：脚本是一次性的，新分析出来的股票不会被收录，
档案会随着时间越来越旧 —— 那正是"存了没人读"的另一种形态。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 纯指数/交易属性类板块 —— 与"这家公司做什么"无关，参与筛选只会误导。
# 例：贵州茅台 28 个板块里，沪股通/融资融券/大盘股/权重股/MSCI中国/富时罗素/
# 央视50_/HS300_ 都是噪音，真正有业务含义的只有少数几个。
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
# **一级行业没有标记** —— 「食品饮料」是一级却不带后缀；而「酿酒概念」「味蕾经济」
# 同样不带后缀却是概念。形状判断不了，只能用申万一级的标准名单来认。
_LEVEL_RE = re.compile(r"[ⅠⅡⅢⅣ]$")

SW_LEVEL1 = {
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰",
    "电力设备", "国防军工", "计算机", "传媒", "通信", "银行", "非银金融",
    "美容护理", "石油石化", "煤炭", "环保", "机械设备",
}


def classify_boards(boards: List[Dict[str, Any]]) -> Dict[str, Any]:
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
            level = "ⅠⅡⅢⅣ".index(bare[-1]) + 1
            industry[level] = bare[:-1]
            continue
        if bare in SW_LEVEL1:
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


def sync_from_snapshots(*, code: Optional[str] = None) -> Tuple[int, int]:
    """把 fundamental_snapshot 的内容同步进 company_profile / company_metrics。

    ``code`` 为 None 时全量重算（定时任务用）；指定时只同步那一只（按需路径用）。

    幂等：profile 按 canonical_id upsert，metrics 按 (canonical_id, as_of) upsert，
    因此重复调用与并发调用都安全。
    """
    try:
        from sqlalchemy import text

        from src.storage import DatabaseManager

        manager = DatabaseManager.get_instance()
        engine = manager._engine
    except Exception as exc:
        logger.warning("[CompanyProfile] 初始化失败: %s", exc, exc_info=True)
        return (0, 0)

    sql = (
        "SELECT code, canonical_id, payload, created_at FROM fundamental_snapshot "
        "WHERE canonical_id IS NOT NULL"
    )
    params: Dict[str, Any] = {}
    if code:
        cid = manager._derive_canonical_id(code)
        if not cid:
            return (0, 0)
        sql += " AND canonical_id = :cid"
        params["cid"] = cid
    sql += " ORDER BY id"

    profiles = metrics = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(sql), params).fetchall()
            for row_code, cid, payload, created_at in rows:
                try:
                    data = json.loads(payload) if payload else {}
                except Exception:
                    continue

                cls = classify_boards(data.get("belong_boards") or [])
                name = (
                    data.get("stock_name")
                    or data.get("name")
                    or ((data.get("quote") or {}).get("data") or {}).get("stock_name")
                )
                market = data.get("market") or (cid[:2] if cid else None)

                conn.execute(
                    text(
                        """
                        INSERT INTO company_profile
                            (canonical_id, code, name, market, industry_l1, industry_l2,
                             industry_l3, region, concepts_json, boards_json, updated_at, created_at)
                        VALUES (:cid,:code,:name,:market,:l1,:l2,:l3,:region,:cj,:bj,:now,:now)
                        ON CONFLICT(canonical_id) DO UPDATE SET
                            code=excluded.code,
                            -- payload 里常常没有 stock_name，无条件覆盖会把从股票索引
                            -- 补好的名字抹成 NULL。有值才覆盖。
                            name=COALESCE(excluded.name, company_profile.name),
                            market=COALESCE(excluded.market, company_profile.market),
                            industry_l1=excluded.industry_l1, industry_l2=excluded.industry_l2,
                            industry_l3=excluded.industry_l3, region=excluded.region,
                            concepts_json=excluded.concepts_json, boards_json=excluded.boards_json,
                            updated_at=excluded.updated_at
                        """
                    ),
                    {
                        "cid": cid, "code": row_code, "name": name, "market": market,
                        "l1": cls["industry_l1"], "l2": cls["industry_l2"],
                        "l3": cls["industry_l3"], "region": cls["region"],
                        "cj": json.dumps(cls["concepts"], ensure_ascii=False),
                        "bj": json.dumps(data.get("belong_boards") or [], ensure_ascii=False),
                        "now": now,
                    },
                )
                profiles += 1

                val = (data.get("valuation") or {}).get("data") or {}
                if isinstance(val, dict) and val:
                    # as_of 用快照创建日：同一天多次抓取只留一份，不制造重复行的假历史。
                    as_of = str(created_at or now)[:10]
                    conn.execute(
                        text(
                            """
                            INSERT INTO company_metrics
                                (canonical_id, as_of, pe_ttm, pb, total_mv, circ_mv, payload, created_at)
                            VALUES (:cid,:as_of,:pe,:pb,:mv,:cmv,:payload,:now)
                            ON CONFLICT(canonical_id, as_of) DO UPDATE SET
                                pe_ttm=excluded.pe_ttm, pb=excluded.pb,
                                total_mv=excluded.total_mv, circ_mv=excluded.circ_mv,
                                payload=excluded.payload
                            """
                        ),
                        {
                            "cid": cid, "as_of": as_of,
                            "pe": val.get("pe_ratio"), "pb": val.get("pb_ratio"),
                            "mv": val.get("total_mv"), "cmv": val.get("circ_mv"),
                            "payload": json.dumps(val, ensure_ascii=False), "now": now,
                        },
                    )
                    metrics += 1
    except Exception as exc:
        logger.warning("[CompanyProfile] 同步失败: %s", exc, exc_info=True)
        return (profiles, metrics)

    return (profiles, metrics)
