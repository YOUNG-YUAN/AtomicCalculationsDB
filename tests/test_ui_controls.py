"""v2.5.1 UI polish: checkmarks, wheel-safe fields and visible spin arrows."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from ui import m3_theme
from ui.controls import WheelSafeComboBox, WheelSafeSpinBox


def _wheel(widget, delta: int = -120) -> None:
    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    widget.wheelEvent(event)


def test_theme_uses_checkmark_and_visible_spin_chevrons():
    stylesheet = m3_theme.load_stylesheet()
    assets = Path(m3_theme.__file__).with_name("assets")
    for name in ("checkmark.svg", "chevron-up.svg", "chevron-down.svg"):
        assert (assets / name).is_file(), name
        assert name in stylesheet
    assert "QCheckBox::indicator:checked" in stylesheet
    assert "QSpinBox::up-arrow" in stylesheet


def test_closed_combo_ignores_mouse_wheel(qapp):
    combo = WheelSafeComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    _wheel(combo)
    assert combo.currentIndex() == 0


def test_unfocused_spinbox_ignores_mouse_wheel(qapp):
    spin = WheelSafeSpinBox()
    spin.setRange(0, 100)
    spin.setValue(10)
    _wheel(spin)
    assert spin.value() == 10
