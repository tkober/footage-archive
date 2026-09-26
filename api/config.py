from fastapi import APIRouter

from api.dtos import ConfigResponse
from env.environment import Environment

ConfigApi = APIRouter(prefix='/config')

_env = Environment()


@ConfigApi.get('')
async def get_config() -> ConfigResponse:
    return ConfigResponse(
        root_dir=_env.get_root_dir(),
        task_poll_interval_ms=_env.get_task_poll_interval_ms(),
        browser_hidden_extensions=_env.get_browser_hidden_extensions(),
        google_maps_api_key=_env.get_google_maps_api_key(),
        google_maps_map_id=_env.get_google_maps_map_id(),
    )
