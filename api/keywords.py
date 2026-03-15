from fastapi import APIRouter, HTTPException

from api.dtos import KeywordRequest
from db.database import Database

KeywordsApi = APIRouter(prefix='/keywords')


@KeywordsApi.get('/')
async def get_all_keywords() -> list[str]:
    return Database().get_all_keywords()


@KeywordsApi.post('/')
async def add_keyword(request: KeywordRequest) -> None:
    keyword = request.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail='Keyword cannot be blank')
    Database().add_keyword(request.md5_hash, keyword)


@KeywordsApi.delete('/')
async def remove_keyword(request: KeywordRequest) -> None:
    Database().delete_keyword(request.md5_hash, request.keyword)
