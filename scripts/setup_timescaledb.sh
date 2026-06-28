#!/usr/bin/env bash
# One-time TimescaleDB bring-up for grow-automation's trend store.
# Idempotent: safe to re-run. Must run as root (sudo bash setup_timescaledb.sh).
#
#   sudo bash ~/Projects/grow-automation/scripts/setup_timescaledb.sh
#
# Installs postgresql + timescaledb + python-psycopg, initializes the cluster,
# enables the timescaledb preload, starts the service, and creates a login role
# matching the invoking desktop user (peer/trust auth over the unix socket -- no
# password) plus a `grow` database owning the trend tables, with the extension.
set -euo pipefail

PGDATA=/var/lib/postgres/data
APP_DB=grow
# The Linux user who owns the grow-automation checkout (passed through sudo).
APP_ROLE="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"

echo ">> app role: $APP_ROLE   db: $APP_DB   pgdata: $PGDATA"

echo ">> [1/6] installing packages"
pacman -S --needed --noconfirm postgresql timescaledb python-psycopg

echo ">> [2/6] initializing cluster (if absent)"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  install -d -o postgres -g postgres "$PGDATA"
  sudo -u postgres initdb -D "$PGDATA" --encoding=UTF8
else
  echo "   cluster already initialized -- skipping"
fi

echo ">> [3/6] enabling timescaledb preload"
CONF="$PGDATA/postgresql.conf"
if ! grep -qE "^[[:space:]]*shared_preload_libraries[[:space:]]*=[[:space:]]*'timescaledb'" "$CONF"; then
  sed -i "/^[[:space:]]*#\?[[:space:]]*shared_preload_libraries[[:space:]]*=/d" "$CONF"
  echo "shared_preload_libraries = 'timescaledb'" >> "$CONF"
  echo "   set shared_preload_libraries = 'timescaledb'"
else
  echo "   already set -- skipping"
fi

echo ">> [4/6] enabling + starting service"
systemctl enable postgresql
systemctl restart postgresql
for _ in $(seq 1 40); do sudo -u postgres pg_isready -q && break; sleep 0.5; done
sudo -u postgres pg_isready

echo ">> [5/6] creating role + database (idempotent)"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_ROLE'" | grep -q 1; then
  sudo -u postgres createuser --login --createdb "$APP_ROLE"
  echo "   created role $APP_ROLE"
else
  echo "   role $APP_ROLE exists"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$APP_DB'" | grep -q 1; then
  sudo -u postgres createdb -O "$APP_ROLE" "$APP_DB"
  echo "   created db $APP_DB"
else
  echo "   db $APP_DB exists"
fi

echo ">> [6/6] enabling timescaledb extension in $APP_DB"
sudo -u postgres psql -d "$APP_DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "SETUP_OK timescaledb=$(sudo -u postgres psql -d "$APP_DB" -tAc "SELECT extversion FROM pg_extension WHERE extname='timescaledb';")"
