#!/usr/bin/env bash
#
# Removes LapSKey from the current user's account.
# System-wide changes (udev rule, modules-load.d, input group) are reverted
# only when the installer created them and only without --keep-system.
#
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lapskey"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lapskey"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/lapskey"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
STATE_FILE="$STATE_DIR/install.env"

KEEP_CONFIG=0
KEEP_SYSTEM=0
REMOVE_DEPS=0
ASSUME_YES=0
DRY_RUN=0

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
info() { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()   { printf '%s  OK%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%sWARN%s %s\n' "$YELLOW" "$OFF" "$*"; }
run()  { if (( DRY_RUN )); then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

usage() {
    cat <<EOF
Usage: $0 [options]

  --yes           do not ask for confirmation
  --keep-config   keep ~/.config/lapskey
  --keep-system   keep udev rule, modules-load entry and group membership
  --remove-deps   also remove pacman packages installed for LapSKey
  --dry-run       show actions without changing anything
  -h, --help      show this help
EOF
}

while (($#)); do
    case "$1" in
        --yes|-y) ASSUME_YES=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        --keep-system) KEEP_SYSTEM=1 ;;
        --remove-deps) REMOVE_DEPS=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [[ $EUID -eq 0 ]]; then
    echo "Запустите от обычного пользователя, не от root." >&2
    exit 1
fi

ADDED_INPUT_GROUP=0
WROTE_UDEV=0
WROTE_MODULES=0
if [[ -f "$STATE_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
    ADDED_INPUT_GROUP="${ADDED_INPUT_GROUP:-0}"
    WROTE_UDEV="${WROTE_UDEV:-0}"
    WROTE_MODULES="${WROTE_MODULES:-0}"
fi

if (( ! ASSUME_YES )); then
    echo "Будет удалено:"
    echo "  - службы lapskey.service и lapskey-tray.service"
    echo "  - приложение $APP_DIR"
    echo "  - команды $BIN_DIR/lapskey, $BIN_DIR/lapskey-force"
    echo "  - ярлыки LapSKey"
    echo "  - привязки F12 / Shift+F12 в KDE"
    if (( ! KEEP_CONFIG )); then
        echo "  - конфигурация $CONFIG_DIR"
    fi
    if (( ! KEEP_SYSTEM )); then
        (( WROTE_UDEV )) && echo "  - /etc/udev/rules.d/70-lapskey.rules"
        (( WROTE_MODULES )) && echo "  - /etc/modules-load.d/lapskey.conf"
        (( ADDED_INPUT_GROUP )) && echo "  - членство $USER в группе input"
    fi
    read -r -p "Продолжить? [y/N] " answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo "Отменено."; exit 0 ;;
    esac
fi

# ------------------------------------------------------------------- services
info "останавливаю службы"
run systemctl --user stop lapskey-tray.service 2>/dev/null || true
run systemctl --user disable lapskey-tray.service 2>/dev/null || true
run systemctl --user stop lapskey.service 2>/dev/null || true
run systemctl --user disable lapskey.service 2>/dev/null || true
run rm -f "$UNIT_DIR/lapskey-tray.service" "$UNIT_DIR/lapskey.service"
run systemctl --user daemon-reload
run systemctl --user reset-failed lapskey.service lapskey-tray.service 2>/dev/null || true
ok "службы остановлены"

# --------------------------------------------------------------------- files
info "удаляю файлы"
run rm -f "$BIN_DIR/lapskey" "$BIN_DIR/lapskey-force"
run rm -rf "$APP_DIR"
run rm -f "$DESKTOP_DIR/lapskey.desktop" \
         "$DESKTOP_DIR/lapskey-tray.desktop" \
         "$DESKTOP_DIR/net.local.lapskey.desktop" \
         "$DESKTOP_DIR/net.local.lapskey-force.desktop"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
run rm -f "$RUNTIME/lapskey.sock" "$RUNTIME/lapskey-tray.lock"
if command -v update-desktop-database >/dev/null 2>&1; then
    run update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    run kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fi
if (( ! KEEP_CONFIG )); then
    run rm -rf "$CONFIG_DIR"
    ok "конфигурация удалена"
else
    warn "конфигурация сохранена: $CONFIG_DIR"
fi
ok "файлы удалены"

# --------------------------------------------------------------------- hotkeys
if command -v gdbus >/dev/null 2>&1; then
    info "снимаю горячие клавиши LapSKey"
    clear_shortcut() {
        local component="$1" name="$2" arg
        arg="['$component', '_launch', '$name', '$name']"
        run gdbus call --session --dest org.kde.kglobalaccel \
            --object-path /kglobalaccel \
            --method org.kde.KGlobalAccel.setForeignShortcut \
            "$arg" "[]" >/dev/null 2>&1 || true
    }
    clear_shortcut "net.local.lapskey.desktop" "LapSKey"
    clear_shortcut "net.local.lapskey-force.desktop" "LapSKey Force"
    ok "привязки сняты"
else
    warn "gdbus не найден — проверьте привязки в настройках KDE"
fi

# --------------------------------------------------------------------- system
if (( ! KEEP_SYSTEM )); then
    if command -v sudo >/dev/null; then
        if (( WROTE_UDEV )) || [[ -f /etc/udev/rules.d/70-lapskey.rules ]]; then
            info "удаляю udev-правило"
            run sudo rm -f /etc/udev/rules.d/70-lapskey.rules
            run sudo udevadm control --reload-rules 2>/dev/null || true
        fi
        if (( WROTE_MODULES )) || [[ -f /etc/modules-load.d/lapskey.conf ]]; then
            info "удаляю автозагрузку модуля uinput"
            run sudo rm -f /etc/modules-load.d/lapskey.conf
        fi
        if (( ADDED_INPUT_GROUP )); then
            info "удаляю $USER из группы input"
            run sudo gpasswd -d "$USER" input >/dev/null 2>&1 || true
            warn "нужен повторный вход, чтобы изменение группы применилось"
        fi
    else
        warn "sudo не найден: системные изменения не откатаны"
    fi
else
    warn "системные изменения сохранены (--keep-system)"
fi

if (( REMOVE_DEPS )); then
    if command -v pacman >/dev/null; then
        warn "удаляю пакеты, установленные для LapSKey"
        run sudo pacman -Rns --noconfirm python-evdev python-pyqt6 wl-clipboard \
            libnotify hunspell hunspell-en_us hunspell-ru
    else
        warn "pacman не найден"
    fi
fi

run rm -rf "$STATE_DIR"
ok "LapSKey удалён."
