"""Configuration loading.

The on-disk format is TOML (Python 3.11+ ships ``tomllib``).  Every option has
a sane default, so the daemon works even without a config file.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "lapskey"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    # Fallback for odd environments (e.g. running from cron without a session)
    path = Path(f"/tmp/{APP_NAME}-{os.getuid()}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def socket_path() -> Path:
    override = os.environ.get("LAPSKEY_SOCKET")
    if override:
        return Path(override)
    return runtime_dir() / f"{APP_NAME}.sock"


@dataclass
class GeneralConfig:
    log_level: str = "info"
    notify: bool = True
    notify_errors: bool = True


@dataclass
class HotkeyConfig:
    # kde   - KDE global shortcut calls `lapskey trigger` (recommended)
    # evdev - the daemon listens to /dev/input directly (see README caveat)
    # both  - do both
    mode: str = "kde"
    evdev_key: str = "KEY_F12"
    evdev_ctrl: bool = False
    evdev_shift: bool = False
    evdev_alt: bool = False


@dataclass
class InputConfig:
    # auto | buffer | clipboard
    acquisition: str = "auto"
    copy_combo: str = "ctrl+insert"
    select_word_combo: str = "ctrl+shift+left"
    primary_selection: bool = True
    copy_probe: bool = True
    restore_clipboard: bool = True
    clipboard_timeout_ms: int = 350
    selection_max_chars: int = 200


@dataclass
class FixConfig:
    # smart  - convert only when the other layout looks clearly better
    # always - every press converts (dangerous, but predictable)
    mode: str = "smart"
    score_threshold: float = 1.5
    double_press_ms: int = 0
    switch_layout: bool = True
    # auto | keys | clipboard
    insert: str = "auto"
    paste_combo: str = "ctrl+v"
    inter_key_delay_ms: int = 2
    layout_switch_timeout_ms: int = 500
    modifier_release_timeout_ms: int = 3000


@dataclass
class AutoConfig:
    enabled: bool = False
    min_word_length: int = 3
    score_threshold: float = 5.0
    delay_ms: int = 140


@dataclass
class KeyboardConfig:
    devices: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: ["lapskey", "ydotool"])
    rescan_seconds: float = 10.0


@dataclass
class LayoutConfig:
    backend: str = "kde"


@dataclass
class MappingConfig:
    # Extra character pairs: [["q", "й"], ...]
    pairs: list[list[str]] = field(default_factory=list)


@dataclass
class DictionaryConfig:
    files: list[str] = field(default_factory=list)
    auto_detect: bool = True


@dataclass
class PathsConfig:
    wl_paste: str = "wl-paste"
    wl_copy: str = "wl-copy"
    ydotool: str = "ydotool"


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    input: InputConfig = field(default_factory=InputConfig)
    fix: FixConfig = field(default_factory=FixConfig)
    auto: AutoConfig = field(default_factory=AutoConfig)
    keyboard: KeyboardConfig = field(default_factory=KeyboardConfig)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)
    dictionaries: DictionaryConfig = field(default_factory=DictionaryConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    source_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return _dataclass_to_dict(self)


def _dataclass_to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {
            f.name: _dataclass_to_dict(getattr(value, f.name))
            for f in fields(value)
            if f.name not in ("source_path", "warnings")
        }
    return copy.deepcopy(value)


def _apply(target: Any, data: dict[str, Any], warnings: list[str], prefix: str = "") -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        name = key.replace("-", "_")
        if name not in known:
            warnings.append(f"unknown option {prefix}{key!r} (ignored)")
            continue
        current = getattr(target, name)
        if hasattr(current, "__dataclass_fields__"):
            if isinstance(value, dict):
                _apply(current, value, warnings, f"{prefix}{key}.")
            else:
                warnings.append(f"option {prefix}{key} must be a table (ignored)")
        else:
            setattr(target, name, value)


def load(path: Path | None = None) -> Config:
    cfg = Config()
    cfg.source_path = path or config_path()
    if not cfg.source_path.is_file():
        return cfg
    try:
        with cfg.source_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        cfg.warnings.append(f"cannot read {cfg.source_path}: {exc}")
        return cfg
    _apply(cfg, data, cfg.warnings)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    choices = {
        "hotkey.mode": (cfg.hotkey.mode, {"kde", "evdev", "both"}),
        "input.acquisition": (cfg.input.acquisition, {"auto", "buffer", "clipboard"}),
        "fix.mode": (cfg.fix.mode, {"smart", "always"}),
        "fix.insert": (cfg.fix.insert, {"auto", "keys", "clipboard"}),
    }
    for name, (value, allowed) in choices.items():
        if value not in allowed:
            cfg.warnings.append(f"{name}={value!r} is invalid, using the default")
            section, key = name.split(".")
            default = Config()
            setattr(getattr(cfg, section), key, getattr(getattr(default, section), key))

    if cfg.input.clipboard_timeout_ms < 20:
        cfg.input.clipboard_timeout_ms = 20
    if cfg.fix.inter_key_delay_ms < 0:
        cfg.fix.inter_key_delay_ms = 0
    if cfg.fix.score_threshold < 0:
        cfg.fix.score_threshold = 0.0
    if cfg.auto.min_word_length < 2:
        cfg.auto.min_word_length = 2


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write the complete configuration (used by the settings dialog).

    Comments from a hand-edited file are not preserved; the GUI becomes the
    source of truth after the first save.
    """
    target = path or cfg.source_path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = to_toml(cfg)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    cfg.source_path = target
    return target


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot serialise {value!r}")


def to_toml(cfg: Config) -> str:
    lines = [
        "# LapSKey configuration",
        "# Generated by `lapskey tray` (settings dialog) / `lapskey config`.",
        "",
    ]
    for section in fields(cfg):
        if section.name in ("source_path", "warnings"):
            continue
        value = getattr(cfg, section.name)
        if not hasattr(value, "__dataclass_fields__"):
            continue
        lines.append(f"[{section.name}]")
        for item in fields(value):
            lines.append(f"{item.name} = {_toml_value(getattr(value, item.name))}")
        lines.append("")
    return "\n".join(lines)


def write_default(path: Path | None = None, force: bool = False) -> Path:
    """Create a commented default config file."""
    target = path or config_path()
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return target


DEFAULT_CONFIG_TOML = '''\
# LapSKey for KDE Plasma / Wayland
# https://github.com/ (see README.md)

[general]
log_level = "info"          # debug | info | warning | error
notify = true               # show desktop notifications
notify_errors = true

[hotkey]
# kde   - recommended: bind `lapskey trigger` in KDE System Settings
# evdev - read the hotkey directly from /dev/input (the key also reaches
#         the focused application, use a combination you do not need there)
mode = "kde"
evdev_key = "KEY_F12"
evdev_ctrl = false
evdev_shift = false
evdev_alt = false

[input]
# auto      - PRIMARY selection -> evdev buffer -> clipboard probe
# buffer    - only the keystroke buffer (no clipboard access)
# clipboard - only select-previous-word + copy
acquisition = "auto"

copy_combo = "ctrl+insert"      # safe copy shortcut (no SIGINT in terminals)
select_word_combo = "ctrl+shift+left"  # select the word before the cursor
primary_selection = true        # read selected text from the PRIMARY selection
copy_probe = true               # fall back to copy if PRIMARY is unavailable
restore_clipboard = true
clipboard_timeout_ms = 350
selection_max_chars = 200       # refuse to auto-convert huge selections

[fix]
mode = "smart"                  # smart | always
score_threshold = 1.5           # higher = more conservative
double_press_ms = 0             # 0 = off; >0 enables "press twice to force"
switch_layout = true            # switch KDE layout after a fix
insert = "auto"                 # auto | keys | clipboard
paste_combo = "ctrl+v"          # only used by the clipboard fallback
inter_key_delay_ms = 2
layout_switch_timeout_ms = 500
modifier_release_timeout_ms = 3000

[auto]
# Automatic conversion of the last word after Space.  Off by default because
# false positives are much more annoying than a missed fix.
enabled = false
min_word_length = 3
score_threshold = 5.0
delay_ms = 140

[keyboard]
devices = []                    # empty = every readable keyboard
exclude = ["lapskey", "ydotool"]
rescan_seconds = 10

[layout]
backend = "kde"                 # KDE Plasma (org.kde.keyboard DBus service)

[mapping]
# Дополнительные/переопределяющие пары символов: [английский, русский].
# Стандартная раскладка ЙЦУКЕН уже встроена.
pairs = []

[dictionaries]
files = []                      # extra word lists, one word per line
auto_detect = true              # /usr/share/hunspell and friends

[paths]
wl_paste = "wl-paste"
wl_copy = "wl-copy"
ydotool = "ydotool"
'''
