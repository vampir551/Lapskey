"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__, config as config_mod, mapping
from .config import Config
from .detect import Detector
from .layout import KdeLayouts
from .util import LapsKeyError, decode, run_command, which

log = logging.getLogger(__name__)


def configure_logging(level: str = "info") -> None:
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


async def _request(payload: dict, timeout: float = 10.0) -> dict:
    path = config_mod.socket_path()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=1.5
        )
    except (FileNotFoundError, ConnectionRefusedError):
        raise LapsKeyError(
            f"демон не запущен (сокет {path}). "
            "Запустите: systemctl --user start lapskey"
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise LapsKeyError(f"не удалось подключиться к демону: {exc}")
    try:
        writer.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        raise LapsKeyError("демон не ответил вовремя")
    except (ConnectionError, OSError) as exc:
        raise LapsKeyError(f"соединение с демоном потеряно: {exc}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    if not raw:
        raise LapsKeyError("пустой ответ демона")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LapsKeyError(f"некорректный ответ демона: {exc}")


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    from .daemon import Daemon

    configure_logging(cfg.general.log_level)
    daemon = Daemon(cfg)
    try:
        asyncio.run(daemon.run())
    except LapsKeyError as exc:
        print(f"lapskey: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_trigger(args: argparse.Namespace, cfg: Config) -> int:
    async def go() -> int:
        log.info(
            "trigger request: force=%s selection=%s", args.force, args.selection
        )
        result = await _request(
            {
                "cmd": "trigger",
                "force": bool(args.force),
                "selection": bool(args.selection),
                "source": "cli",
            }
        )
        if not result.get("ok"):
            if not args.quiet:
                print(f"Ошибка: {result.get('error', 'неизвестная ошибка')}", file=sys.stderr)
            return 1
        if not args.quiet:
            if result.get("changed"):
                print(
                    f"Исправлено: «{result.get('original', '')}» → "
                    f"«{result.get('converted', '')}» "
                    f"[{result.get('insert')}, {result.get('target')}]"
                )
            else:
                print(result.get("message", "изменений нет"))
        return 0

    try:
        return asyncio.run(go())
    except LapsKeyError as exc:
        if not args.quiet:
            print(f"lapskey: {exc}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    try:
        result = asyncio.run(_request({"cmd": "status"}))
    except LapsKeyError as exc:
        print(f"lapskey: {exc}", file=sys.stderr)
        return 1
    if not result.get("ok"):
        print(f"Ошибка: {result.get('error')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"PID:            {result.get('pid')}")
    print(f"Uptime:         {result.get('uptime_s')} s")
    print(f"Socket:         {result.get('socket')}")
    print(f"Config:         {result.get('config')}")
    print(f"Injector:       {result.get('injector') or 'НЕТ'}")
    if result.get("injector_error"):
        print(f"  error:        {result['injector_error']}")
    monitor = result.get("monitor") or {}
    print(f"Input monitor:  {'OK' if monitor.get('available') else 'НЕТ'}")
    if monitor.get("error"):
        print(f"  error:        {monitor['error']}")
    mods = monitor.get("pressed_modifiers") or []
    print(
        f"  modifiers:    {'held: ' + ', '.join(mods) if mods else 'released'}"
    )
    print(
        f"  buffer:       {monitor.get('buffer_keys', 0)} keys, "
        f"selection={'yes' if monitor.get('selection_possible') else 'no'}"
    )
    clip = result.get("clipboard") or {}
    print(f"Clipboard:      paste={clip.get('paste')} copy={clip.get('copy')}")
    layout = result.get("layout")
    if layout:
        print(
            f"Layout:         {layout.get('code')} / {layout.get('lang')} "
            f"({layout.get('name')})"
        )
    else:
        print("Layout:         неизвестно (org.kde.keyboard недоступен)")
    print(f"Auto mode:      {'on' if result.get('auto') else 'off'}")
    print("Dictionaries:")
    for item in result.get("dictionaries") or []:
        print(f"  - {item}")
    return 0


def cmd_convert(args: argparse.Namespace, cfg: Config) -> int:
    text = args.text
    if text is None:
        text = sys.stdin.read()
    detector = Detector(
        extra_files=cfg.dictionaries.files,
        auto_detect=cfg.dictionaries.auto_detect,
    )
    detector.load_dictionaries()
    decision = detector.convert_text(
        text,
        threshold=args.threshold if args.threshold is not None else cfg.fix.score_threshold,
        force=args.force,
        source_lang=args.layout,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "original": decision.original,
                    "converted": decision.converted,
                    "target": decision.target,
                    "changed": decision.changed,
                    "words": [
                        {
                            "word": w.original,
                            "converted": w.converted,
                            "convert": w.convert,
                            "source": w.source,
                            "target": w.target,
                            "score": round(w.original_score, 2),
                            "converted_score": round(w.converted_score, 2),
                        }
                        for w in decision.words
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(decision.converted)
    return 0


async def _doctor(cfg: Config) -> int:
    problems = 0
    warnings = 0

    def ok(message: str, hint: str = "") -> None:
        print(f"\033[32m OK \033[0m {message}")

    def warn(message: str, hint: str = "") -> None:
        nonlocal warnings
        warnings += 1
        print(f"\033[33mWARN\033[0m {message}")
        if hint:
            print(f"     {hint}")

    def fail(message: str, hint: str = "") -> None:
        nonlocal problems
        problems += 1
        print(f"\033[31mFAIL\033[0m {message}")
        if hint:
            print(f"     {hint}")

    print("LapSKey doctor\n")

    if sys.version_info >= (3, 11):
        ok(f"Python {sys.version.split()[0]}")
    else:
        fail(f"Python {sys.version.split()[0]}", "нужен Python 3.11+ (tomllib)")

    try:
        import evdev  # noqa: F401

        ok("python-evdev установлен")
    except ImportError:
        fail("python-evdev не установлен", "sudo pacman -S python-evdev")

    if Path("/dev/uinput").exists():
        if os.access("/dev/uinput", os.W_OK):
            ok("/dev/uinput доступен для записи")
        else:
            fail(
                "/dev/uinput недоступен для записи",
                "добавьте пользователя в группу input и правила udev (см. README)",
            )
    else:
        fail("/dev/uinput отсутствует", "sudo modprobe uinput")

    try:
        from .injector import create_injector

        probe = await asyncio.to_thread(create_injector, 0, cfg.paths.ydotool)
        ok(f"эмуляция клавиатуры: {probe.name}")
        probe.close()
    except LapsKeyError as exc:
        fail("эмуляция клавиатуры недоступна", str(exc))
    except Exception as exc:  # noqa: BLE001
        fail(f"эмуляция клавиатуры: {exc}")

    try:
        groups = os.getgroups()
        import grp

        try:
            input_gid = grp.getgrnam("input").gr_gid
            if input_gid in groups:
                ok("пользователь входит в группу input")
            else:
                warn(
                    "пользователь не входит в группу input",
                    "sudo usermod -aG input $USER (нужен повторный вход)",
                )
        except KeyError:
            warn("группа input не найдена")
    except Exception:  # noqa: BLE001
        pass

    try:
        import evdev

        paths = evdev.list_devices()
        readable = 0
        keyboards = 0
        for path in paths:
            try:
                device = evdev.InputDevice(path)
            except (PermissionError, OSError):
                continue
            try:
                caps = device.capabilities()
                keys = caps.get(evdev.ecodes.EV_KEY, [])
                if evdev.ecodes.KEY_A in keys and evdev.ecodes.KEY_SPACE in keys:
                    keyboards += 1
                readable += 1
            finally:
                device.close()
        if keyboards:
            ok(f"читаемых клавиатур: {keyboards} (всего устройств: {readable})")
        elif readable:
            warn(
                "клавиатуры не найдены среди читаемых устройств",
                "проверьте группу input",
            )
        else:
            fail(
                "нет доступа к /dev/input",
                "sudo usermod -aG input $USER; перезайдите в систему",
            )
    except Exception as exc:  # noqa: BLE001
        fail(f"не удалось прочитать /dev/input: {exc}")

    layouts = KdeLayouts(cfg.fix.layout_switch_timeout_ms)
    if which("gdbus") is None:
        fail("gdbus не найден", "sudo pacman -S glib2")
    else:
        current = await layouts.current()
        if current:
            ok(f"раскладка KDE: {current.code} / {current.lang} ({current.name})")
            if current.lang not in ("ru", "en"):
                warn(
                    "активная раскладка не ru/en",
                    "добавьте русскую и английскую раскладки в KDE",
                )
        else:
            fail(
                "сервис org.kde.keyboard недоступен",
                "нужен KDE Plasma (Wayland/X11) и активная сессия",
            )

    for tool, hint in (
        ("wl-paste", "sudo pacman -S wl-clipboard"),
        ("wl-copy", "sudo pacman -S wl-clipboard"),
        ("notify-send", "sudo pacman -S libnotify"),
    ):
        if which(tool):
            ok(f"{tool} найден")
        else:
            warn(f"{tool} не найден", hint)

    if os.environ.get("WAYLAND_DISPLAY"):
        ok(f"Wayland: {os.environ['WAYLAND_DISPLAY']}")
    else:
        warn("WAYLAND_DISPLAY не задан", "утилита рассчитана на Wayland/KDE")

    detector = Detector(
        extra_files=cfg.dictionaries.files,
        auto_detect=cfg.dictionaries.auto_detect,
    )
    await asyncio.to_thread(detector.load_dictionaries)
    dicts = detector.dictionary_info()
    if dicts:
        ok("словари: " + ", ".join(dicts))
    else:
        warn(
            "hunspell-словари не найдены, работает встроенный мини-словарь",
            "sudo pacman -S hunspell-en_us hunspell-ru",
        )

    try:
        status = await _request({"cmd": "status"}, timeout=3.0)
        if status.get("ok"):
            ok(f"демон запущен (PID {status.get('pid')})")
        else:
            fail(f"демон отвечает ошибкой: {status.get('error')}")
    except LapsKeyError as exc:
        warn(str(exc))

    try:
        rc, out, _err = await run_command(
            ["systemctl", "--user", "is-active", "lapskey.service"], timeout=2.0
        )
        state = decode(out)
        if rc == 0 and state == "active":
            ok("systemd user service активен")
        else:
            warn(
                f"systemd user service: {state or 'не активен'}",
                "systemctl --user enable --now lapskey.service",
            )
    except LapsKeyError:
        pass

    print()
    if problems:
        print(f"Итог: {problems} критичных проблем, {warnings} предупреждений")
        return 1
    if warnings:
        print(f"Итог: работает, но есть {warnings} предупреждений")
    else:
        print("Итог: всё в порядке")
    return 0


def cmd_reload(args: argparse.Namespace, cfg: Config) -> int:
    async def go() -> int:
        result = await _request({"cmd": "reload"}, timeout=15.0)
        if not result.get("ok"):
            print(f"Ошибка: {result.get('error')}", file=sys.stderr)
            return 1
        print(result.get("message", "configuration reloaded"))
        return 0

    try:
        return asyncio.run(go())
    except LapsKeyError as exc:
        print(f"lapskey: {exc}", file=sys.stderr)
        return 1


def cmd_tray(args: argparse.Namespace, cfg: Config) -> int:
    from .tray import run_tray

    return run_tray(cfg)


def cmd_stop(args: argparse.Namespace, cfg: Config) -> int:
    async def go() -> int:
        result = await _request({"cmd": "stop"}, timeout=3.0)
        print(result.get("message", "остановлено"))
        return 0

    try:
        return asyncio.run(go())
    except LapsKeyError as exc:
        print(f"lapskey: {exc}", file=sys.stderr)
        return 1


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    return asyncio.run(_doctor(cfg))


def cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    path = Path(args.config) if args.config else config_mod.config_path()
    if args.init:
        target = config_mod.write_default(path, force=args.force)
        print(f"Конфигурация: {target}")
        return 0
    print(f"Путь: {path}")
    if path.is_file():
        print(path.read_text(encoding="utf-8"))
    else:
        print("Файла нет; используется встроенная конфигурация.")
        print(f"Создать: lapskey config --init")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lapskey",
        description="LapSKey для KDE Plasma / Wayland",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="путь к config.toml (по умолчанию ~/.config/lapskey/config.toml)",
    )
    parser.add_argument("--version", action="version", version=f"lapskey {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="запустить демон (используется systemd)")

    trigger = sub.add_parser("trigger", help="исправить последнее слово/выделение")
    trigger.add_argument("--force", action="store_true", help="конвертировать принудительно")
    trigger.add_argument("--selection", action="store_true", help="читать только выделение")
    trigger.add_argument("--quiet", action="store_true")

    convert = sub.add_parser("convert", help="конвертировать текст и выйти (без демона)")
    convert.add_argument("text", nargs="?", help="текст; если не задан — читать stdin")
    convert.add_argument("--force", action="store_true")
    convert.add_argument("--threshold", type=float, default=None)
    convert.add_argument(
        "--layout",
        choices=["en", "ru"],
        default=None,
        help="текущая раскладка (для преобразования одних только знаков препинания)",
    )
    convert.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="состояние демона")
    status.add_argument("--json", action="store_true")

    sub.add_parser("stop", help="остановить демон")
    sub.add_parser("reload", help="перечитать конфигурацию демоном")
    sub.add_parser("tray", help="запустить значок в трее и настройки")
    sub.add_parser("doctor", help="проверить окружение и права")

    cfg = sub.add_parser("config", help="показать/создать конфигурацию")
    cfg.add_argument("--init", action="store_true", help="создать файл с настройками")
    cfg.add_argument("--force", action="store_true", help="перезаписать существующий файл")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    cfg = config_mod.load(Path(args.config) if args.config else None)
    configure_logging(cfg.general.log_level)
    for warning in cfg.warnings:
        log.warning("%s", warning)
    for warning in mapping.apply_custom_pairs(cfg.mapping.pairs):
        log.warning("%s", warning)

    handlers = {
        "run": cmd_run,
        "trigger": cmd_trigger,
        "status": cmd_status,
        "convert": cmd_convert,
        "stop": cmd_stop,
        "reload": cmd_reload,
        "tray": cmd_tray,
        "doctor": cmd_doctor,
        "config": cmd_config,
    }
    handler = handlers.get(command)
    if handler is None:
        parser.error(f"unknown command: {command}")
        return 2
    return handler(args, cfg)
