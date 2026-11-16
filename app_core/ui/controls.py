"""Small input widgets with predictable mouse-wheel behaviour.

Qt changes a closed combo box / spin box when the wheel passes over it, even
when the widget was never clicked.  That is risky on a settings page: a user
who scrolls the page can silently change a model or timeout.  These subclasses
only forward the wheel while the combo popup is open, or while a spin box has
keyboard focus.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QWidget,
)


class SwitchButton(QAbstractButton):
    """Animated/styled pill toggle switch button."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(50, 26)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        checked = self.isChecked()

        # Track: green when checked, light grey when unchecked
        track_color = QColor("#86efac") if checked else QColor("#e5e7eb")
        painter.setBrush(QBrush(track_color))
        radius = self.height() / 2.0
        painter.drawRoundedRect(0, 0, self.width(), self.height(), radius, radius)

        # Thumb / Handle: vibrant green when checked, grey when unchecked
        thumb_color = QColor("#16a34a") if checked else QColor("#6b7280")
        painter.setBrush(QBrush(thumb_color))
        margin = 2.5
        diameter = self.height() - margin * 2.0
        thumb_x = self.width() - margin - diameter if checked else margin
        painter.drawEllipse(thumb_x, margin, diameter, diameter)
        painter.end()


class StrategyToggleSwitch(QWidget):
    """Toggle switch with status label: '当前节点已开启' / '当前节点已关闭'."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.switch = SwitchButton(self)
        self.switch.setChecked(checked)
        self.label = QLabel(self)

        layout.addWidget(self.switch)
        layout.addWidget(self.label)
        layout.addStretch(1)

        self.switch.toggled.connect(self._on_toggled)
        self._update_label(checked)

    def isChecked(self) -> bool:
        return self.switch.isChecked()

    def setChecked(self, checked: bool) -> None:
        self.switch.setChecked(checked)
        self._update_label(checked)

    def _on_toggled(self, checked: bool) -> None:
        self._update_label(checked)
        self.toggled.emit(checked)

    def _update_label(self, checked: bool) -> None:
        if checked:
            self.label.setText("当前节点已开启")
            self.label.setStyleSheet(
                "color: #16a34a; font-weight: bold; font-size: 13px;"
            )
        else:
            self.label.setText("当前节点已关闭")
            self.label.setStyleSheet(
                "color: #dc2626; font-weight: bold; font-size: 13px;"
            )


class WheelSafeComboBox(QComboBox):
    """Ignore wheel events until the popup is actually open."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        view = self.view()
        if view is not None and view.isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class WheelSafeSpinBox(QSpinBox):
    """Do not change a spin box merely because the cursor passed over it."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class WheelSafeDoubleSpinBox(QDoubleSpinBox):
    """Double-precision counterpart of :class:`WheelSafeSpinBox`."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


__all__ = [
    "SwitchButton",
    "StrategyToggleSwitch",
    "WheelSafeComboBox",
    "WheelSafeDoubleSpinBox",
    "WheelSafeSpinBox",
]
