"""Brand icon conversion and application-wide icon helpers.

The finalized vector logo lives at the project root.  At build/runtime we
derive a vector copy, PNG sizes for the UI and a multi-resolution Windows
``.ico`` for PyInstaller.  QtSvg is used so the conversion works on a clean
machine without ImageMagick or Pillow.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap

ASSET_DIR = Path(__file__).resolve().with_name("assets")
LOGO_NAME = "LOGO-AtomicCalculationsDB.svg"
PNG_SIZES = (32, 64, 256)
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def project_root() -> Path:
    # icon_assets.py -> ui -> app_core -> project root
    return Path(__file__).resolve().parents[2]


def source_logo() -> Path:
    return project_root() / LOGO_NAME


def _render_png_bytes(svg: Path, size: int) -> bytes:
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise RuntimeError(f"无法解析 SVG 图标：{svg}")
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    from PySide6.QtCore import QBuffer, QIODevice

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"PNG 图标写入失败：{size}px")
    buffer.close()
    return bytes(data)


def _write_ico(target: Path, png_images: list[bytes], sizes: list[int]) -> None:
    """Write a PNG-compressed multi-layer ICO (supported by Windows Vista+)."""
    count = len(png_images)
    offset = 6 + 16 * count
    directory = bytearray(struct.pack("<HHH", 0, 1, count))
    payload = bytearray()
    for size, data in zip(sizes, png_images):
        width = 0 if size >= 256 else size
        height = 0 if size >= 256 else size
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                width,
                height,
                0,
                0,
                1,
                32,
                len(data),
                offset,
            )
        )
        payload.extend(data)
        offset += len(data)
    target.write_bytes(bytes(directory + payload))


def ensure_assets(source: str | Path | None = None, *, force: bool = False) -> dict[str, Path]:
    """Generate logo.svg, PNG sizes and logo.ico.  Returns the asset paths."""
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    svg_source = Path(source) if source is not None else source_logo()
    logo_svg = ASSET_DIR / "logo.svg"
    if svg_source.is_file():
        if force or not logo_svg.exists() or svg_source.stat().st_mtime > logo_svg.stat().st_mtime:
            shutil.copy2(svg_source, logo_svg)
    elif not logo_svg.exists():
        raise FileNotFoundError(f"品牌图标不存在：{svg_source}")

    targets: dict[str, Path] = {"svg": logo_svg}
    for size in PNG_SIZES:
        target = ASSET_DIR / f"logo_{size}.png"
        if force or not target.exists():
            target.write_bytes(_render_png_bytes(logo_svg, size))
        targets[f"png_{size}"] = target

    ico = ASSET_DIR / "logo.ico"
    if force or not ico.exists():
        pngs = [_render_png_bytes(logo_svg, size) for size in ICO_SIZES]
        _write_ico(ico, pngs, list(ICO_SIZES))
    targets["ico"] = ico
    return targets


def logo_pixmap(size: int = 64) -> QPixmap:
    """Load a crisp logo pixmap, generating assets on first use."""
    try:
        assets = ensure_assets()
        path = assets.get(f"png_{size}") or assets["png_256"]
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            raise RuntimeError("logo pixmap load failed")
        if pixmap.width() != size:
            pixmap = pixmap.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        return pixmap
    except Exception:
        # A quiet fallback keeps the GUI usable in source-only checkouts.
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(Qt.GlobalColor.darkBlue)
        painter.drawRoundedRect(0, 0, size, size, size // 4, size // 4)
        painter.end()
        return pixmap


def app_icon() -> QIcon:
    try:
        assets = ensure_assets()
        icon = QIcon(str(assets["ico"]))
        if not icon.isNull():
            return icon
    except Exception:
        pass
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(size))
    return icon


def workbench_icon(size: int = 28) -> QIcon:
    """Material 3 reactor/pulse icon used by the scheduler navigation."""
    path = ASSET_DIR / "workbench.svg"
    if path.exists():
        try:
            from PySide6.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(str(path))
            image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            return QIcon(QPixmap.fromImage(image))
        except Exception:
            pass
    return QIcon(logo_pixmap(size))


__all__ = [
    "ASSET_DIR",
    "ICO_SIZES",
    "ensure_assets",
    "app_icon",
    "logo_pixmap",
    "workbench_icon",
    "source_logo",
]
