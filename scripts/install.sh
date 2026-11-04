#!/usr/bin/env bash
#
# Installs LapSKey for KDE Plasma / Wayland into the current user's home.
# System-wide changes (udev rule, module autoload, group membership) need sudo
# and can be skipped with --no-system.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lapskey"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lapskey"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/lapskey"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
CONFIG_FILE="$CONFIG_DIR/config.toml"
STATE_FILE="$STATE_DIR/install.env"

INSTALL_DEPS=1
SYSTEM_SETUP=1
ENABLE_SERVICE=1
INSTALL_TRAY=1
INSTALL_HOTKEYS=1
FORCE_CONFIG=0
FORCE_HOTKEYS=0
DRY_RUN=0

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
info()  { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()    { printf '%s  OK%s %s\n' "$GREEN" "$OFF" "$*"; }
warn()  { printf '%sWARN%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()   { printf '%sFAIL%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
run()   { if (( DRY_RUN )); then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

usage() {
    cat <<EOF
Usage: $0 [options]

  --no-deps       do not install pacman packages
  --no-system     do not touch /etc (udev, modules-load.d, usermod)
  --no-service    install files but do not enable/start services
  --no-tray       do not install/enable the tray service
  --no-hotkeys    do not register KDE shortcuts
  --force-config  overwrite an existing config.toml (with backup)
  --force-hotkeys overwrite existing KDE shortcuts
  --dry-run       show actions without changing anything
  -h, --help      show this help
EOF
}

while (($#)); do
    case "$1" in
        --no-deps) INSTALL_DEPS=0 ;;
        --no-system) SYSTEM_SETUP=0 ;;
        --no-service) ENABLE_SERVICE=0 ;;
        --no-tray) INSTALL_TRAY=0 ;;
        --no-hotkeys) INSTALL_HOTKEYS=0 ;;
        --force-config) FORCE_CONFIG=1 ;;
        --force-hotkeys) FORCE_HOTKEYS=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

if [[ $EUID -eq 0 ]]; then
    die "запустите скрипт от обычного пользователя, не от root"
fi
command -v python3 >/dev/null || die "python3 не найден"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "нужен Python 3.11+ (модуль tomllib)"
PYTHON_BIN="$(command -v python3)"
[[ -d "$PROJECT_DIR/lapskey" ]] || die "не найден каталог lapskey рядом со скриптом"

info "LapSKey: установка в $APP_DIR"
(( DRY_RUN )) && warn "режим --dry-run: изменения не применяются"

# --------------------------------------------------------------- dependencies
if (( INSTALL_DEPS )); then
    if command -v pacman >/dev/null; then
        DEPS=(python-evdev python-pyqt6 wl-clipboard libnotify hunspell hunspell-en_us hunspell-ru)
        MISSING=()
        for pkg in "${DEPS[@]}"; do
            pacman -Qq "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
        done
        if ((${#MISSING[@]})); then
            info "устанавливаю пакеты: ${MISSING[*]}"
            run sudo pacman -S --needed --noconfirm "${MISSING[@]}"
        else
            ok "все пакеты уже установлены"
        fi
    else
        warn "pacman не найден — установите зависимости вручную (см. README)"
    fi
else
    warn "установка зависимостей пропущена (--no-deps)"
fi

# --------------------------------------------------------------- system setup
ADDED_GROUP=0
WROTE_UDEV=0
WROTE_MODULES=0
NEED_RELOGIN=0

if (( SYSTEM_SETUP )); then
    if command -v sudo >/dev/null; then
        if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
            ok "пользователь уже входит в группу input"
        else
            info "добавляю $USER в группу input (нужен повторный вход)"
            run sudo usermod -aG input "$USER"
            ADDED_GROUP=1
            NEED_RELOGIN=1
        fi

        if [[ ! -f /etc/modules-load.d/lapskey.conf ]]; then
            info "включаю автозагрузку модуля uinput"
            if (( DRY_RUN )); then
                printf '  [dry-run] write /etc/modules-load.d/lapskey.conf\n'
            else
                printf 'uinput\n' | sudo tee /etc/modules-load.d/lapskey.conf >/dev/null
            fi
            WROTE_MODULES=1
        fi
        run sudo modprobe uinput 2>/dev/null || true

        if [[ ! -f /etc/udev/rules.d/70-lapskey.rules ]]; then
            info "устанавливаю udev-правило для /dev/uinput"
            if (( DRY_RUN )); then
                printf '  [dry-run] write /etc/udev/rules.d/70-lapskey.rules\n'
            else
                sudo tee /etc/udev/rules.d/70-lapskey.rules >/dev/null <<'RULE'
# LapSKey: let the input group create a virtual keyboard
KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
RULE
            fi
            WROTE_UDEV=1
        fi
        run sudo udevadm control --reload-rules 2>/dev/null || true
        run sudo udevadm trigger --name-match=uinput 2>/dev/null || true
        if [[ -e /dev/uinput ]] && (( ! DRY_RUN )); then
            sudo chgrp input /dev/uinput 2>/dev/null || true
            sudo chmod 0660 /dev/uinput 2>/dev/null || true
        fi
    else
        warn "sudo не найден — системные настройки пропущены"
    fi
else
    warn "системные настройки пропущены (--no-system)"
fi

# ------------------------------------------------------------------ app files
info "копирую файлы приложения"
run mkdir -p "$APP_DIR" "$BIN_DIR" "$CONFIG_DIR" "$UNIT_DIR" "$DESKTOP_DIR" "$STATE_DIR"

if (( ! DRY_RUN )); then
    rm -rf "$APP_DIR/app.new"
    mkdir -p "$APP_DIR/app.new"
    cp -a "$PROJECT_DIR/lapskey" "$APP_DIR/app.new/"
    cp -a "$PROJECT_DIR/README.md" "$APP_DIR/app.new/" 2>/dev/null || true
    rm -rf "$APP_DIR/app.old"
    [[ -d "$APP_DIR/app" ]] && mv "$APP_DIR/app" "$APP_DIR/app.old"
    mv "$APP_DIR/app.new" "$APP_DIR/app"
    rm -rf "$APP_DIR/app.old"
    find "$APP_DIR/app" -type d -name __pycache__ -prune -exec rm -rf {} +
else
    printf '  [dry-run] copy %s/lapskey -> %s/app\n' "$PROJECT_DIR" "$APP_DIR"
fi

if (( ! DRY_RUN )); then
    cat > "$BIN_DIR/lapskey" <<EOF
#!/bin/sh
export PYTHONPATH="$APP_DIR/app\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON_BIN" -m lapskey "\$@"
EOF
    chmod +x "$BIN_DIR/lapskey"
    cat > "$BIN_DIR/lapskey-force" <<EOF
#!/bin/sh
exec "$BIN_DIR/lapskey" trigger --force
EOF
    chmod +x "$BIN_DIR/lapskey-force"
else
    printf '  [dry-run] write %s/lapskey and lapskey-force\n' "$BIN_DIR"
fi
ok "команда: $BIN_DIR/lapskey (и lapskey-force)"

# --------------------------------------------------------------------- config
if [[ ! -f "$CONFIG_FILE" ]]; then
    run mkdir -p "$CONFIG_DIR"
    run cp "$PROJECT_DIR/config/config.toml" "$CONFIG_FILE"
    ok "конфигурация: $CONFIG_FILE"
elif (( FORCE_CONFIG )); then
    BACKUP="$CONFIG_FILE.bak-$(date +%Y%m%d-%H%M%S)"
    info "сохраняю старый конфиг: $BACKUP"
    run cp "$CONFIG_FILE" "$BACKUP"
    run cp "$PROJECT_DIR/config/config.toml" "$CONFIG_FILE"
    ok "конфигурация перезаписана"
else
    ok "конфигурация сохранена: $CONFIG_FILE"
fi

# -------------------------------------------------------------------- systemd
info "устанавливаю systemd user services"
run install -m 0644 "$PROJECT_DIR/systemd/lapskey.service" "$UNIT_DIR/lapskey.service"
if (( INSTALL_TRAY )); then
    run install -m 0644 "$PROJECT_DIR/systemd/lapskey-tray.service" "$UNIT_DIR/lapskey-tray.service"
fi
run systemctl --user daemon-reload

if (( ENABLE_SERVICE )); then
    if (( DRY_RUN )); then
        printf '  [dry-run] systemctl --user enable --now lapskey.service\n'
        (( INSTALL_TRAY )) && printf '  [dry-run] systemctl --user enable --now lapskey-tray.service\n'
    else
        if systemctl --user enable lapskey.service && systemctl --user restart lapskey.service; then
            ok "демон включён и запущен"
        else
            warn "не удалось запустить демон: journalctl --user -u lapskey -n 50"
        fi
        if (( INSTALL_TRAY )); then
            if python3 -c 'import PyQt6' >/dev/null 2>&1; then
                if systemctl --user enable lapskey-tray.service && systemctl --user restart lapskey-tray.service; then
                    ok "значок в трее запущен"
                else
                    warn "не удалось запустить трей: journalctl --user -u lapskey-tray -n 50"
                fi
            else
                warn "python-pyqt6 не установлен — трей не будет запущен"
            fi
        fi
    fi
else
    warn "автозапуск не включён (--no-service)"
fi

# -------------------------------------------------------------------- desktop
info "устанавливаю ярлыки"
if (( ! DRY_RUN )); then
    sed "s|/usr/bin/env lapskey|$BIN_DIR/lapskey|g" \
        "$PROJECT_DIR/desktop/lapskey.desktop" > "$DESKTOP_DIR/lapskey.desktop"
    sed "s|/usr/bin/env lapskey|$BIN_DIR/lapskey|g" \
        "$PROJECT_DIR/desktop/lapskey-tray.desktop" > "$DESKTOP_DIR/lapskey-tray.desktop"
    sed "s|/usr/bin/env lapskey|$BIN_DIR/lapskey|g" \
        "$PROJECT_DIR/desktop/lapskey-command.desktop" > "$DESKTOP_DIR/net.local.lapskey.desktop"
    sed "s|/usr/bin/env lapskey-force|$BIN_DIR/lapskey-force|g" \
        "$PROJECT_DIR/desktop/lapskey-force.desktop" > "$DESKTOP_DIR/net.local.lapskey-force.desktop"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    fi
    if command -v kbuildsycoca6 >/dev/null 2>&1; then
        kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    fi
else
    printf '  [dry-run] install desktop entries: lapskey*.desktop, net.local.lapskey*.desktop\n'
fi
ok "ярлыки установлены"

# -------------------------------------------------------------------- hotkeys
HOTKEYS_SET=0
F12=16777275
SHIFT_F12=50331707

kde_available() {
    command -v gdbus >/dev/null 2>&1 \
        && gdbus call --session --dest org.kde.kglobalaccel \
            --object-path /kglobalaccel \
            --method org.kde.KGlobalAccel.allComponents >/dev/null 2>&1
}

shortcut_arg() {
    printf "['%s', '_launch', '%s', '%s']" "$1" "$2" "$2"
}

register_shortcut() {
    local component="$1" name="$2" key="$3" arg raw
    arg="$(shortcut_arg "$component" "$name")"
    raw="$(gdbus call --session --dest org.kde.kglobalaccel \
        --object-path /kglobalaccel \
        --method org.kde.KGlobalAccel.shortcut "$arg" 2>/dev/null || true)"
    if (( ! FORCE_HOTKEYS )) && [[ "$raw" =~ [0-9]{4,} ]]; then
        ok "клавиша уже назначена: $name"
        return 0
    fi
    gdbus call --session --dest org.kde.kglobalaccel \
        --object-path /kglobalaccel \
        --method org.kde.KGlobalAccel.doRegister "$arg" >/dev/null 2>&1 || true
    if gdbus call --session --dest org.kde.kglobalaccel \
        --object-path /kglobalaccel \
        --method org.kde.KGlobalAccel.setForeignShortcut "$arg" "[$key]" >/dev/null 2>&1; then
        ok "назначено: $name"
        HOTKEYS_SET=1
    else
        warn "не удалось назначить $name; назначьте вручную в KDE"
    fi
}

if (( INSTALL_HOTKEYS )); then
    if (( DRY_RUN )); then
        printf '  [dry-run] register F12 and Shift+F12 in KDE\n'
    elif kde_available; then
        info "настраиваю горячие клавиши KDE"
        register_shortcut "net.local.lapskey.desktop" "LapSKey" "$F12"
        register_shortcut "net.local.lapskey-force.desktop" "LapSKey Force" "$SHIFT_F12"
    else
        warn "KDE kglobalaccel недоступен — назначьте F12/Shift+F12 вручную"
    fi
else
    warn "регистрация горячих клавиш пропущена (--no-hotkeys)"
fi

# ---------------------------------------------------------------------- state
if (( ! DRY_RUN )); then
    cat > "$STATE_FILE" <<EOF
ADDED_INPUT_GROUP=$ADDED_GROUP
WROTE_UDEV=$WROTE_UDEV
WROTE_MODULES=$WROTE_MODULES
APP_DIR=$APP_DIR
BIN_DIR=$BIN_DIR
UNIT_FILE=$UNIT_DIR/lapskey.service
TRAY_UNIT_FILE=$UNIT_DIR/lapskey-tray.service
DESKTOP_FILE=$DESKTOP_DIR/lapskey.desktop
TRAY_DESKTOP_FILE=$DESKTOP_DIR/lapskey-tray.desktop
COMMAND_DESKTOP_FILE=$DESKTOP_DIR/net.local.lapskey.desktop
FORCE_DESKTOP_FILE=$DESKTOP_DIR/net.local.lapskey-force.desktop
CONFIG_FILE=$CONFIG_FILE
HOTKEYS_SET=$HOTKEYS_SET
EOF
fi

# --------------------------------------------------------------------- checks
if (( ENABLE_SERVICE )) && (( ! DRY_RUN )); then
    sleep 1
    if systemctl --user is-active --quiet lapskey.service; then
        ok "демон работает"
    else
        warn "демон не активен: journalctl --user -u lapskey -n 50"
    fi
    if (( INSTALL_TRAY )) && systemctl --user is-active --quiet lapskey-tray.service; then
        ok "трей работает"
    fi
    "$BIN_DIR/lapskey" doctor || true
fi

cat <<EOF

${BOLD}LapSKey установлен.${OFF}

  Умное исправление:        F12
  Принудительное:           Shift+F12
  Изменить клавиши:         трей → «Настройки…» → «Клавиши»

  Команды:
    $BIN_DIR/lapskey status
    $BIN_DIR/lapskey doctor
    $BIN_DIR/lapskey trigger
    $BIN_DIR/lapskey trigger --force
    journalctl --user -u lapskey -f

  Перезапуск после правки config.toml:
    systemctl --user restart lapskey
EOF

if (( NEED_RELOGIN )); then
    warn "вы добавлены в группу input — перезайдите в систему, иначе /dev/input"
    warn "и /dev/uinput могут быть недоступны."
fi
