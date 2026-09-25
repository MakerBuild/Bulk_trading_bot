#!/usr/bin/env bash
# Run Telegram control as a systemd service, so the bot is listening again
# after the server reboots -- no one has to log in and start it.
#
#   sudo ./service.sh install     set it up and start it
#   sudo ./service.sh uninstall   stop it and remove it
#   ./service.sh status          is it running
#   ./service.sh logs            follow its output (Ctrl+C to leave)
#   sudo ./service.sh restart     restart it, e.g. after ./update.sh
#
# The service only LISTENS. It starts no run on its own: a run begins when
# someone presses Run in Telegram, exactly as when control is started by hand.
#
# The price is the key password. A service has no one to type it, so it is
# kept in /etc/bulkdn/bulkdn.env, readable by root only -- which makes this
# server a hot wallet. Keep on it only what the bot needs to trade.

UNIT_NAME="bulkdn-telegram"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"
ENV_DIR="/etc/bulkdn"
ENV_FILE="${ENV_DIR}/bulkdn.env"

main() {
    cd "$(dirname "$0")" || exit 1
    local dir
    dir="$(pwd -P)"

    say() { printf '  %s\n' "$@"; }
    fail() { echo; say "$@"; echo; exit 1; }
    need_root() {
        [ "$(id -u)" -eq 0 ] || fail "Run this one with sudo:  sudo ./service.sh $1"
    }

    case "${1:-}" in
        install)
            need_root install
            command -v systemctl >/dev/null 2>&1 || fail "This system has no systemd."
            [ -x "$dir/app/.venv/bin/python" ] || fail "Not installed yet. Run ./install.sh first."
            [ -f "$dir/private_key.local" ] || fail "No private_key.local -- put your key in first."

            # The account that owns the bot's folder runs it -- not root.
            local owner
            owner="$(stat -c '%U' "$dir")"

            echo
            say "The key password is stored in $ENV_FILE (root only), so the" \
                "service can start without anyone typing it. That makes this server" \
                "a hot wallet: anyone who gets root here can use the key." ""
            local password=""
            read -rsp "  Key password (Enter for none or the default): " password
            echo
            local live=""
            local answer=""
            read -rp "  Start in LIVE mode? Runs still need Start in Telegram. [y/N] " answer
            case "$answer" in
                y|Y|yes|YES) live="--live" ;;
            esac

            mkdir -p "$ENV_DIR"
            chmod 700 "$ENV_DIR"
            # systemd reads double-quoted values with backslash escapes, and
            # does not expand $ in an EnvironmentFile.
            local escaped="${password//\\/\\\\}"
            escaped="${escaped//\"/\\\"}"
            umask 077
            printf 'BULK_KEY_PASSWORD="%s"\n' "$escaped" > "$ENV_FILE"
            chmod 600 "$ENV_FILE"

            cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=BULK bot -- Telegram control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${owner}
WorkingDirectory=${dir}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${dir}/run.sh telegram ${live}
# A crash restarts the listener -- not a run. Exit status 1 is a setup
# problem (Telegram not configured, a bad settings file, another copy
# polling the token), which restarting every 30 seconds would not fix.
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=1
# Ctrl+C, in effect: a run in progress is asked to stop and given time to.
# The bot then exits with 130, which is how a stop looks, not a failure.
KillSignal=SIGINT
TimeoutStopSec=150
SuccessExitStatus=130

[Install]
WantedBy=multi-user.target
UNIT
            systemctl daemon-reload
            systemctl enable --now "$UNIT_NAME"
            echo
            say "Installed and started (${live:-dry run}). It starts again by itself after a reboot." \
                "Check it:  ./service.sh status     Follow it:  ./service.sh logs"
            ;;
        uninstall)
            need_root uninstall
            systemctl disable --now "$UNIT_NAME" 2>/dev/null
            rm -f "$UNIT_FILE"
            rm -rf "$ENV_DIR"
            systemctl daemon-reload
            say "Removed, and the stored password deleted."
            ;;
        restart)
            need_root restart
            systemctl restart "$UNIT_NAME"
            say "Restarted."
            ;;
        status)
            systemctl status "$UNIT_NAME" --no-pager
            ;;
        logs)
            journalctl -u "$UNIT_NAME" -f
            ;;
        *)
            say "Usage:" \
                "  sudo ./service.sh install     set it up and start it" \
                "  sudo ./service.sh uninstall   stop it and remove it" \
                "  ./service.sh status          is it running" \
                "  ./service.sh logs            follow its output" \
                "  sudo ./service.sh restart     restart it, e.g. after ./update.sh"
            exit 1
            ;;
    esac
}

main "$@"
exit $?
