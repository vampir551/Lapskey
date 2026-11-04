# LapSKey для KDE Plasma / Wayland

Фоновая утилита исправляет слово, набранное в неправильной раскладке,
и переключает раскладку KDE. Написана на Python,
рассчитана на Arch Linux, KDE Plasma 6 и Wayland (работает и в X11-сессии
KDE).

```
ghbdtn   -> привет
зштп     -> ping
руддщ    -> hello
nfr      -> так
```

## Быстрый старт

```bash
git clone https://github.com/vampir551/Lapskey.git
cd Lapskey
./scripts/install.sh
```

Или, если проект уже скачан в произвольный каталог:

```bash
cd /путь/к/LapSKey
./scripts/install.sh
```

После установки:

- **F12** — исправить последнее слово или выделение;
- **Shift+F12** — принудительно, без проверки эвристикой;
- в трее — флаг активной раскладки (RU/US), меню и настройки;
- изменить клавиши можно в трее: «Настройки…» → «Клавиши».

Проверка:

```bash
lapskey status
lapskey doctor
lapskey convert "ghbdtn"
```

Удаление:

```bash
./scripts/uninstall.sh
```

---

## 1. Как это работает и почему именно так

### 1.1. Ввод: `/dev/uinput` (python-evdev)

В Wayland приложение не может «нажать клавишу» через X11-протокол, поэтому
`xdotool` неприменим. `wtype` тоже не подходит: он работает только на
wlroots-композиторах (Sway, Hyprland), а KWin — не wlroots.

Выбранный способ — создать собственную виртуальную клавиатуру через
`/dev/uinput`. Для композитора это обычное ядровое устройство ввода: события
доходят до нативных Wayland-приложений, XWayland и терминалов одинаково.
Никакого демона-посредника не нужно (в отличие от `ydotool`).

`ydotool` оставлен как запасной вариант: если `/dev/uinput` недоступен,
демон попробует отправить клавиши через `ydotoold`. Пакет `ydotool` при этом
не обязателен.

### 1.2. Горячая клавиша: KDE Global Shortcuts

Wayland не даёт программам глобально перехватывать клавиши. Правильный путь —
зарегистрировать команду в KDE: **System Settings → Keyboard → Shortcuts →
Add New → Command or Script** и назначить ей `lapskey trigger`.

KDE перехватывает клавишу сам (приложение её не видит) и запускает команду,
которая по Unix-сокету просит демон выполнить исправление. Сокет имеет права
`0600` и лежит в `$XDG_RUNTIME_DIR`.

Вручную это делать не обязательно: установщик сам регистрирует `F12` и
`Shift+F12`, если они свободны. Поменять их можно в трее на вкладке «Клавиши»
без открытия системных настроек KDE.

Дополнительно есть встроенный evdev-хоткей (`hotkey.mode = "evdev"`), но он
только *наблюдает* за `/dev/input`: клавиша доходит и до активного приложения.
Поэтому по умолчанию используется KDE-способ.

### 1.3. Чтение текста: буфер клавиш + PRIMARY + резервный copy

Wayland (и KWin в частности) не позволяет читать содержимое чужих окон.
Поэтому используются три источника, в порядке приоритета:

1. **Буфер последнего слова из evdev.** Демон в реальном времени запоминает
   физические нажатия (клавиша + Shift/CapsLock) и на момент вызова знает
   последнее слово. Раскладка берётся из KDE DBus, поэтому слово
   восстанавливается в тех символах, которые реально видит пользователь.
   Клипборд не трогается вообще.
2. **PRIMARY selection.** Если пользователь только что выделил текст мышью,
   двойным кликом или Shift+стрелками, демон читает `wl-paste --primary`.
   Это не изменяет ни клипборд, ни выделение.
3. **Резервный `Ctrl+Insert` + `wl-paste`.** Если буфер пуст (например, демон
   запустили только что) или приложение не отдаёт PRIMARY, можно скопировать
   выделение/предыдущее слово. Клипборд при этом сохраняется и
   восстанавливается.

Признаки выделения (перетаскивание мышью, двойной клик, Shift+стрелки,
Ctrl+A) отслеживаются тем же evdev-монитором, поэтому «выделение» читается
именно тогда, когда оно действительно есть, а не устаревшее.

### 1.4. Замена текста и переключение раскладки

1. удалить старое слово (`Backspace` × длину) или выделение (один `Backspace`);
2. через KDE DBus `org.kde.keyboard /Layouts` переключить раскладку на нужную;
3. напечатать исправленный текст физическими клавишами уже в правильной
   раскладке.

Так работает классический автопереключатель раскладки: после исправления
раскладка остаётся правильной, и можно продолжать печатать. Клипборд не
используется.

Если нужной раскладки в KDE нет или DBus недоступен, включается запасной
режим: текст кладётся в клипборд и вставляется (`Ctrl+V`, настраивается).
Исходный клипборд восстанавливается.

### 1.5. Определение «неправильной» раскладки

Кандидаты — исходный текст и он же, переведённый по таблице раскладок.
Оценка слова в языке складывается из:

* проверки по словарю Hunspell (`hunspell-ru`, `hunspell-en_us`);
* частотного мини-словаря (встроен, работает без зависимостей);
* доли гласных, длины согласных кластеров, частых биграмм.

Решение принимается, только если второй вариант заметно лучше первого и
похож на настоящее слово. Это защищает от порчи правильного текста и от
замены «мусора на мусор». Принудительная замена — `lapskey trigger --force`
(или `fix.double_press_ms`, по умолчанию выключено).

Автоматический режим (после пробела) выключен по умолчанию: ложные
срабатывания раздражают сильнее, чем пропущенное слово. Включается в
`config.toml` (`[auto] enabled = true`).

### 1.6. Ограничения, о которых честно стоит знать

* Чтение «последнего слова» работает, пока демон запущен и видел ввод.
  После запуска демона нужно набрать слово заново (или пользоваться
  выделением/резервным копированием).
* Буфер сбрасывается при кликах мышью, стрелках, Enter/Tab/Esc — как и
  полагается. Alt+Tab тоже сбрасывает (через сброс по модификаторам).
* Если приложение не поддерживает `Ctrl+Insert` и не отдаёт PRIMARY, остаётся
  только буфер evdev. Для терминалов это нормальный сценарий.
* Вставка через клипборд (запасной путь) в терминале требует
  `paste_combo = "ctrl+shift+v"`; настраивается.
* Для доступа к `/dev/input` нужна группа `input`, для `/dev/uinput` — права
  на запись (udev-правило ставит установщик).
* Это не «чтение экрана»: содержимое чужого окна не анализируется.

---

## 2. Установка

### 2.1. Зависимости (pacman)

```bash
sudo pacman -S --needed python-evdev python-pyqt6 wl-clipboard libnotify \
    hunspell hunspell-en_us hunspell-ru
```

AUR не требуется. Опционально:

```bash
sudo pacman -S --needed ydotool     # запасной способ ввода
```

### 2.2. Автоматический установщик

```bash
git clone https://github.com/vampir551/Lapskey.git
cd Lapskey
./scripts/install.sh
```

Или, если проект уже скачан в произвольный каталог:

```bash
cd /путь/к/LapSKey
./scripts/install.sh
```

Что делает скрипт:

* ставит пакеты (можно пропустить: `--no-deps`);
* добавляет пользователя в группу `input`, включает модуль `uinput`,
  ставит udev-правило (можно пропустить: `--no-system`);
* копирует приложение в `~/.local/share/lapskey/app`;
* создаёт команду `~/.local/bin/lapskey`;
* создаёт `~/.config/lapskey/config.toml`, если его ещё нет;
* устанавливает и запускает `lapskey.service` и `lapskey-tray.service`
  (`systemd --user`);
* кладёт ярлыки в меню приложений;
* регистрирует `F12` и `Shift+F12` в KDE, если они ещё не заняты
  (можно пропустить: `--no-hotkeys`);
* запускает `lapskey doctor`.

Полезные флаги: `--no-deps`, `--no-system`, `--no-service`, `--no-tray`,
`--no-hotkeys`, `--force-config`, `--force-hotkeys`, `--dry-run`.

Если пользователя только что добавили в группу `input`, нужен повторный вход
в систему (или `newgrp input`).

### 2.3. Горячие клавиши

Обычно настраивать вручную не нужно: установщик регистрирует

* `F12` — умное исправление;
* `Shift+F12` — принудительная замена.

Если клавиши уже были заняты, установщик их не трогает. Изменить их можно
в трее: **Настройки… → Клавиши → Записать… → Применить**. Там же есть
кнопка «Открыть настройки клавиш KDE» для ручной настройки.

Проверить: набрать `ghbdtn`, нажать `F12` — должно получиться `привет`.
Для принудительной замены — `Shift+F12`.

Если эвристика не уверена, `F12` оставляет текст без изменений и показывает
уведомление. Принудительная замена: `Shift+F12` или
`lapskey trigger --force`.

### 2.4. Проверка

```bash
~/.local/bin/lapskey doctor
~/.local/bin/lapskey status
~/.local/bin/lapskey convert "ghbdtn"      # офлайн-проверка
journalctl --user -u lapskey -f
```

---

## 3. Использование

```bash
lapskey trigger                 # исправить последнее слово/выделение
lapskey trigger --force         # конвертировать принудительно
lapskey trigger --selection     # читать только выделение
lapskey convert "ghbdtn"        # офлайн: показать результат
lapskey convert --json "руддщ"  # подробности решения
lapskey status [--json]
lapskey doctor
lapskey stop
lapskey reload                  # перечитать config.toml без перезапуска
lapskey config [--init] [--force]
lapskey tray                    # значок в трее и настройки (PyQt6)
```

Горячая клавиша вызывает `lapskey trigger`. В обычном сценарии клипборд не
изменяется.

---

## 3.1. Значок в трее и настройки

Установщик ставит `lapskey-tray.service`: при входе в KDE появляется
значок **LapSKey**. Левый и правый клик открывают меню; «Настройки…» —
первый пункт.

Меню значка:

* состояние службы и активной раскладки;
* «Исправить слово / выделение (F12)»;
* «Исправить принудительно (Shift+F12)»;
* «Настройки…» — графический редактор `config.toml`;
* «Служба» → запустить / остановить / перезапустить демон;
* «Автозапуск трея» — включить/выключить автозапуск;
* «Проверка окружения…» — вывод `lapskey doctor`;
* «Журнал…» — последние 300 строк журнала;
* «Настройки клавиш KDE…» — открывает KCM со шорткатами.

Настройки сохраняются в тот же `~/.config/lapskey/config.toml`, после
чего служба перезапускается (или перечитывает конфиг, если снять галочку).
При первом сохранении комментарии из файла заменяются сгенерированным
содержимым — это нормально, дальше конфигом управляет GUI.

Запуск вручную:

```bash
lapskey tray
systemctl --user restart lapskey-tray.service
```

Если нужен только трей без автозапуска:

```bash
systemctl --user disable --now lapskey-tray.service
```

---

## 4. Конфигурация

`~/.config/lapskey/config.toml` (создаётся установщиком, все параметры
необязательны):

```toml
[general]
log_level = "info"        # debug | info | warning | error
notify = true
notify_errors = true

[hotkey]
mode = "kde"              # kde | evdev | both
evdev_key = "KEY_F12"
evdev_ctrl = false
evdev_shift = false
evdev_alt = false

[input]
acquisition = "auto"      # auto | buffer | clipboard
copy_combo = "ctrl+insert"
select_word_combo = "ctrl+shift+left"
primary_selection = true
copy_probe = true
restore_clipboard = true
clipboard_timeout_ms = 350
selection_max_chars = 200

[fix]
mode = "smart"            # smart | always
score_threshold = 1.5
double_press_ms = 0        # 0 = выключено
switch_layout = true
insert = "auto"           # auto | keys | clipboard
paste_combo = "ctrl+v"
inter_key_delay_ms = 2
layout_switch_timeout_ms = 500
modifier_release_timeout_ms = 3000

[auto]
enabled = false
min_word_length = 3
score_threshold = 5.0
delay_ms = 140

[keyboard]
devices = []              # например ["/dev/input/event4"]
exclude = ["lapskey", "ydotool"]
rescan_seconds = 10

[layout]
backend = "kde"

[mapping]
# дополнительные/переопределяющие пары [английский, русский]
pairs = []                # например [["q", "й"], ["]", "ъ"]]

[dictionaries]
files = []                # свои списки слов, по одному на строку
auto_detect = true

[paths]
wl_paste = "wl-paste"
wl_copy = "wl-copy"
ydotool = "ydotool"
```

После изменения конфигурации:

```bash
systemctl --user restart lapskey
# или без перезапуска: большинство параметров перечитывается по SIGHUP
kill -HUP "$(pgrep -u "$USER" -f 'lapskey run')"
```

`SIGHUP` перечитывает конфиг; настройки хоткея и устройств требуют рестарта.

### 4.1. Автоматический режим

```toml
[auto]
enabled = true
```

Демон будет проверять слово после каждого пробела и исправлять его, если
уверенность высокая. Работает только при доступном `/dev/input` и
раскладке ru/en. Если начать печатать во время задержки, исправление
отменяется.

---

## 5. systemd (user)

Файл: `~/.config/systemd/user/lapskey.service`

```bash
systemctl --user enable --now lapskey.service
systemctl --user status lapskey.service
journalctl --user -u lapskey -f
```

Юнит входит в `graphical-session.target`. Если в вашей сессии не проброшены
переменные окружения:

```bash
systemctl --user import-environment WAYLAND_DISPLAY DISPLAY \
    XDG_CURRENT_DESKTOP DBUS_SESSION_BUS_ADDRESS
systemctl --user restart lapskey
```

---

## 6. Диагностика

| Симптом | Что проверить |
|---|---|
| `lapskey doctor` пишет FAIL про `/dev/uinput` | группа `input`, udev-правило, повторный вход |
| «нет доступа к /dev/input» | `sudo usermod -aG input $USER`, перелогин |
| Горячая клавиша не срабатывает | команда в KDE должна быть абсолютным путём; `kglobalaccel5`/KDE перезапустить; `lapskey trigger` из терминала |
| «демон не запущен» | `systemctl --user start lapskey`; `journalctl --user -u lapskey -n 50` |
| Раскладка не переключается | в KDE должны быть настроены us и ru; проверка: `gdbus call --session --dest org.kde.keyboard --object-path /Layouts --method org.kde.KeyboardLayouts.getLayoutsList` |
| В терминале вставка не работает | `paste_combo = "ctrl+shift+v"` или используйте ввод клавишами (обычный путь) |
| Демон ест CPU в простое | не должен: evdev-поток спит в `select`, автопроверка только по пробелу |
| Ничего не исправляется, хотя слово явно неверное | нажмите клавишу дважды (форс) или `lapskey convert --json "..."`, чтобы увидеть оценку; поставьте hunspell-словари |

Логи: `journalctl --user -u lapskey -f` или `log_level = "debug"`.

---

## 7. Удаление / полный откат

```bash
./scripts/uninstall.sh                 # спросит подтверждение
./scripts/uninstall.sh --yes           # без вопроса
./scripts/uninstall.sh --keep-config --yes
./scripts/uninstall.sh --dry-run --yes # показать, что будет удалено
./scripts/uninstall.sh --remove-deps --yes  # ещё и пакеты
```

Скрипт останавливает и удаляет службу, приложение, команду, ярлык,
конфигурацию, udev-правило, запись в `modules-load.d` и (если установщик её
добавлял) членство в группе `input`. После изменения группы нужен повторный
вход.

Только файлы, без системных изменений:

```bash
./scripts/uninstall.sh --keep-system --yes
```

---

## 8. Структура проекта

```
lapskey/
  mapping.py        таблицы раскладок, keycode <-> символ
  detect.py         словари, эвристики, решение о конвертации
  evdev_monitor.py  чтение /dev/input: буфер слова, выделение, хоткей
  injector.py       uinput / ydotool, эмуляция клавиш
  layout.py         KDE DBus: получить/переключить раскладку
  clipboard.py      PRIMARY + резервный клипборд (wl-clipboard)
  daemon.py          сокет, конвейер исправления, авторежим
  cli.py            lapskey run/trigger/convert/status/doctor/...
  config.py         TOML-конфигурация
config/config.toml
systemd/lapskey.service
systemd/lapskey-tray.service
desktop/lapskey.desktop
desktop/lapskey-tray.desktop
desktop/lapskey-command.desktop
desktop/lapskey-force.desktop
scripts/install.sh
scripts/uninstall.sh
tests/
```

Тесты:

```bash
python3 -m unittest discover -s tests -v
```

В GitHub Actions те же тесты прогоняются на Python 3.11 и 3.12
(`.github/workflows/tests.yml`).

---

## 9. Безопасность

* Демон не открывает сетевых портов; сокет `0600` в `$XDG_RUNTIME_DIR`.
* `/dev/input` только читается, клавиатура не захватывается (`grab` не
  используется), поэтому падение демона не блокирует ввод.
* Виртуальная клавиатура создаётся в ядре; её события видны композитору как
  обычный ввод. Не давайте доступ к `/dev/uinput` недоверенным пользователям.
* Клипборд в штатном сценарии не изменяется. Резервный путь сохраняет и
  восстанавливает текстовое содержимое клипборда.
* `NoNewPrivileges=yes` в юните; демон работает от вашего пользователя.

---

## 10. Лицензия

MIT. См. файл [LICENSE](LICENSE).
