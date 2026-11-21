#!/usr/bin/env python3
"""Build a static GitHub Pages bundle with an interactive 3D Station Max emulator.

Usage:
    python docs/demo/build_pages_demo.py [_site]
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

COVERS = [
    ("c1.svg", "#ff6a00", "#1b1124", "DRILL", "10H BASS"),
    ("c2.svg", "#ef4444", "#171722", "BRICK", "BOOT.IMG"),
    ("c3.svg", "#a855f7", "#141828", "BORIS", "4:00 AM"),
    ("c4.svg", "#3b82f6", "#10192b", "BACKUP", "FORGOT"),
    ("c5.svg", "#f59e0b", "#1a1412", "OAUTH", "LOCAL"),
    ("c6.svg", "#ec4899", "#1d1020", "48 Hz", "SUBBASS"),
    ("c7.svg", "#06b6d4", "#0f1c24", "5 GHz", "WIFI RIP"),
    ("c8.svg", "#10b981", "#10201a", "LO-FI", "KETTLE"),
    ("c9.svg", "#22c55e", "#0c1c13", "UID=0", "TELNET"),
    ("c10.svg", "#6366f1", "#121326", "1 GB", "OOM KILL"),
    ("m1.svg", "#ff6a00", "#221018", "MIX 1", "ADB BASS"),
    ("m2.svg", "#38bdf8", "#0f172a", "MIX 2", "UART 3AM"),
    ("m3.svg", "#f43f5e", "#240f1a", "MIX 3", "REPLAY"),
    ("m4.svg", "#8b5cf6", "#171128", "MIX 4", "PRE .50"),
    ("m5.svg", "#eab308", "#201b0d", "MIX 5", "PROD FRI"),
    ("m6.svg", "#14b8a6", "#0d2120", "MIX 6", "DNS RUN"),
]


def _cover_svg(accent: str, bg: str, top: str, bottom: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{bg}"/>
      <stop offset="100%" stop-color="#0a0a0f"/>
    </linearGradient>
    <radialGradient id="r" cx="50%" cy="45%" r="55%">
      <stop offset="0%" stop-color="{accent}" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="{accent}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="240" height="240" rx="28" fill="url(#g)"/>
  <circle cx="120" cy="112" r="95" fill="url(#r)"/>
  <circle cx="120" cy="112" r="56" fill="none" stroke="{accent}" stroke-width="4" stroke-opacity="0.7"/>
  <circle cx="120" cy="112" r="18" fill="{accent}"/>
  <g fill="{accent}" fill-opacity="0.85">
    <rect x="44" y="182" width="10" height="22" rx="4"/>
    <rect x="62" y="170" width="10" height="34" rx="4"/>
    <rect x="80" y="176" width="10" height="28" rx="4"/>
    <rect x="98" y="164" width="10" height="40" rx="4"/>
    <rect x="116" y="172" width="10" height="32" rx="4"/>
    <rect x="134" y="166" width="10" height="38" rx="4"/>
    <rect x="152" y="178" width="10" height="26" rx="4"/>
    <rect x="170" y="170" width="10" height="34" rx="4"/>
    <rect x="188" y="184" width="10" height="20" rx="4"/>
  </g>
  <text x="120" y="42" text-anchor="middle" fill="#ffffff" font-family="system-ui,sans-serif" font-weight="800" font-size="20" letter-spacing="2">{top}</text>
  <text x="120" y="226" text-anchor="middle" fill="{accent}" font-family="monospace" font-weight="700" font-size="14" letter-spacing="1.5">{bottom}</text>
</svg>
"""


def build_site(out_dir: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    static_src = root / "static"
    mock_src = root / "docs" / "demo" / "demo-mock.js"
    if not static_src.is_dir():
        raise FileNotFoundError(f"Missing {static_src}")
    if not mock_src.is_file():
        raise FileNotFoundError(f"Missing {mock_src}")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copytree(static_src, out_dir / "static")
    manifest = static_src / "manifest.webmanifest"
    if manifest.is_file():
        shutil.copy2(manifest, out_dir / "manifest.webmanifest")
    shutil.copy2(mock_src, out_dir / "static" / "demo-mock.js")

    covers_dir = out_dir / "static" / "demo-covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    for fname, accent, bg, top, bottom in COVERS:
        (covers_dir / fname).write_text(_cover_svg(accent, bg, top, bottom), encoding="utf-8")

    index_src = (static_src / "index.html").read_text(encoding="utf-8")
    anchor = '<script src="static/panel-context.js"></script>'
    if anchor not in index_src:
        raise RuntimeError("Could not find panel-context.js script tag in static/index.html")
    html = index_src.replace(
        anchor,
        anchor + '\n<script src="static/demo-mock.js"></script>',
        1,
    ).replace(
        "<title>yapanel-web_max</title>",
        "<title>yapanel-web_max — Интерактивный 3D-эмулятор</title>",
        1,
    )
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return out_dir


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_site")
    out = build_site(target)
    print(f"Built GitHub Pages demo site in: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
