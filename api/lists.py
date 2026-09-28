import re
import unicodedata
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from api.dtos import (
    AddFilesToListRequest,
    AddFilesToListResponse,
    CreateListRequest,
    ListDto,
    ListItemDto,
    ListItemsResponse,
    RenameListRequest,
)
from db.database import Database, DuplicateListNameError
from env.environment import Environment
from exports.list_cards_pdf import render_list_cards_pdf

ListsApi = APIRouter(prefix='/lists')


def _slugify(name: str) -> str:
    """ASCII-safe filename slug, e.g. 'Édit TODOs' -> 'edit-todos'."""
    ascii_name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', ascii_name).strip('-').lower()
    return slug or 'list'


@ListsApi.get('')
async def get_all_lists() -> list[ListDto]:
    rows = Database().get_all_lists()
    return [ListDto(**row) for row in rows]


@ListsApi.post('')
async def create_list(request: CreateListRequest) -> ListDto:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail='Name cannot be blank')
    try:
        row = Database().create_list(name)
    except DuplicateListNameError:
        raise HTTPException(status_code=409, detail='A list with this name already exists')
    return ListDto(**row)


@ListsApi.patch('/{list_id}')
async def rename_list(list_id: int, request: RenameListRequest) -> ListDto:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail='Name cannot be blank')
    try:
        row = Database().rename_list(list_id, name)
    except DuplicateListNameError:
        raise HTTPException(status_code=409, detail='A list with this name already exists')
    if row is None:
        raise HTTPException(status_code=404, detail='List not found')
    return ListDto(**row)


@ListsApi.delete('/{list_id}')
async def delete_list(list_id: int) -> None:
    deleted = Database().delete_list(list_id)
    if not deleted:
        raise HTTPException(status_code=404, detail='List not found')


@ListsApi.get('/{list_id}/items')
async def get_list_items(
    list_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
) -> ListItemsResponse:
    if Database().get_list(list_id) is None:
        raise HTTPException(status_code=404, detail='List not found')
    total, rows = Database().get_list_items(list_id, page, page_size)
    return ListItemsResponse(
        total=total, page=page, page_size=page_size,
        items=[ListItemDto(**row) for row in rows],
    )


@ListsApi.post('/{list_id}/items')
async def add_files_to_list(list_id: int, request: AddFilesToListRequest) -> AddFilesToListResponse:
    if Database().get_list(list_id) is None:
        raise HTTPException(status_code=404, detail='List not found')
    result = Database().add_files_to_list(list_id, request.md5_hashes)
    return AddFilesToListResponse(
        added=[ListItemDto(**row) for row in result['added']],
        existing=[ListItemDto(**row) for row in result['existing']],
        unknown=result['unknown'],
    )


@ListsApi.delete('/{list_id}/items/{md5_hash}')
async def remove_file_from_list(list_id: int, md5_hash: str) -> None:
    removed = Database().remove_file_from_list(list_id, md5_hash)
    if not removed:
        raise HTTPException(status_code=404, detail='File is not in this list')


@ListsApi.get('/{list_id}/export.pdf')
async def export_list_pdf(
    list_id: int,
    cols: int = Query(default=4, ge=1, le=8),
    rows: int = Query(default=7, ge=1, le=12),
) -> Response:
    list_row = Database().get_list(list_id)
    if list_row is None:
        raise HTTPException(status_code=404, detail='List not found')
    items = Database().get_all_list_items_for_export(list_id)
    root_dir = Environment().get_root_dir()
    pdf_bytes = render_list_cards_pdf(list_row['name'], items, root_dir, cols=cols, rows=rows)

    slug = _slugify(list_row['name'])
    ascii_filename = f'{slug}-cards.pdf'
    utf8_filename = f"{list_row['name']}-cards.pdf"
    content_disposition = (
        f'attachment; filename="{ascii_filename}"; '
        f"filename*=UTF-8''{quote(utf8_filename)}"
    )
    return Response(
        content=pdf_bytes,
        media_type='application/pdf',
        headers={'Content-Disposition': content_disposition},
    )


@ListsApi.get('/{list_id}/items/by-code/{code}')
async def get_list_item_by_code(list_id: int, code: str) -> ListItemDto:
    row = Database().get_list_item_by_code(list_id, code)
    if row is None:
        raise HTTPException(status_code=404, detail='Item not found')
    return ListItemDto(**row)
