import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from api.base import BaseApi
from api.files import FilesApi
from api.scanning import ScanningApi
from api.tasks import TasksApi
from davinci.davinciresolve import Metadata
from db.database import Database
from env.environment import Environment

env = Environment()

# init logging ...
logging.basicConfig(
    level=logging.getLevelName(env.get_log_level()),
    format="%(name)s [%(asctime)s] - %(levelname)s : %(message)s"
)
logger = logging.getLogger(f'{__name__}')


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.include_router(BaseApi)
    application.include_router(FilesApi)
    application.include_router(ScanningApi)
    application.include_router(TasksApi)

    yield


if __name__ == '__main__':
    Database().connect().setup().disconnect()
    
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
        env_file='../.env' if os.path.isfile('../.env') else None
    )
