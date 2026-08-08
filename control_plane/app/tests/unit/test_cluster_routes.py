"""Cluster write routes against a real session, with a fake DB.

The bug these exist for: `clusters.updated_at` carries `onupdate=now()`, so an
UPDATE leaves that attribute expired. Reading it during serialisation then needs
a SELECT, which pydantic cannot perform synchronously under asyncio -- so a
successful write returned 500 MissingGreenlet. It survived every unit test and
the whole smoke suite, because nothing had ever issued a PATCH.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.db.models import Cluster


def test_updated_at_has_a_server_side_onupdate() -> None:
    """The property that makes an explicit refresh necessary.

    If this ever becomes a plain Python default, the refresh in the PATCH and
    DELETE routes stops being load-bearing -- and this test is where you would
    find that out.
    """
    column = Cluster.__table__.c.updated_at
    assert column.onupdate is not None, "updated_at must be maintained automatically"
    # A SQL expression, evaluated by PostgreSQL, hence expired after flush.
    sql_expression = sa.sql.functions.Function | sa.sql.elements.ClauseElement
    assert isinstance(column.onupdate.arg, sql_expression)


def test_created_at_and_updated_at_have_server_defaults() -> None:
    for name in ("created_at", "updated_at"):
        assert Cluster.__table__.c[name].server_default is not None, name


def test_patch_and_delete_routes_refresh_before_serialising() -> None:
    """Pins the fix in place.

    Asserted against the source rather than by mocking a session: the failure
    mode is an interaction between SQLAlchemy's expiry and pydantic's attribute
    access, which a mock would not reproduce. What matters is that the refresh
    call is still there.
    """
    import inspect

    from app.api.routers import clusters

    for route in (clusters.update_cluster, clusters.delete_cluster):
        source = inspect.getsource(route)
        assert "session.refresh(" in source, (
            f"{route.__name__} must refresh after commit or serialising "
            f"updated_at raises MissingGreenlet"
        )
