"""Tray icon and settings dialog (PyQt6).

The tray is a separate process from the daemon.  It talks to the daemon over
the same Unix socket as the CLI, so the daemon stays a small asyncio program
and a crashed GUI can never break keyboard input.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import __version__, config as config_mod
from .config import Config

APP_NAME = "LapSKey"
log = logging.getLogger(__name__)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _lapskey_binary() -> str:
    env = os.environ.get("LAPSKEY_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("lapskey")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "lapskey")


def daemon_request(payload: dict, timeout: float = 1.5) -> dict:
    path = config_mod.socket_path()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
            if part.endswith(b"\n"):
                break
        sock.close()
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def _run_detached(program: str, args: list[str]) -> bool:
    from PyQt6.QtCore import QProcess

    return QProcess.startDetached(program, args)


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _systemctl(action: str, unit: str) -> bool:
    return _run_detached("systemctl", ["--user", action, unit])


def _tray_autostart_enabled() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "lapskey-tray.service"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0 and result.stdout.strip() == "enabled"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _show_text_dialog(parent, title: str, text: str, width: int = 760, height: int = 520):
    from PyQt6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QPlainTextEdit,
        QVBoxLayout,
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(width, height)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setPlainText(_strip_ansi(text))
    layout.addWidget(view)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


def _run_and_show(parent, title: str, args: list[str], timeout: float = 30.0) -> None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        text = result.stdout + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0:
            text += f"\n\n(код возврата: {result.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        text = f"Не удалось выполнить {args!r}: {exc}"
    _show_text_dialog(parent, title, text)


# --------------------------------------------------------------------------- settings


def parse_pairs(text: str) -> list[list[str]]:
    """Parse ``q:й, w:ц`` into the config representation."""
    pairs: list[list[str]] = []
    for item in re.split(r"[,;\s]+", text.strip()):
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"пара {item!r}: нужен формат английский:русский")
        en, ru = item.split(":", 1)
        if len(en) != 1 or len(ru) != 1:
            raise ValueError(f"пара {item!r}: нужны одиночные символы")
        pairs.append([en, ru])
    return pairs


def format_pairs(pairs: list[list[str]]) -> str:
    return ", ".join(f"{pair[0]}:{pair[1]}" for pair in pairs if len(pair) == 2)


def parse_list(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\n]+", text) if item.strip()]


QT_MODS = (
    (0x02000000, "Shift"),
    (0x04000000, "Ctrl"),
    (0x08000000, "Alt"),
    (0x10000000, "Meta"),
)
QT_SPECIAL = {
    0x01000000: "Esc",
    0x01000001: "Tab",
    0x01000003: "Backspace",
    0x01000004: "Enter",
    0x01000006: "Insert",
    0x01000007: "Delete",
    0x01000010: "Home",
    0x01000011: "End",
    0x01000012: "Left",
    0x01000013: "Up",
    0x01000014: "Right",
    0x01000015: "Down",
    0x01000016: "PageUp",
    0x01000017: "PageDown",
    0x20: "Space",
}


def format_qt_key(code: int) -> str:
    """Turn a Qt QKeySequence integer into ``Ctrl+Shift+F12``."""
    mods = [name for bit, name in QT_MODS if code & bit]
    base = code
    for bit, _name in QT_MODS:
        base &= ~bit
    if 0x01000030 <= base <= 0x01000053:  # F1..F35
        name = f"F{base - 0x01000030 + 1}"
    elif base in QT_SPECIAL:
        name = QT_SPECIAL[base]
    elif 0x41 <= base <= 0x5A:
        name = chr(base)
    elif 0x30 <= base <= 0x39:
        name = chr(base)
    elif base == 0x2C:
        name = "Comma"
    elif base == 0x2E:
        name = "Period"
    elif base == 0x2F:
        name = "Slash"
    elif base == 0x3B:
        name = "Semicolon"
    elif base == 0x5B:
        name = "BracketLeft"
    elif base == 0x5D:
        name = "BracketRight"
    else:
        name = f"0x{base:x}"
    return "+".join(mods + [name])


def _gdbus(*args: str, timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(
            ["gdbus", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def query_binding(action_id: list[str]) -> list[str] | None:
    """Return the human-readable shortcuts of a KGlobalAccel action.

    ``None`` means the KDE service is unavailable; ``[]`` means "not assigned".
    """
    arg = "[" + ", ".join(repr(part) for part in action_id) + "]"
    raw = _gdbus(
        "call",
        "--session",
        "--dest",
        "org.kde.kglobalaccel",
        "--object-path",
        "/kglobalaccel",
        "--method",
        "org.kde.KGlobalAccel.shortcut",
        arg,
    )
    if raw is None:
        return None
    if raw in ("(@ai [],)", "()"):
        return []
    numbers = re.findall(r"\b\d{4,}\b", raw)
    return [format_qt_key(int(number)) for number in numbers]


def bindings_info() -> dict[str, object]:
    smart = query_binding(
        ["net.local.lapskey.desktop", "_launch", "LapSKey", "LapSKey"]
    )
    force = query_binding(
        [
            "net.local.lapskey-force.desktop",
            "_launch",
            "LapSKey Force",
            "LapSKey Force",
        ]
    )
    return {"smart": smart, "force": force}


_ICON_CACHE: dict[tuple[bool, str | None], object] = {}


def _draw_flag_pixmap(lang: str, size: int):
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import (
        QColor,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
    )

    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin = max(1.0, size * 0.05)
    width = size - 2 * margin
    height = width * 2.0 / 3.0
    x = margin
    y = (size - height) / 2.0
    rect = QRectF(x, y, width, height)

    path = QPainterPath()
    path.addRoundedRect(rect, size * 0.08, size * 0.08)
    painter.setClipPath(path)

    if lang == "ru":
        painter.fillRect(QRectF(x, y, width, height / 3), QColor("#ffffff"))
        painter.fillRect(
            QRectF(x, y + height / 3, width, height / 3), QColor("#0039a6")
        )
        painter.fillRect(
            QRectF(x, y + 2 * height / 3, width, height / 3), QColor("#d52b1e")
        )
    else:  # en -> US flag
        stripe = height / 13.0
        for index in range(13):
            color = QColor("#b22234") if index % 2 == 0 else QColor("#ffffff")
            painter.fillRect(
                QRectF(x, y + index * stripe, width, stripe + 0.4), color
            )
        canton_w = width * 0.4
        canton_h = stripe * 7
        painter.fillRect(QRectF(x, y, canton_w, canton_h), QColor("#3c3b6e"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        rows = 9
        for row in range(rows):
            columns = 6 if row % 2 == 0 else 5
            for column in range(columns):
                cx = x + (column + 0.5) * (canton_w / 6.0)
                if row % 2:
                    cx += canton_w / 12.0
                cy = y + (row + 0.5) * (canton_h / rows)
                radius = max(0.55, size * 0.011)
                painter.drawEllipse(
                    QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
                )

    painter.setClipping(False)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 90), max(1.0, size * 0.02)))
    painter.drawRoundedRect(rect, size * 0.08, size * 0.08)
    painter.end()
    return pix


def flag_icon(lang: str):
    """RU or US flag as a tray icon (cached, several DPI sizes)."""
    from PyQt6.QtGui import QIcon

    key = (True, lang)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    icon = QIcon()
    for size in (22, 32, 48, 64, 96):
        icon.addPixmap(_draw_flag_pixmap(lang, size))
    _ICON_CACHE[key] = icon
    return icon


def status_icon(ok: bool, lang: str | None = None):
    """Flag of the active layout, or a warning icon when the daemon is down."""
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

    key = (bool(ok), lang if ok else None)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    if not ok:
        icon = QIcon.fromTheme("dialog-warning")
        if icon.isNull():
            icon = QIcon.fromTheme("input-keyboard")
    elif lang in ("ru", "en"):
        icon = flag_icon(lang)
    else:
        icon = QIcon.fromTheme("input-keyboard")
        if icon.isNull():
            icon = QIcon.fromTheme("keyboard")

    if icon.isNull():
        pix = QPixmap(64, 64)
        pix.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#3daee9" if ok else "#da4453"))
        painter.setPen(QColor(0, 0, 0, 0))
        painter.drawRoundedRect(6, 18, 52, 28, 8, 8)
        painter.end()
        icon = QIcon(pix)

    _ICON_CACHE[key] = icon
    return icon


SMART_ACTION = ["net.local.lapskey.desktop", "_launch", "LapSKey", "LapSKey"]
FORCE_ACTION = [
    "net.local.lapskey-force.desktop",
    "_launch",
    "LapSKey Force",
    "LapSKey Force",
]
SHIFT_BIT = 0x02000000


def _kde_set_shortcut(action_id: list[str], keys: list[int]) -> bool:
    arg = "[" + ", ".join(repr(part) for part in action_id) + "]"
    keyarg = "[" + ", ".join(str(key) for key in keys) + "]"
    # Make sure the component is known to kglobalaccel in this session too.
    _gdbus(
        "call",
        "--session",
        "--dest",
        "org.kde.kglobalaccel",
        "--object-path",
        "/kglobalaccel",
        "--method",
        "org.kde.KGlobalAccel.doRegister",
        arg,
    )
    raw = _gdbus(
        "call",
        "--session",
        "--dest",
        "org.kde.kglobalaccel",
        "--object-path",
        "/kglobalaccel",
        "--method",
        "org.kde.KGlobalAccel.setForeignShortcut",
        arg,
        keyarg,
    )
    return raw is not None


def apply_hotkey(code: int | None) -> tuple[bool, str]:
    """Set the main shortcut and derive Shift+shortcut for force.

    The force action is updated too, but the daemon also recognises the
    physical Shift+key gesture, so it works even on setups where KDE routes
    Shift+F12 to the plain F12 action.
    """
    if code is None:
        ok_smart = _kde_set_shortcut(SMART_ACTION, [])
        ok_force = _kde_set_shortcut(FORCE_ACTION, [])
        if ok_smart and ok_force:
            return True, "горячая клавиша снята"
        return False, "не удалось изменить привязку в KDE"

    if code & SHIFT_BIT:
        return False, "Shift зарезервирован для принудительной замены"

    ok_smart = _kde_set_shortcut(SMART_ACTION, [code])
    ok_force = _kde_set_shortcut(FORCE_ACTION, [code | SHIFT_BIT])
    if ok_smart and ok_force:
        return True, format_qt_key(code)
    return False, "не удалось изменить привязку в KDE"


def bindings_text(info: dict[str, object]) -> str:
    smart = info.get("smart")
    force = info.get("force")
    if smart is None or force is None:
        return (
            "KDE kglobalaccel недоступен.\n"
            "Проверьте, что приложение запущено в сессии KDE Plasma."
        )
    smart_text = (
        "назначено: " + ", ".join(smart) if smart else "не назначено"
    )
    if force:
        force_text = "назначено: " + ", ".join(force)
    else:
        force_text = "отдельная привязка не задана; используется Shift + основная"
    return (
        f"<b>Основная клавиша:</b> {smart_text}<br>"
        f"<b>Принудительно:</b> {force_text}"
    )


class SettingsDialog:
    """Built lazily so importing the module never requires PyQt6."""

    def __new__(cls, cfg: Config, parent=None, initial_tab: int = 0):
        from PyQt6.QtCore import Qt, QTimer
        from PyQt6.QtWidgets import (
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QDoubleSpinBox,
            QFormLayout,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QSpinBox,
            QTabWidget,
            QVBoxLayout,
            QWidget,
        )

        class _KeyGrab(QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Запись горячей клавиши")
                self.setModal(True)
                self.resize(460, 170)
                self.code: int | None = None
                layout = QVBoxLayout(self)
                self.label = QLabel(
                    "<b>Нажмите нужную комбинацию клавиш</b><br><br>"
                    "Esc — отмена. Shift не используйте: "
                    "Shift + основная клавиша зарезервирован для "
                    "принудительной замены."
                )
                self.label.setWordWrap(True)
                layout.addWidget(self.label)
                cancel = QPushButton("Отмена")
                cancel.clicked.connect(self.reject)
                layout.addWidget(cancel)

            def keyPressEvent(self, event):
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    self.reject()
                    return
                if key in (
                    Qt.Key.Key_Shift,
                    Qt.Key.Key_Control,
                    Qt.Key.Key_Alt,
                    Qt.Key.Key_Meta,
                    Qt.Key.Key_AltGr,
                ):
                    return
                mods = event.modifiers()
                mods_value = mods.value if hasattr(mods, "value") else int(mods)
                self.code = int(key) | int(mods_value)
                self.label.setText(
                    f"<b>Выбрано:</b> {format_qt_key(self.code)}<br><br>"
                    "Нажмите ещё раз, чтобы заменить, или закройте окно."
                )
                self.accept()

        class _Dialog(QDialog):
            def __init__(self, cfg: Config, parent=None, initial_tab: int = 0):
                super().__init__(parent)
                self.cfg = cfg
                self._pending_hotkey: int | None = None
                self.setWindowTitle(f"{APP_NAME} — настройки")
                self.resize(680, 640)

                tabs = QTabWidget()
                self.tabs = tabs
                tabs.addTab(self._tab_status(), "Состояние")
                tabs.addTab(self._tab_general(), "Общие")
                tabs.addTab(self._tab_fix(), "Исправление")
                tabs.addTab(self._tab_input(), "Ввод")
                tabs.addTab(self._tab_auto(), "Авторежим")
                tabs.addTab(self._tab_keys(), "Клавиши")
                tabs.addTab(self._tab_advanced(), "Дополнительно")
                tabs.setCurrentIndex(max(0, min(initial_tab, tabs.count() - 1)))

                self.restart_check = QCheckBox(
                    "Перезапустить службу после сохранения (применит все настройки)"
                )
                self.restart_check.setChecked(True)

                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Save
                    | QDialogButtonBox.StandardButton.Cancel
                )
                buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
                buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
                buttons.accepted.connect(self._save)
                buttons.rejected.connect(self.reject)

                layout = QVBoxLayout(self)
                layout.addWidget(tabs)
                layout.addWidget(self.restart_check)
                layout.addWidget(buttons)

            # -- helpers --------------------------------------------------
            def _combo(self, form, label, items, current):
                w = QComboBox()
                w.addItems(items)
                if current in items:
                    w.setCurrentText(current)
                form.addRow(label, w)
                return w

            def _check(self, form, label, value):
                w = QCheckBox()
                w.setChecked(bool(value))
                form.addRow(label, w)
                return w

            def _spin(self, form, label, value, minimum, maximum, suffix=""):
                w = QSpinBox()
                w.setRange(minimum, maximum)
                w.setValue(int(value))
                if suffix:
                    w.setSuffix(suffix)
                form.addRow(label, w)
                return w

            def _dspin(self, form, label, value, minimum, maximum, step=0.1, suffix=""):
                w = QDoubleSpinBox()
                w.setRange(minimum, maximum)
                w.setSingleStep(step)
                w.setDecimals(2)
                w.setValue(float(value))
                if suffix:
                    w.setSuffix(suffix)
                form.addRow(label, w)
                return w

            def _line(self, form, label, value):
                w = QLineEdit(str(value))
                form.addRow(label, w)
                return w

            def _page(self):
                page = QWidget()
                form = QFormLayout(page)
                form.setFieldGrowthPolicy(
                    QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
                )
                return page, form

            # -- tabs -----------------------------------------------------
            def _tab_status(self):
                page = QWidget()
                layout = QVBoxLayout(page)
                self.status_view = QLabel("Загрузка состояния…")
                self.status_view.setWordWrap(True)
                self.status_view.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                layout.addWidget(self.status_view)

                row = QHBoxLayout()
                refresh = QPushButton("Обновить")
                refresh.clicked.connect(self._refresh_status)
                doctor = QPushButton("Проверить окружение")
                doctor.clicked.connect(self._doctor)
                journal = QPushButton("Показать журнал")
                journal.clicked.connect(self._journal)
                row.addWidget(refresh)
                row.addWidget(doctor)
                row.addWidget(journal)
                layout.addLayout(row)
                layout.addStretch(1)
                QTimer.singleShot(60, self._refresh_status)
                return page

            def _refresh_status(self):
                status = daemon_request({"cmd": "status"}, timeout=1.2)
                if not status.get("ok"):
                    error = html.escape(str(status.get("error", "неизвестная ошибка")))
                    self.status_view.setText(
                        f"<h3>Служба не запущена</h3><p>{error}</p>"
                        "<p>Запустите её кнопкой ниже или командой "
                        "<code>systemctl --user start lapskey</code>.</p>"
                    )
                    return
                layout = status.get("layout") or {}
                monitor = status.get("monitor") or {}
                clipboard = status.get("clipboard") or {}
                dictionaries = status.get("dictionaries") or []
                uptime = int(status.get("uptime_s") or 0)
                minutes, seconds = divmod(uptime, 60)

                lang = html.escape(str(layout.get("name") or "неизвестно"))
                code = html.escape(str(layout.get("code") or "?"))
                lang_code = html.escape(str(layout.get("lang") or "?"))
                injector = html.escape(str(status.get("injector") or "нет"))
                monitor_state = "OK" if monitor.get("available") else "НЕТ"
                monitor_error = monitor.get("error")
                mods = ", ".join(monitor.get("pressed_modifiers") or []) or "отпущены"
                auto = "включён" if status.get("auto") else "выключен"

                rows = [
                    f"<b>Служба:</b> работает, PID {status.get('pid')} "
                    f"({minutes} мин {seconds} с)",
                    f"<b>Раскладка:</b> {lang} [{code}/{lang_code}]",
                    f"<b>Ввод:</b> {injector}",
                    f"<b>Монитор ввода:</b> {monitor_state}; "
                    f"буфер {monitor.get('buffer_keys', 0)} клавиш; "
                    f"модификаторы: {html.escape(mods)}",
                ]
                if monitor_error:
                    rows.append(
                        f"<b>Ошибка монитора:</b> {html.escape(str(monitor_error))}"
                    )
                rows.extend(
                    [
                        f"<b>Клипборд:</b> чтение "
                        f"{'да' if clipboard.get('paste') else 'нет'}, запись "
                        f"{'да' if clipboard.get('copy') else 'нет'}",
                        f"<b>Авторежим:</b> {auto}",
                    ]
                )
                if dictionaries:
                    rows.append("<b>Словари:</b>")
                    rows.extend(
                        f"&nbsp;&nbsp;· {html.escape(str(item))}"
                        for item in dictionaries
                    )
                else:
                    rows.append("<b>Словари:</b> только встроенный мини-словарь")
                rows.append(
                    f"<b>Сокет:</b> <code>{html.escape(str(status.get('socket', '')))}</code>"
                )
                rows.append(
                    f"<b>Конфиг:</b> <code>{html.escape(str(status.get('config', '')))}</code>"
                )
                self.status_view.setText("<br>".join(rows))

            def _journal(self):
                _run_and_show(
                    self,
                    "LapSKey — журнал",
                    [
                        "journalctl",
                        "--user",
                        "-u",
                        "lapskey.service",
                        "-n",
                        "300",
                        "--no-pager",
                    ],
                    timeout=15,
                )

            def _tab_general(self):
                page, form = self._page()
                self.log_level = self._combo(
                    form,
                    "Уровень логов:",
                    ["debug", "info", "warning", "error"],
                    self.cfg.general.log_level,
                )
                self.notify = self._check(form, "Показывать уведомления:", self.cfg.general.notify)
                self.notify_errors = self._check(
                    form, "Показывать ошибки:", self.cfg.general.notify_errors
                )
                return page

            def _tab_fix(self):
                page, form = self._page()
                self.mode = self._combo(
                    form,
                    "Режим:",
                    ["smart", "always"],
                    self.cfg.fix.mode,
                )
                self.threshold = self._dspin(
                    form,
                    "Порог уверенности:",
                    self.cfg.fix.score_threshold,
                    0.0,
                    20.0,
                    0.1,
                )
                self.double_press = self._spin(
                    form,
                    "Двойное нажатие (мс):",
                    self.cfg.fix.double_press_ms,
                    0,
                    5000,
                    " мс",
                )
                self.switch_layout = self._check(
                    form, "Переключать раскладку KDE:", self.cfg.fix.switch_layout
                )
                self.insert = self._combo(
                    form,
                    "Способ ввода:",
                    ["auto", "keys", "clipboard"],
                    self.cfg.fix.insert,
                )
                self.paste_combo = self._line(
                    form, "Комбинация вставки:", self.cfg.fix.paste_combo
                )
                self.paste_combo.setToolTip(
                    "Используется только при вставке через буфер; в терминалах это обычно ctrl+shift+v"
                )
                return page

            def _tab_input(self):
                page, form = self._page()
                self.acquisition = self._combo(
                    form,
                    "Источник текста:",
                    ["auto", "buffer", "clipboard"],
                    self.cfg.input.acquisition,
                )
                self.copy_combo = self._line(
                    form, "Копирование:", self.cfg.input.copy_combo
                )
                self.select_combo = self._line(
                    form, "Выделение слова:", self.cfg.input.select_word_combo
                )
                self.primary = self._check(
                    form, "Читать PRIMARY selection:", self.cfg.input.primary_selection
                )
                self.copy_probe = self._check(
                    form, "Резервное копирование:", self.cfg.input.copy_probe
                )
                self.restore_clip = self._check(
                    form, "Восстанавливать клипборд:", self.cfg.input.restore_clipboard
                )
                self.sel_max = self._spin(
                    form,
                    "Максимум символов в выделении:",
                    self.cfg.input.selection_max_chars,
                    10,
                    5000,
                )
                return page

            def _tab_auto(self):
                page, form = self._page()
                self.auto_enabled = self._check(
                    form, "Автоисправление после пробела:", self.cfg.auto.enabled
                )
                self.auto_min = self._spin(
                    form, "Минимальная длина слова:", self.cfg.auto.min_word_length, 2, 30
                )
                self.auto_threshold = self._dspin(
                    form,
                    "Порог уверенности:",
                    self.cfg.auto.score_threshold,
                    0.0,
                    30.0,
                    0.1,
                )
                self.auto_delay = self._spin(
                    form, "Задержка (мс):", self.cfg.auto.delay_ms, 0, 2000, " мс"
                )
                return page

            def _tab_keys(self):
                page = QWidget()
                layout = QVBoxLayout(page)
                layout.addWidget(
                    QLabel(
                        "Основная клавиша запускает умное исправление.<br>"
                        "<b>Принудительная замена — Shift + основная клавиша.</b>"
                        "<br><br>"
                        "Клавиши можно менять здесь, не открывая настройки KDE. "
                        "Shift зарезервирован: он превращает основную клавишу в "
                        "принудительную."
                    )
                )
                form = QFormLayout()
                self.hotkey_value = QLabel("проверка…")
                self.hotkey_value.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                form.addRow("Основная клавиша:", self.hotkey_value)
                form.addRow(
                    "Принудительно:",
                    QLabel("Shift + основная клавиша (автоматически)"),
                )
                layout.addLayout(form)

                self.bindings_label = QLabel("Проверка привязки…")
                self.bindings_label.setWordWrap(True)
                self.bindings_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                layout.addWidget(self.bindings_label)

                row = QHBoxLayout()
                record = QPushButton("Записать…")
                record.clicked.connect(self._record_hotkey)
                apply_button = QPushButton("Применить")
                apply_button.clicked.connect(self._apply_hotkey)
                clear = QPushButton("Убрать")
                clear.clicked.connect(self._clear_hotkey)
                row.addWidget(record)
                row.addWidget(apply_button)
                row.addWidget(clear)
                layout.addLayout(row)

                row2 = QHBoxLayout()
                refresh = QPushButton("Обновить")
                refresh.clicked.connect(self._refresh_bindings)
                open_kde = QPushButton("Открыть настройки клавиш KDE")
                open_kde.clicked.connect(self._open_kde_keys)
                check = QPushButton("Подробная проверка")
                check.clicked.connect(self._check_kde_keys)
                row2.addWidget(refresh)
                row2.addWidget(open_kde)
                row2.addWidget(check)
                layout.addLayout(row2)
                layout.addStretch(1)
                QTimer.singleShot(80, self._refresh_bindings)
                return page

            def _refresh_bindings(self):
                try:
                    info = bindings_info()
                    self.bindings_label.setText(bindings_text(info))
                    smart = info.get("smart")
                    if self._pending_hotkey is not None:
                        self.hotkey_value.setText(
                            f"{format_qt_key(self._pending_hotkey)} (не применено)"
                        )
                    elif smart:
                        self.hotkey_value.setText(", ".join(smart))
                    else:
                        self.hotkey_value.setText("не назначено")
                except Exception as exc:  # noqa: BLE001 - UI must not crash
                    self.bindings_label.setText(f"Не удалось проверить: {exc}")

            def _record_hotkey(self):
                from PyQt6.QtWidgets import QMessageBox

                grab = _KeyGrab(self)
                if grab.exec() != QDialog.DialogCode.Accepted.value or grab.code is None:
                    return
                if grab.code & SHIFT_BIT:
                    QMessageBox.warning(
                        self,
                        APP_NAME,
                        "Shift зарезервирован: принудительная замена — это "
                        "Shift + основная клавиша. Выберите комбинацию без Shift.",
                    )
                    return
                self._pending_hotkey = grab.code
                self._refresh_bindings()

            def _apply_hotkey(self):
                from PyQt6.QtWidgets import QMessageBox

                if self._pending_hotkey is None:
                    QMessageBox.information(
                        self,
                        APP_NAME,
                        "Сначала нажмите «Записать…» и выберите клавишу.",
                    )
                    return
                ok, message = apply_hotkey(self._pending_hotkey)
                if not ok:
                    QMessageBox.warning(self, APP_NAME, message)
                    return
                self._pending_hotkey = None
                self._refresh_bindings()
                QMessageBox.information(
                    self, APP_NAME, f"Горячая клавиша изменена: {message}"
                )

            def _clear_hotkey(self):
                from PyQt6.QtWidgets import QMessageBox

                ok, message = apply_hotkey(None)
                if not ok:
                    QMessageBox.warning(self, APP_NAME, message)
                    return
                self._pending_hotkey = None
                self._refresh_bindings()
                QMessageBox.information(self, APP_NAME, "Горячие клавиши сняты.")

            def _tab_advanced(self):
                page, form = self._page()
                self.devices = self._line(
                    form, "Устройства ввода:", ", ".join(self.cfg.keyboard.devices)
                )
                self.devices.setToolTip("Пусто = все доступные клавиатуры")
                self.exclude = self._line(
                    form, "Исключить устройства:", ", ".join(self.cfg.keyboard.exclude)
                )
                self.pairs = self._line(
                    form, "Пары символов:", format_pairs(self.cfg.mapping.pairs)
                )
                self.pairs.setToolTip("Формат: q:й, w:ц")
                self.dict_auto = self._check(
                    form, "Искать словари hunspell:", self.cfg.dictionaries.auto_detect
                )
                self.dict_files = QPlainTextEdit("\n".join(self.cfg.dictionaries.files))
                self.dict_files.setMaximumHeight(90)
                form.addRow("Свои словари:", self.dict_files)

                row = QHBoxLayout()
                open_config = QPushButton("Открыть config.toml")
                open_config.clicked.connect(self._open_config)
                doctor = QPushButton("Запустить doctor")
                doctor.clicked.connect(self._doctor)
                row.addWidget(open_config)
                row.addWidget(doctor)
                form.addRow("", row)
                return page

            # -- actions --------------------------------------------------
            def _open_kde_keys(self):
                if not _run_detached("kcmshell6", ["kcm_keys"]):
                    _run_detached("systemsettings", [])

            def _check_kde_keys(self):
                self._refresh_bindings()
                info = bindings_info()
                smart = info.get("smart")
                force = info.get("force")
                if smart is None or force is None:
                    text = (
                        "Служба KDE kglobalaccel недоступна.\n"
                        "Убедитесь, что приложение запущено в сессии KDE Plasma."
                    )
                else:
                    main = smart[0] if smart else "—"
                    text = (
                        "Умное исправление\n"
                        f"  действие: LapSKey ({', '.join(smart) if smart else 'не назначено'})\n"
                        f"  клавиша:  {', '.join(smart) if smart else '—'}\n\n"
                        "Принудительная замена\n"
                        f"  действие: LapSKey Force "
                        f"({', '.join(force) if force else 'не назначено'})\n"
                        f"  клавиша:  {', '.join(force) if force else 'Shift + ' + main}\n\n"
                        "Если отдельная привязка LapSKey Force не сработала, демон "
                        "всё равно распознаёт физический Shift + основную клавишу."
                    )
                _show_text_dialog(self, "Проверка горячих клавиш", text, 620, 360)

            def _open_config(self):
                from PyQt6.QtCore import QUrl
                from PyQt6.QtGui import QDesktopServices

                target = self.cfg.source_path or config_mod.config_path()
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

            def _doctor(self):
                _run_and_show(
                    self,
                    "LapSKey — doctor",
                    [_lapskey_binary(), "doctor"],
                    timeout=30,
                )

            def _save(self):
                from PyQt6.QtWidgets import QMessageBox

                if self._pending_hotkey is not None:
                    ok, message = apply_hotkey(self._pending_hotkey)
                    if not ok:
                        QMessageBox.warning(self, APP_NAME, message)
                        return
                    self._pending_hotkey = None
                    self._refresh_bindings()

                cfg = self.cfg
                cfg.general.log_level = self.log_level.currentText()
                cfg.general.notify = self.notify.isChecked()
                cfg.general.notify_errors = self.notify_errors.isChecked()

                cfg.fix.mode = self.mode.currentText()
                cfg.fix.score_threshold = self.threshold.value()
                cfg.fix.double_press_ms = self.double_press.value()
                cfg.fix.switch_layout = self.switch_layout.isChecked()
                cfg.fix.insert = self.insert.currentText()
                cfg.fix.paste_combo = self.paste_combo.text().strip() or "ctrl+v"

                cfg.input.acquisition = self.acquisition.currentText()
                cfg.input.copy_combo = self.copy_combo.text().strip() or "ctrl+insert"
                cfg.input.select_word_combo = (
                    self.select_combo.text().strip() or "ctrl+shift+left"
                )
                cfg.input.primary_selection = self.primary.isChecked()
                cfg.input.copy_probe = self.copy_probe.isChecked()
                cfg.input.restore_clipboard = self.restore_clip.isChecked()
                cfg.input.selection_max_chars = self.sel_max.value()

                cfg.auto.enabled = self.auto_enabled.isChecked()
                cfg.auto.min_word_length = self.auto_min.value()
                cfg.auto.score_threshold = self.auto_threshold.value()
                cfg.auto.delay_ms = self.auto_delay.value()

                cfg.keyboard.devices = parse_list(self.devices.text())
                cfg.keyboard.exclude = parse_list(self.exclude.text())
                cfg.dictionaries.auto_detect = self.dict_auto.isChecked()
                cfg.dictionaries.files = [
                    line.strip()
                    for line in self.dict_files.toPlainText().splitlines()
                    if line.strip()
                ]
                try:
                    cfg.mapping.pairs = parse_pairs(self.pairs.text())
                except ValueError as exc:
                    QMessageBox.warning(self, "LapSKey", str(exc))
                    return

                try:
                    path = config_mod.save(cfg)
                except OSError as exc:
                    QMessageBox.critical(
                        self,
                        "LapSKey",
                        f"Не удалось сохранить конфигурацию: {exc}",
                    )
                    return

                if self.restart_check.isChecked():
                    try:
                        result = subprocess.run(
                            ["systemctl", "--user", "restart", "lapskey.service"],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if result.returncode != 0:
                            QMessageBox.warning(
                                self,
                                "LapSKey",
                                "Настройки сохранены, но службу перезапустить не удалось:\n"
                                + (result.stderr or result.stdout),
                            )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        QMessageBox.warning(
                            self,
                            "LapSKey",
                            f"Настройки сохранены, но служба не перезапущена: {exc}",
                        )
                else:
                    result = daemon_request({"cmd": "reload"}, timeout=10)
                    if not result.get("ok"):
                        QMessageBox.information(
                            self,
                            "LapSKey",
                            "Настройки сохранены. Служба не запущена — "
                            "изменения применятся при следующем запуске.",
                        )

                self.saved_path = path
                self.accept()

        return _Dialog(cfg, parent)


# --------------------------------------------------------------------------- tray


class TrayController:
    def __new__(cls, app, cfg: Config):
        from PyQt6.QtCore import QThread, pyqtSignal
        from PyQt6.QtGui import QAction, QCursor
        from PyQt6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

        class StatusWorker(QThread):
            updated = pyqtSignal(dict)

            def run(self):
                while not self.isInterruptionRequested():
                    self.updated.emit(daemon_request({"cmd": "status"}, timeout=1.0))
                    for _ in range(10):
                        if self.isInterruptionRequested():
                            return
                        self.msleep(100)

        class _Controller:
            def __init__(self):
                self.cfg = cfg
                self.quitting = False
                self._settings_dialog = None
                self.menu = QMenu()
                self.tray = QSystemTrayIcon(status_icon(False))
                self.tray.setToolTip(APP_NAME)
                self.status_action = QAction("Проверка службы…")
                self.status_action.setEnabled(False)
                self.menu.addAction(self.status_action)
                self.menu.addSeparator()

                # Keep a Python reference: a local QAction may be garbage
                # collected, which silently removes it from the DBusMenu KDE
                # reads (the menu then looks "old" or loses entries).
                self.settings_action = QAction("Настройки…")
                self.settings_action.triggered.connect(
                    lambda: self.open_settings(0)
                )
                self.menu.addAction(self.settings_action)
                self.menu.addSeparator()

                self.fix_action = QAction("Исправить слово / выделение (F12)")
                self.fix_action.triggered.connect(
                    lambda: _run_detached(_lapskey_binary(), ["trigger", "--quiet"])
                )
                self.force_action = QAction("Исправить принудительно (Shift+F12)")
                self.force_action.triggered.connect(
                    lambda: _run_detached(
                        _lapskey_binary(), ["trigger", "--force", "--quiet"]
                    )
                )
                self.menu.addAction(self.fix_action)
                self.menu.addAction(self.force_action)
                self.menu.addSeparator()

                self.service_menu = self.menu.addMenu("Служба")
                self.start_action = self.service_menu.addAction("Запустить")
                self.start_action.triggered.connect(
                    lambda: _systemctl("start", "lapskey.service")
                )
                self.stop_action = self.service_menu.addAction("Остановить")
                self.stop_action.triggered.connect(
                    lambda: _systemctl("stop", "lapskey.service")
                )
                self.restart_action = self.service_menu.addAction("Перезапустить")
                self.restart_action.triggered.connect(
                    lambda: _systemctl("restart", "lapskey.service")
                )

                self.autostart_action = QAction("Автозапуск трея")
                self.autostart_action.setCheckable(True)
                self.autostart_action.setChecked(_tray_autostart_enabled())
                self.autostart_action.toggled.connect(self._toggle_autostart)
                self.menu.addAction(self.autostart_action)

                self.menu.addSeparator()
                self.doctor_action = QAction("Проверка окружения…")
                self.doctor_action.triggered.connect(self.show_doctor)
                self.menu.addAction(self.doctor_action)

                self.log_action = QAction("Журнал…")
                self.log_action.triggered.connect(self.show_log)
                self.menu.addAction(self.log_action)

                self.kde_action = QAction("Настройки клавиш KDE…")
                self.kde_action.triggered.connect(self.open_kde_keys)
                self.menu.addAction(self.kde_action)

                self.menu.addSeparator()
                self.about_action = QAction("О программе")
                self.about_action.triggered.connect(self.about)
                self.menu.addAction(self.about_action)
                self.quit_action = QAction("Выход из трея")
                self.quit_action.triggered.connect(self.quit)
                self.menu.addAction(self.quit_action)

                # setContextMenu() is required on KDE/Wayland: without it the
                # StatusNotifierItem never receives a ContextMenu request, so
                # right-click does nothing.  Left-click still emits Trigger and
                # opens the status window on a standard KDE tray.
                self.tray.setContextMenu(self.menu)
                log.info("tray menu actions: %s", [a.text() for a in self.menu.actions()])
                self.tray.activated.connect(self._activated)
                self.tray.show()

                self.worker = StatusWorker()
                self.worker.updated.connect(self._on_status)
                self.worker.start()

            # -- status ---------------------------------------------------
            def _on_status(self, status: dict):
                ok = bool(status.get("ok"))
                layout_info = status.get("layout") or {}
                lang = layout_info.get("lang") if ok else None
                self.tray.setIcon(status_icon(ok, lang))
                if ok:
                    layout = layout_info.get("code", "?")
                    injector = status.get("injector") or "нет"
                    auto = "вкл" if status.get("auto") else "выкл"
                    self.status_action.setText(
                        f"Работает · раскладка {layout} · ввод {injector} · авто {auto}"
                    )
                    monitor = status.get("monitor") or {}
                    tip = (
                        f"{APP_NAME}\nРаскладка: {layout}\n"
                        f"Ввод: {injector}\nАвторежим: {auto}"
                    )
                    if monitor.get("error"):
                        tip += f"\nОшибка ввода: {monitor['error']}"
                    self.tray.setToolTip(tip)
                else:
                    self.status_action.setText("Служба не запущена")
                    self.tray.setToolTip(
                        f"{APP_NAME}\n{status.get('error', 'служба не запущена')}"
                    )
                self.fix_action.setEnabled(ok)
                self.force_action.setEnabled(ok)
                self.start_action.setEnabled(not ok)
                self.stop_action.setEnabled(ok)
                self.restart_action.setEnabled(ok)

            # -- actions --------------------------------------------------
            def _activated(self, reason):
                if reason == QSystemTrayIcon.ActivationReason.Trigger:
                    # Settings live in the context menu now; a left click
                    # opens the same menu instead of a separate window.
                    self.menu.popup(QCursor.pos())
                elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
                    _run_detached(
                        _lapskey_binary(), ["trigger", "--force", "--quiet"]
                    )

            def open_settings(self, initial_tab: int = 0):
                # One window only: repeated clicks must raise the existing
                # settings window instead of spawning more copies.
                dialog = self._settings_dialog
                if dialog is not None:
                    try:
                        dialog.tabs.setCurrentIndex(
                            max(0, min(initial_tab, dialog.tabs.count() - 1))
                        )
                    except (AttributeError, RuntimeError):
                        pass
                    dialog.show()
                    dialog.raise_()
                    dialog.activateWindow()
                    handle = dialog.windowHandle()
                    if handle is not None:
                        with contextlib.suppress(RuntimeError):
                            handle.requestActivate()
                    return

                dialog = SettingsDialog(self.cfg, None, initial_tab)
                self._settings_dialog = dialog
                dialog.finished.connect(self._settings_finished)
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                handle = dialog.windowHandle()
                if handle is not None:
                    with contextlib.suppress(RuntimeError):
                        handle.requestActivate()

            def _settings_finished(self, _result: int):
                dialog = self._settings_dialog
                self._settings_dialog = None
                if dialog is not None and getattr(dialog, "saved_path", None):
                    self.cfg = config_mod.load(dialog.saved_path)
                    # Give systemd a moment to restart the daemon.
                    from PyQt6.QtCore import QTimer

                    QTimer.singleShot(
                        1200,
                        lambda: self.worker.updated.emit(
                            daemon_request({"cmd": "status"}, timeout=1.0)
                        ),
                    )

            def open_kde_keys(self):
                if not _run_detached("kcmshell6", ["kcm_keys"]):
                    _run_detached("systemsettings", [])

            def show_doctor(self):
                _run_and_show(
                    None,
                    "LapSKey — doctor",
                    [_lapskey_binary(), "doctor"],
                    timeout=60,
                )

            def show_log(self):
                text = ""
                try:
                    result = subprocess.run(
                        [
                            "journalctl",
                            "--user",
                            "-u",
                            "lapskey.service",
                            "-n",
                            "300",
                            "--no-pager",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    text = result.stdout + result.stderr
                except (OSError, subprocess.TimeoutExpired) as exc:
                    text = str(exc)

                from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

                dialog = QDialog(None)
                dialog.setWindowTitle("LapSKey — журнал")
                dialog.resize(820, 560)
                layout = QVBoxLayout(dialog)
                view = QPlainTextEdit()
                view.setReadOnly(True)
                view.setPlainText(_strip_ansi(text))
                layout.addWidget(view)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
                buttons.rejected.connect(dialog.reject)
                layout.addWidget(buttons)
                dialog.exec()

            def _toggle_autostart(self, enabled: bool):
                action = "enable" if enabled else "disable"
                _systemctl(action, "lapskey-tray.service")

            def about(self):
                QMessageBox.about(
                    None,
                    f"О {APP_NAME}",
                    f"<b>{APP_NAME}</b> {__version__}<br><br>"
                    "Исправление раскладки для KDE Plasma / Wayland.<br>"
                    "Демон: lapskey.service<br>"
                    "Трей: lapskey-tray.service",
                )

            def quit(self):
                self.quitting = True
                self.worker.requestInterruption()
                self.worker.wait(1500)
                self.tray.hide()
                app.quit()

        return _Controller()


def run_tray(cfg: Config) -> int:
    try:
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
    except ImportError:
        print(
            "lapskey: для трея нужен python-pyqt6 (sudo pacman -S python-pyqt6)",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    try:
        QGuiApplication.setDesktopFileName("lapskey")
    except AttributeError:
        pass

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            APP_NAME,
            "Системный трей недоступен. Запустите приложение в сессии KDE.",
        )
        return 1

    from PyQt6.QtCore import QLockFile

    lock_path = config_mod.runtime_dir() / "lapskey-tray.lock"
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(100):
        QMessageBox.information(
            None, APP_NAME, "Значок LapSKey уже запущен."
        )
        return 0

    controller = TrayController(app, cfg)
    try:
        return app.exec()
    finally:
        controller.quitting = True
        lock.unlock()
