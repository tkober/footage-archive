from fastapi import APIRouter, Query

from api.dtos import CreateLocationRequest, LocationDto, MapPoint
from db.database import Database

LocationsApi = APIRouter(prefix='/locations')


@LocationsApi.get('/map-points')
async def get_map_points(
    bbox_west:  float = Query(default=-180),
    bbox_south: float = Query(default=-90),
    bbox_east:  float = Query(default=180),
    bbox_north: float = Query(default=90),
    zoom:       int   = Query(default=2, ge=0, le=20),
) -> list[MapPoint]:
    rows = Database().get_map_points(bbox_west, bbox_south, bbox_east, bbox_north, zoom)
    return [MapPoint(**row) for row in rows]


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
