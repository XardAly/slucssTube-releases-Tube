from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from backend.notifications.releases import AndroidRelease, publish_release


def test_restart_and_concurrent_boot_publish_one_release(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'release.db'}")
    AndroidRelease.__table__.create(engine)

    def boot(_):
        with Session(engine) as db:
            row, created = publish_release(db, release_id="android-23", version="1.3.0",
                                          version_code=23, artifacts={"sha256": "a" * 64})
            return row.published_at, created

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(boot, range(8)))
    assert sum(created for _, created in results) == 1
    assert len({str(date).replace("+00:00", "") for date, _ in results}) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AndroidRelease)) == 1
        with pytest.raises(ValueError, match="immutable"):
            publish_release(db, release_id="android-23", version="1.3.0", version_code=23,
                            artifacts={"sha256": "b" * 64})
        with pytest.raises(ValueError, match="immutable"):
            publish_release(db, release_id="different-id", version="1.3.0", version_code=23,
                            artifacts={"sha256": "a" * 64})
        _, created = publish_release(db, release_id="android-24", version="1.3.1", version_code=24,
                                     artifacts={"sha256": "b" * 64})
        assert created
    engine.dispose()
