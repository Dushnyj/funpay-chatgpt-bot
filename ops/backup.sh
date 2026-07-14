#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

PROJECT_DIR="${FUNPAY_PROJECT_DIR:-/opt/funpay-chatgpt-bot}"
BACKUP_DIR="${FUNPAY_BACKUP_DIR:-/opt/backups/funpay/daily}"
RETENTION_DAYS="${FUNPAY_BACKUP_RETENTION_DAYS:-14}"
LOCK_FILE="${FUNPAY_BACKUP_LOCK_FILE:-/run/lock/funpay-backup.lock}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING_DIR="${BACKUP_DIR}/.${STAMP}.tmp"
FINAL_DIR="${BACKUP_DIR}/${STAMP}"
VERIFY_DB="funpay_backup_verify_${STAMP,,}"

case "${RETENTION_DAYS}" in
  ''|*[!0-9]*) echo "FUNPAY_BACKUP_RETENTION_DAYS must be an integer" >&2; exit 2 ;;
esac

test -d "${PROJECT_DIR}"
test -f "${PROJECT_DIR}/docker-compose.yml"
install -d -m 0755 "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "Another FunPay backup is already running" >&2
  exit 75
fi
install -d -m 0700 "${BACKUP_DIR}"

cd "${PROJECT_DIR}"

# Verification temporarily restores a second copy of the database into the
# live PostgreSQL volume. Refuse to begin unless both filesystems have enough
# conservative headroom for the dump, restored relations, indexes and WAL.
DATABASE_SIZE_BYTES="$(docker compose exec -T postgres sh -eu -c \
  'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT pg_database_size(current_database())"')"
BACKUP_FREE_KIB="$(df -Pk "${BACKUP_DIR}" | awk 'NR == 2 {print $4}')"
POSTGRES_FREE_KIB="$(docker compose exec -T postgres sh -eu -c \
  'df -Pk "${PGDATA:-/var/lib/postgresql/data}" | awk "NR == 2 {print \$4}"')"
for value_name in DATABASE_SIZE_BYTES BACKUP_FREE_KIB POSTGRES_FREE_KIB; do
  value="${!value_name}"
  case "${value}" in
    ''|*[!0-9]*) echo "Unable to determine ${value_name} for backup preflight" >&2; exit 1 ;;
  esac
done
DATABASE_SIZE_KIB=$(( (DATABASE_SIZE_BYTES + 1023) / 1024 ))
BACKUP_REQUIRED_KIB=$(( DATABASE_SIZE_KIB + 128 * 1024 ))
POSTGRES_REQUIRED_KIB=$(( DATABASE_SIZE_KIB * 2 + 512 * 1024 ))
if (( BACKUP_FREE_KIB < BACKUP_REQUIRED_KIB )); then
  echo "Insufficient backup filesystem space: ${BACKUP_FREE_KIB} KiB available, ${BACKUP_REQUIRED_KIB} KiB required" >&2
  exit 1
fi
if (( POSTGRES_FREE_KIB < POSTGRES_REQUIRED_KIB )); then
  echo "Insufficient PostgreSQL volume space: ${POSTGRES_FREE_KIB} KiB available, ${POSTGRES_REQUIRED_KIB} KiB required" >&2
  exit 1
fi

install -d -m 0700 "${STAGING_DIR}"
VERIFY_DB_CREATED=0
cleanup() {
  set +e
  if test "${VERIFY_DB_CREATED}" -eq 1; then
    docker compose exec -T postgres sh -eu -c \
      'dropdb --if-exists --force --username="$POSTGRES_USER" "$1"' \
      sh "${VERIFY_DB}" > /dev/null 2>&1
  fi
  rm -rf -- "${STAGING_DIR}"
}
trap cleanup EXIT

docker compose exec -T postgres sh -eu -c \
  'pg_dump --format=custom --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  > "${STAGING_DIR}/database.dump"
test -s "${STAGING_DIR}/database.dump"
docker compose exec -T postgres pg_restore --list \
  < "${STAGING_DIR}/database.dump" > /dev/null

# A TOC listing alone does not read every compressed data block. Restore the
# dump into an isolated temporary database and run a smoke query before the
# backup is published.
docker compose exec -T postgres sh -eu -c \
  'dropdb --if-exists --force --username="$POSTGRES_USER" "$1"; createdb --username="$POSTGRES_USER" "$1"' \
  sh "${VERIFY_DB}"
VERIFY_DB_CREATED=1
docker compose exec -T postgres sh -eu -c \
  'pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$1"' \
  sh "${VERIFY_DB}" < "${STAGING_DIR}/database.dump"
TABLE_COUNT="$(docker compose exec -T postgres sh -eu -c \
  'psql --username="$POSTGRES_USER" --dbname="$1" --tuples-only --no-align --command="SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\''"' \
  sh "${VERIFY_DB}")"
case "${TABLE_COUNT}" in
  ''|*[!0-9]*|0) echo "Restored backup has no public tables" >&2; exit 1 ;;
esac
docker compose exec -T postgres sh -eu -c \
  'dropdb --if-exists --force --username="$POSTGRES_USER" "$1"' \
  sh "${VERIFY_DB}" > /dev/null
VERIFY_DB_CREATED=0

git bundle create "${STAGING_DIR}/repository.bundle" --all
git bundle verify "${STAGING_DIR}/repository.bundle" > /dev/null

if test -f .env; then
  install -m 0600 .env "${STAGING_DIR}/environment.backup"
fi

(
  cd "${STAGING_DIR}"
  checksum_files=(database.dump repository.bundle)
  if test -f environment.backup; then
    checksum_files+=(environment.backup)
  fi
  sha256sum "${checksum_files[@]}" > SHA256SUMS
  sha256sum --check SHA256SUMS > /dev/null
)

mv "${STAGING_DIR}" "${FINAL_DIR}"
trap - EXIT

# Remove old files first and then their empty timestamp directories. This
# avoids a broad recursive delete while retaining a bounded local history.
find "${BACKUP_DIR}" -mindepth 2 -maxdepth 2 -type f \
  -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -empty -delete

printf 'Verified backup created: %s\n' "${FINAL_DIR}"
