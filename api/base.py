from fastapi import APIRouter
from starlette.responses import RedirectResponse

from env.environment import Environment

BaseApi = APIRouter()


@BaseApi.get('/')
async def get_root():
    response = RedirectResponse(url='/docs')
    return response


@BaseApi.get('/version')
async def get_version():
    return Environment().get_release_name()
