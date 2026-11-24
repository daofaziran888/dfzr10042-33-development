"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import region_publication, resource

# 具体前缀路由必须排在通用 /api/{module}/{entry_id} 之前，否则
# /api/region-publication/policy 会被当成 entry_id 抢走并返回 422。
ROUTERS = [region_publication, resource]
