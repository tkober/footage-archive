from fastapi import APIRouter, Query

from api.dtos import FileSearchQuery, SearchResponse, SearchResult
from db.database import Database

SearchApi = APIRouter(prefix='/files')


@SearchApi.get('/search-facets')
async def get_search_facets(
    field: str,
    q: str = '',
    limit: int = Query(default=10, ge=1, le=50),
) -> list[str]:
    return Database().get_facet_values(field, q, limit)


@SearchApi.post('/search')
async def search_files(query: FileSearchQuery) -> SearchResponse:
    db = Database()
    total, rows = db.search_files(query.model_dump())
    return SearchResponse(
        total=total,
        page=query.page,
        page_size=query.page_size,
        items=[SearchResult(**r) for r in rows],
    )
