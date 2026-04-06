from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.database import Database
from env.environment import Environment
from shot_classifier.classifier import ShotClassifier

AiApi = APIRouter(prefix='/ai')

VIDEO_TYPES = {'video', '360_video'}


class ClassifyRequest(BaseModel):
    path: str


@AiApi.post('/classify-shot')
def classify_shot(request: ClassifyRequest):
    env = Environment()
    if not Path(request.path).is_relative_to(Path(env.get_root_dir())):
        raise HTTPException(status_code=403, detail='Path is outside ROOT_DIR')

    db = Database()
    file_record = db.get_file_by_path(Path(request.path))
    if file_record is None:
        raise HTTPException(status_code=404, detail='File not tracked')
    if file_record['media_type'] not in VIDEO_TYPES:
        raise HTTPException(status_code=400, detail='File is not a video')

    result = ShotClassifier().classify(file_record['md5_hash'])
    return result.model_dump()
