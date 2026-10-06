"""clear 0/0 GPS fixes in FileDetails

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06

"""
from typing import Sequence, Union

from alembic import op

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Insta360 X3 photos without a GPS fix write 0/0 instead of omitting the
    # tags (#91), which rendered as a map point at "Null Island" in the
    # Atlantic. probe_photo now treats 0/0 as "no fix" for new scans; this
    # clears the stray rows already in the table.
    op.execute(
        'UPDATE "FileDetails" SET latitude = NULL, longitude = NULL, altitude = NULL '
        'WHERE latitude = 0 AND longitude = 0'
    )


def downgrade() -> None:
    # No-op: the original 0/0 values were never a real position, so there's
    # nothing worth restoring.
    pass
