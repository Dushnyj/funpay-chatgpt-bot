#!/bin/sh
set -eu

AUTH_DIR=/var/lib/funpay-relay/auth
HOST_DIR=/var/lib/funpay-relay/host
GENERATION_FILE="${AUTH_DIR}/session_generation"
ACK_FILE="${AUTH_DIR}/session_generation.ack"

install -d -m 0700 -o relay -g relay "${AUTH_DIR}"
touch "${AUTH_DIR}/authorized_keys"
chown relay:relay "${AUTH_DIR}/authorized_keys"
chmod 0600 "${AUTH_DIR}/authorized_keys"

install -d -m 0700 "${HOST_DIR}"
if [ ! -s "${HOST_DIR}/ssh_host_ed25519_key" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "${HOST_DIR}/ssh_host_ed25519_key"
fi
chmod 0600 "${HOST_DIR}/ssh_host_ed25519_key"
chmod 0644 "${HOST_DIR}/ssh_host_ed25519_key.pub"

# The backend only needs the public host key to pin the first Windows
# connection. The private host key never leaves the sidecar-only volume.
install -m 0644 "${HOST_DIR}/ssh_host_ed25519_key.pub" \
    "${AUTH_DIR}/ssh_host_ed25519_key.pub"
chown relay:relay "${AUTH_DIR}/ssh_host_ed25519_key.pub"

# sshd validates authorized_keys only when a connection is established. Merely
# removing a key would leave an already authenticated reverse tunnel alive.
# The backend therefore rotates a generation file after every key replace or
# revoke. This watcher terminates every active relay child and acknowledges the
# exact generation before the API is allowed to report success.
terminate_relay_sessions() {
    children_file="/proc/${SSHD_PID}/task/${SSHD_PID}/children"
    children=""
    if [ -r "${children_file}" ]; then
        children="$(cat "${children_file}" 2>/dev/null || true)"
    fi
    for child in ${children}; do
        kill -TERM "${child}" 2>/dev/null || true
    done

    # Do not acknowledge revocation while a captured session is still alive.
    attempts=0
    while [ "${attempts}" -lt 20 ]; do
        alive=0
        for child in ${children}; do
            if kill -0 "${child}" 2>/dev/null; then
                alive=1
            fi
        done
        [ "${alive}" -eq 0 ] && return 0
        attempts=$((attempts + 1))
        sleep 0.05
    done
    for child in ${children}; do
        kill -KILL "${child}" 2>/dev/null || true
    done

    # A SIGKILL request is asynchronous.  Do not publish the generation ACK
    # until sshd has reaped every captured session child; otherwise the API
    # could install a replacement key while the revoked tunnel is still alive
    # for a short scheduling window.
    attempts=0
    while [ "${attempts}" -lt 20 ]; do
        alive=0
        for child in ${children}; do
            if kill -0 "${child}" 2>/dev/null; then
                alive=1
            fi
        done
        [ "${alive}" -eq 0 ] && return 0
        attempts=$((attempts + 1))
        sleep 0.05
    done
    return 1
}

watch_session_generation() {
    last_generation=""
    while kill -0 "${SSHD_PID}" 2>/dev/null; do
        generation=""
        if [ -r "${GENERATION_FILE}" ]; then
            generation="$(sed -n '1p' "${GENERATION_FILE}" 2>/dev/null || true)"
        fi
        case "${generation}" in
            ""|*[!A-Za-z0-9_-]*) ;;
            *)
                if [ "${generation}" != "${last_generation}" ]; then
                    # A failed drain is fatal: without a trustworthy ACK the
                    # backend must keep the route disabled and Compose must
                    # restart this sidecar together with its watcher.
                    terminate_relay_sessions || return 1
                    temporary_ack="${ACK_FILE}.$$.tmp"
                    printf '%s\n' "${generation}" > "${temporary_ack}"
                    chown relay:relay "${temporary_ack}"
                    chmod 0600 "${temporary_ack}"
                    mv -f "${temporary_ack}" "${ACK_FILE}"
                    last_generation="${generation}"
                fi
                ;;
        esac
        sleep 0.2
    done
}

supervise_session_watcher() {
    # A live sshd without the revocation watcher would violate the key-removal
    # contract. Fail the container so Compose restarts both together.
    set +e
    watch_session_generation
    watcher_status=$?
    if kill -0 "${SSHD_PID}" 2>/dev/null; then
        kill -TERM "${SSHD_PID}" 2>/dev/null || true
    fi
    return "${watcher_status}"
}

/usr/sbin/sshd -D -e -f /etc/ssh/sshd_config &
SSHD_PID=$!
supervise_session_watcher &
WATCHER_PID=$!

forward_signal() {
    kill -TERM "${SSHD_PID}" 2>/dev/null || true
}
trap forward_signal INT TERM

status=0
wait "${SSHD_PID}" || status=$?
kill -TERM "${WATCHER_PID}" 2>/dev/null || true
wait "${WATCHER_PID}" 2>/dev/null || true
exit "${status}"
