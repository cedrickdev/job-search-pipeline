#!/bin/sh
# Creates the database the test suite owns, once, when the data directory is
# first initialized.
#
# The tests drop and rebuild `public` on every run (tests/conftest.py). Pointing
# them at the development database would therefore delete imported V1 data, so
# they get a database of their own instead — and one that Alembic still has to
# build from scratch, because this script deliberately does not install PostGIS:
# `CREATE EXTENSION postgis` is migration rev_0001's job, and a test suite that
# starts from a database where the extension already exists never proves that.
#
# `CREATE DATABASE` without `TEMPLATE` copies `template1`, which the image leaves
# untouched — it loads PostGIS into `template_postgis` and into `POSTGRES_DB`
# only. So the new database comes up with nothing but plpgsql whatever order the
# entrypoint happens to run these scripts in (shell glob order follows the
# container's collation, not byte order).
#
# Written without `exit` and without `set -e` on purpose: the PostgreSQL
# entrypoint *sources* init scripts that are not executable, and in that case
# either one would abort the whole initialization instead of just this file.

if [ -z "${POSTGRES_TEST_DB:-}" ]; then
    echo "init-test-database: POSTGRES_TEST_DB is unset, nothing to do"
elif [ "${POSTGRES_TEST_DB}" = "${POSTGRES_DB}" ]; then
    echo "init-test-database: POSTGRES_TEST_DB equals POSTGRES_DB, refusing" >&2
    false
else
    echo "init-test-database: creating ${POSTGRES_TEST_DB}"
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres <<-SQL
	CREATE DATABASE "${POSTGRES_TEST_DB}" OWNER "${POSTGRES_USER}";
	SQL
fi
