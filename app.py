import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

from api.ai import AiApi
from api.base import BaseApi
from api.config import ConfigApi
from api.files import FilesApi
from api.keywords import KeywordsApi
from api.lists import ListsApi
from api.locations import LocationsApi
from api.search import SearchApi
from api.tracking import TrackingApi
from api.tasks import TasksApi
from api.troubleshoot import TroubleShootingApi
from api.system import SystemApi
from alembic import command
from alembic.config import Config
from env.environment import Environment
from fileops.service import recover_pending_operations
from fileops.trash import ensure_trash_dir
from tasks.loadcontrol import read_cpu_limit, read_cpu_temperature

env = Environment()

# init logging ...
logging.basicConfig(
    level=logging.getLevelName(env.get_log_level()),
    format="%(name)s [%(asctime)s] - %(levelname)s : %(message)s"
)
logger = logging.getLogger(f'{__name__}')


@asynccontextmanager
async def lifespan(application: FastAPI):
    # An invalid TRASH_DIR_NAME raises ValueError here, so a bad config fails
    # fast. A missing ROOT_DIR only warns: deletes create the trash lazily.
    try:
        ensure_trash_dir()
    except OSError:
        logger.warning('Could not create the trash directory on startup', exc_info=True)

    try:
        recover_pending_operations()
    except Exception:
        logger.exception('Failed to recover pending file operations on startup')

    temp = read_cpu_temperature()
    logger.info(
        'Load settings (#71): worker_pool_size=%s heavy_job_concurrency=%s ffmpeg_threads=%s '
        'process_niceness=%s cpu_temp_limit_c=%s load_avg_limit=%s cpu_count=%s cgroup_cpu_limit=%s '
        'cpu_temperature=%s',
        env.get_worker_pool_size(),
        env.get_heavy_job_concurrency(),
        env.get_ffmpeg_threads(),
        env.get_process_niceness(),
        env.get_cpu_temp_limit_c(),
        env.get_load_avg_limit(),
        os.cpu_count(),
        read_cpu_limit(),
        f'{temp:.1f}°C' if temp is not None else 'no sensor',
    )

    application.include_router(AiApi)
    application.include_router(BaseApi)
    application.include_router(ConfigApi)
    application.include_router(FilesApi)
    application.include_router(SearchApi)
    application.include_router(KeywordsApi)
    application.include_router(ListsApi)
    application.include_router(LocationsApi)
    application.include_router(TrackingApi)
    application.include_router(TasksApi)
    application.include_router(TroubleShootingApi)
    application.include_router(SystemApi)

    yield


if __name__ == '__main__':
    command.upgrade(Config('alembic.ini'), 'head')

    app = FastAPI(
        title='Footage Archive',
        description='A simple app to cataloge footage.',
        lifespan=lifespan,
        redoc_url=None,
        openapi=None
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins="*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    uvicorn.run(
        app,
        host=env.get_server_host(),
        port=env.get_server_port(),
    )
