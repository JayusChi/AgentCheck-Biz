"""Read-only discovery of the backend actually serving this request."""
from fastapi import APIRouter,Request
from agentcheck_biz.v2.manifest import capabilities


def create_capabilities_router():
    router=APIRouter()
    @router.get('/api/business/capabilities')
    def discover(request:Request):
        return capabilities() | dict(backend_address=str(request.base_url).rstrip('/'))
    return router
