"""Render the favicon set and the Open Graph image from their SVG sources.

    python -m scripts.build_icons

Needs macOS (``sips`` draws the SVGs). Sources: ``frontend/icon.svg`` (rounded tile),
``assets/brand/icon-maskable.svg`` (full-bleed tile for launchers and iOS, which crop
it themselves) and ``assets/brand/og-image.svg``. Re-run after changing any of them and
commit the PNG/JPEG/ICO files; bump ``CACHE_NAME`` in ``frontend/sw.js``.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent.parent
FRONTEND: Final = ROOT / "frontend"
BRAND: Final = ROOT / "assets" / "brand"

# (source, output, width, height)
RENDERS: Final = (
    (FRONTEND / "icon.svg", FRONTEND / "icon-192.png", 192, 192),
    (FRONTEND / "icon.svg", FRONTEND / "icon-512.png", 512, 512),
    (BRAND / "icon-maskable.svg", FRONTEND / "icon-maskable-512.png", 512, 512),
    (BRAND / "icon-maskable.svg", FRONTEND / "apple-touch-icon.png", 180, 180),
    (BRAND / "og-image.svg", FRONTEND / "og-image.jpg", 1200, 630),
)
ICO_SIZES: Final = (16, 32, 48)


def render(source: Path, output: Path, width: int, height: int) -> None:
    """Draw ``source`` at the given size as a PNG, or a JPEG for a ``.jpg`` output (link previews load faster)."""
    options = ["-s", "format", "jpeg", "-s", "formatOptions", "85"] if output.suffix == ".jpg" else ["-s", "format", "png"]
    subprocess.run(  # noqa: S603 - fixed tool, paths from this file
        ["sips", *options, "-z", str(height), str(width), str(source), "--out", str(output)],
        check=True, capture_output=True,
    )


def write_ico(pngs: list[tuple[int, bytes]], output: Path) -> None:
    """Pack PNG images into one .ico file (PNG entries, supported by every current browser)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(header) + 16 * len(pngs)
    entries, images = b"", b""
    for size, data in pngs:
        # A width or height of 0 means 256 in the ICO format.
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        images += data
        offset += len(data)
    output.write_bytes(header + entries + images)


def main() -> int:
    if sys.platform != "darwin":
        sys.stderr.write("build_icons needs macOS (sips).\n")
        return 1
    for source, output, width, height in RENDERS:
        render(source, output, width, height)
        sys.stdout.write(f"{output.relative_to(ROOT)}\n")
    with tempfile.TemporaryDirectory() as folder:
        pngs = []
        for size in ICO_SIZES:
            path = Path(folder) / f"{size}.png"
            render(FRONTEND / "icon.svg", path, size, size)
            pngs.append((size, path.read_bytes()))
    write_ico(pngs, FRONTEND / "favicon.ico")
    sys.stdout.write("frontend/favicon.ico\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
