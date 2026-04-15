"""运行时配置 API 路由"""

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field

from ...config import get_runtime_settings, update_runtime_settings
from ...services.amap_service import reset_amap_service

router = APIRouter(prefix="/settings", tags=["运行时配置"])


class RuntimeSettingsPayload(BaseModel):
    """前端设置页提交的运行时配置。"""

    vite_amap_web_js_key: Optional[str] = Field(default=None, description="高德 JS SDK Key")


def _require_admin_token(token: Optional[str]) -> None:
    expected = os.getenv("TRIPSTAR_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="运行时设置已禁用")
    if token != expected:
        raise HTTPException(status_code=401, detail="未授权")


@router.get("")
async def get_settings():
    """获取当前运行时配置。"""
    return {
        "success": True,
        "message": "ok",
        "data": get_runtime_settings(),
    }


@router.put("")
async def save_settings(
    payload: RuntimeSettingsPayload,
    admin_token: Optional[str] = Header(default=None, alias="X-TripStar-Admin-Token"),
):
    """保存运行时配置并立即生效。"""
    _require_admin_token(admin_token)
    try:
        updates = payload.model_dump(exclude_unset=True)
        updated = update_runtime_settings(updates)

        # 重置单例，确保新配置立即生效
        reset_amap_service()

        return {
            "success": True,
            "message": "配置已保存并立即生效",
            "data": updated,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {str(e)}") from e
