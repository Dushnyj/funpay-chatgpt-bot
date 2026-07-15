from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_backup_has_disk_preflight_for_both_filesystems():
    script = (ROOT / "ops" / "backup.sh").read_text(encoding="utf-8")

    assert "SELECT pg_database_size(current_database())" in script
    assert 'df -Pk "${BACKUP_DIR}"' in script
    assert "POSTGRES_FREE_KIB" in script
    assert "BACKUP_REQUIRED_KIB" in script
    assert "POSTGRES_REQUIRED_KIB" in script
    assert "Insufficient backup filesystem space" in script
    assert "Insufficient PostgreSQL volume space" in script
    assert script.index('install -d -m 0700 "${STAGING_DIR}"') > script.index(
        "Insufficient PostgreSQL volume space"
    )


def test_backup_service_has_bounded_runtime():
    service = (
        ROOT / "ops" / "systemd" / "funpay-backup.service"
    ).read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "TimeoutStartSec=1h" in service


def test_deployment_runbook_requires_dump_restore_through_0021():
    runbook = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "20260713_0015`–`20260715_0021" in runbook
    assert "docker compose stop backend" in runbook
    assert "dropdb --if-exists --force" in runbook
    assert "pg_restore --exit-on-error --single-transaction" in runbook
    assert "git switch --detach FETCH_HEAD" in runbook
    assert "20260715_0021" in runbook
