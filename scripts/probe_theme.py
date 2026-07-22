#!/usr/bin/env python3
"""Theme verification probe.

Two parts:

1. **Static audit** — token-table parity, QSS template coverage, no stray hex
   literals in ui/, and the video-surface constants in video/ffmpeg_player.py
   still matching the VIDEO tokens they mirror.
2. **Render pass** — every top-level window built offscreen in both themes
   and saved as PNG for eyeballing (white-on-white, unstyled native widgets,
   broken calendar cells).

Usage:
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/probe_theme.py [outdir]
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def static_audit() -> None:
    from ui.theme import DARK_TOKENS, LIGHT_TOKENS, VIDEO_TOKENS

    print("\n== static audit ==")

    dark, light = set(DARK_TOKENS), set(LIGHT_TOKENS)
    check(dark == light, "DARK/LIGHT key parity",
          f"dark-only={sorted(dark - light)} light-only={sorted(light - dark)}")

    overlap = dark & set(VIDEO_TOKENS)
    check(not overlap, "no VIDEO/chrome token name collisions", str(sorted(overlap)))

    tmpl = (ROOT / "resources" / "styles.qss.tmpl").read_text(encoding="utf-8")
    used = set(re.findall(r"(?<!\$)\$\{(\w+)\}", tmpl))  # skip $$-escaped
    known = dark | set(VIDEO_TOKENS)
    check(used <= known, "QSS references only defined tokens",
          str(sorted(used - known)))
    check(not re.search(r"#[0-9a-fA-F]{6}\b", tmpl), "QSS has no literal colors")

    # ui/*.py must go through the token tables. ui/theme.py is the one place
    # where literals belong.
    offenders = []
    for path in sorted((ROOT / "ui").glob("*.py")):
        if path.name == "theme.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"#[0-9a-fA-F]{6}\b", line):
                offenders.append(f"{path.name}:{n}")
    check(not offenders, "no hex literals in ui/ outside theme.py", str(offenders))

    # video/ is a leaf module and mirrors two VIDEO tokens as local constants
    # (see the comment there). Guard against them drifting apart.
    player = (ROOT / "video" / "ffmpeg_player.py").read_text(encoding="utf-8")
    for const, token in (("_SURFACE_BG", "video_bg"),
                         ("_SURFACE_TEXT", "video_text_muted")):
        m = re.search(rf'{const} = QColor\("(#[0-9a-fA-F]{{6}})"\)', player)
        check(m is not None and m.group(1) == VIDEO_TOKENS[token],
              f"{const} == VIDEO_TOKENS[{token!r}]",
              f"{m.group(1) if m else 'not found'} vs {VIDEO_TOKENS[token]}")


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast_audit() -> None:
    """WCAG AA (4.5:1) for the text-on-background pairs we actually ship."""
    from ui.theme import DARK_TOKENS, LIGHT_TOKENS

    print("\n== contrast audit ==")
    pairs = [
        ("text_primary", "bg_base"),
        ("text_primary", "bg_surface"),
        ("text_secondary", "bg_surface"),
        ("text_muted", "bg_base"),
        ("text_muted", "bg_surface"),
        ("accent_fg", "accent"),
        ("danger", "bg_base"),
        ("success", "bg_base"),
        ("warning", "bg_base"),
        ("warning_fg", "warning_fill"),
        ("selection_fg", "selection_bg"),
        ("tooltip_fg", "tooltip_bg"),
    ]
    for mode, table in (("dark", DARK_TOKENS), ("light", LIGHT_TOKENS)):
        for fg, bg in pairs:
            l1, l2 = _luminance(table[fg]), _luminance(table[bg])
            hi, lo = max(l1, l2), min(l1, l2)
            ratio = (hi + 0.05) / (lo + 0.05)
            check(ratio >= 4.5, f"{mode}: {fg} on {bg}", f"{ratio:.2f}:1")


def render_pass(outdir: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from app import nvr_config as nvrcfg
    from ui.theme import theme

    print("\n== render pass ==")
    outdir.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme.apply(app, "dark", source="probe")

    from ui.camera_form import CameraForm
    from ui.main_window import MainWindow
    from ui.nvr_form import NvrForm
    from ui.settings_dialog import SettingsDialog

    windows: list[tuple[str, object]] = []

    main = MainWindow()
    main.resize(1200, 700)
    main.show()
    windows.append(("main", main))

    for name, factory in (("settings", SettingsDialog),
                          ("camera_form", CameraForm),
                          ("nvr_form", NvrForm)):
        w = factory()
        w.show()
        windows.append((name, w))

    # The archive window needs a configured NVR; skip cleanly without one.
    nvrs = nvrcfg.load_nvrs()
    if nvrs:
        from app import secrets as keystore
        from app.nvr_config import nvr_keyring_user
        from ui.playback_view import PlaybackView
        nvr = nvrs[0]
        pwd = keystore.get_password(nvr_keyring_user(nvr.id)) or ""
        pv = PlaybackView(nvr, 1, "CAM01", pwd, parent=None)
        pv.resize(1200, 700)
        pv.show()
        windows.append(("playback", pv))
    else:
        print("  SKIP  playback window — no NVR configured")

    app.processEvents()
    for mode in ("dark", "light"):
        theme.apply(app, mode, source="probe")
        app.processEvents()
        for name, w in windows:
            path = outdir / f"{name}_{mode}.png"
            w.grab().save(str(path))
            print(f"  saved {path}")

    for _name, w in windows:
        w.close()


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "theme-shots"
    static_audit()
    contrast_audit()
    render_pass(outdir)
    print("\n" + ("ALL CHECKS PASSED" if not failures
                  else f"{len(failures)} FAILURE(S): {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
