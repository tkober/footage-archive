from fastapi import APIRouter

from api.dtos import CreateLocationRequest, LocationDto
from db.database import Database

LocationsApi = APIRouter(prefix='/locations')


@LocationsApi.get('/')
async def get_all_locations() -> list[LocationDto]:
    rows = Database().get_all_locations()
    return [LocationDto(**row) for row in rows]


@LocationsApi.post('/')
async def create_location(request: CreateLocationRequest) -> LocationDto:
    db = Database()
    row_id = db.create_location(
        name=request.name,
        city=request.city,
        region=request.region,
        country=request.country,
        latitude=request.latitude,
        longitude=request.longitude,
    )
    return LocationDto(
        id=row_id,
        name=request.name,
        city=request.city,
        region=request.region,
        country=request.country,
        latitude=request.latitude,
        longitude=request.longitude,
    )
