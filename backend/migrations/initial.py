"""Initialize the first installation from the versioned ORM schema."""
import logging

from src.config import Session, engine
from src.models import Base
from src.settlement import backfill

log = logging.getLogger('uvicorn.error')


def upgrade():
    Base.metadata.create_all(engine)
    try:
        with Session.begin() as db:
            count = backfill(db)
        if count:
            log.info('Settled %s previously approved plan(s)', count)
    except Exception:
        # A failed backfill must not block startup; it is retried on the next run.
        log.exception('Settlement backfill failed; approval path still settles new plans')


if __name__ == '__main__':
    upgrade()
