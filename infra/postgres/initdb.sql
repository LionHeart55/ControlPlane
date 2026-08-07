-- Control-plane database bootstrap.
--
-- Runs ONCE, on first initialisation of an empty PGDATA directory, as the
-- POSTGRES_USER against the POSTGRES_DB created by the postgres image entrypoint.
--
-- SCOPE: extensions only.
--
-- Tables, enums, indexes and constraints come from Alembic (WP-04) and from
-- nowhere else. Creating schema here would give the project two competing
-- sources of truth: this file only runs on a fresh volume, so any table defined
-- here would silently drift from the migration history and would never be
-- applied to an existing database. If you are tempted to add a CREATE TABLE
-- below, write a migration instead.

-- pgcrypto supplies gen_random_uuid(), the server-side default for clusters.id.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
