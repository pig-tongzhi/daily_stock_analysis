# -*- coding: utf-8 -*-
"""数据盘点接口：给前端「数据观测」页用。"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.services.data_inventory_service import build_data_inventory

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/inventory",
    summary="数据库内容盘点",
    description=(
        "列出每张表有多少行、覆盖多少标的、最新一条的时间与新鲜度、"
        "每个列为空的比例，以及值得注意的缺口（整列为空、表为空、表不存在）。"
        "只读，用于观测而不是控制。"
    ),
)
def get_data_inventory() -> Dict[str, Any]:
    try:
        return build_data_inventory()
    except Exception as exc:
        logger.warning("[DataInventory] 盘点失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "inventory_failed", "message": str(exc)[:200]},
        ) from exc
