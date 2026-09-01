from __future__ import annotations

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["operations"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    if not request.app.state.settings.metrics_enabled:
        return Response(status_code=404)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
