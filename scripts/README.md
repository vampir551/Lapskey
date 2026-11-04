# Скрипты LapSKey

## install.sh

Полная установка для текущего пользователя.

```bash
./scripts/install.sh
```

Флаги:

| Флаг | Что делает |
|---|---|
| `--no-deps` | не устанавливать пакеты pacman |
| `--no-system` | не трогать `/etc`, группу `input` и udev |
| `--no-service` | не включать и не запускать сервисы |
| `--no-tray` | не устанавливать автозапуск трея |
| `--no-hotkeys` | не регистрировать `F12` / `Shift+F12` |
| `--force-config` | перезаписать `config.toml` (со старым бэкапом) |
| `--force-hotkeys` | перезаписать уже занятые горячие клавиши |
| `--dry-run` | показать действия без изменений |

Скрипт идемпотентен: повторный запуск обновляет приложение, юниты и ярлыки,
сохраняя конфиг и уже назначенные клавиши.

## uninstall.sh

Удаление.

```bash
./scripts/uninstall.sh --yes
```

| Флаг | Что делает |
|---|---|
| `--yes` | без вопроса о подтверждении |
| `--keep-config` | сохранить `~/.config/lapskey` |
| `--keep-system` | не удалять udev-правило, `modules-load` и группу `input` |
| `--remove-deps` | удалить пакеты, поставленные для LapSKey |
| `--dry-run` | показать действия без изменений |

## Полезные команды

```bash
lapskey status
lapskey doctor
lapskey trigger
lapskey trigger --force
systemctl --user restart lapskey
systemctl --user restart lapskey-tray
```
