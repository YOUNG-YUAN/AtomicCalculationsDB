"""Visual 5-Step Stepper Widget & Mini Stepper for Document Pipeline.

Steps:
  ① 提取文字  ──  ② 提取关键信息  ──  ③ AI 快速提取  ──  ④ 写入数据库  ──  ⑤ AI 深度归纳

Glow Effects:
  - Step 4 Completed: Green Glow (冒绿光) -> Basic data ingested, ready for search
  - Step 5 Completed: Gold Glow (冒金光)  -> L1 Master Card synthesized, thoroughly processed
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from models.pipeline_stepper import (
    STEP_DEFINITIONS,
    DocumentPipelineStatus,
    GlowState,
    StepInfo,
    StepStatus,
    compute_document_pipeline_status,
)


# Colors
COLOR_GREEN = QColor("#2E7D32")        # Completed green
COLOR_GREEN_LIGHT = QColor("#E8F5E9")  # Background tint
COLOR_GREEN_GLOW = QColor(76, 175, 80, 160)
COLOR_GOLD = QColor("#D97706")         # Master gold
COLOR_GOLD_LIGHT = QColor("#FEF3C7")   # Gold tint
COLOR_GOLD_GLOW = QColor(245, 158, 11, 200)
COLOR_GRAY = QColor("#9E9E9E")         # Inactive gray
COLOR_GRAY_LIGHT = QColor("#F5F5F5")   # Pending background
COLOR_BLUE = QColor("#1976D2")         # Active blue
COLOR_RED = QColor("#D32F2F")          # Failed red


class StepProgressWidget(QWidget):
    """Full-featured interactive 5-step visual progress bar with dual-glow effects."""

    step_clicked = Signal(int)  # 1..5

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.status: DocumentPipelineStatus | None = None
        self._glow_effect: QGraphicsDropShadowEffect | None = None
        self._hover_step: int = 0
        self.setMouseTracking(True)
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._init_glow()

    def _init_glow(self) -> None:
        effect = QGraphicsDropShadowEffect(self)
        effect.setOffset(0, 0)
        effect.setBlurRadius(0)
        effect.setColor(QColor(0, 0, 0, 0))
        self.setGraphicsEffect(effect)
        self._glow_effect = effect

    def set_pipeline_status(self, status: DocumentPipelineStatus) -> None:
        self.status = status
        self._update_glow()
        self.update()

    def update_from_document(
        self,
        doc: dict[str, Any],
        cards: list[dict[str, Any]] | None = None,
    ) -> None:
        status = compute_document_pipeline_status(doc, cards)
        self.set_pipeline_status(status)

    def _update_glow(self) -> None:
        if not self._glow_effect or not self.status:
            return
        glow = self.status.glow
        if glow == GlowState.GOLD:
            self._glow_effect.setBlurRadius(26)
            self._glow_effect.setColor(COLOR_GOLD_GLOW)
        elif glow == GlowState.GREEN:
            self._glow_effect.setBlurRadius(22)
            self._glow_effect.setColor(COLOR_GREEN_GLOW)
        else:
            self._glow_effect.setBlurRadius(0)
            self._glow_effect.setColor(QColor(0, 0, 0, 0))

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        hovered = self._hit_test_step(pos.x(), pos.y())
        if hovered != self._hover_step:
            self._hover_step = hovered
            self.setCursor(Qt.PointingHandCursor if hovered > 0 else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover_step != 0:
            self._hover_step = 0
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            pos = event.position()
            step = self._hit_test_step(pos.x(), pos.y())
            if step > 0:
                self.step_clicked.emit(step)
        super().mousePressEvent(event)

    def _node_geometry(self, index: int) -> tuple[float, float, float]:
        """Return (cx, cy, radius) for 1-based step index."""
        w = float(self.width())
        margin_x = 42.0
        cy = 28.0
        radius = 16.0
        step_w = (w - margin_x * 2) / 4.0
        cx = margin_x + (index - 1) * step_w
        return cx, cy, radius

    def _hit_test_step(self, x: float, y: float) -> int:
        for idx in range(1, 6):
            cx, cy, r = self._node_geometry(idx)
            # expanded hit box for better clickability
            if (x - cx) ** 2 + (y - cy) ** 2 <= (r + 14) ** 2:
                return idx
        return 0

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = float(self.width())
        h = float(self.height())

        # 1. Subtle background card
        bg_path = QPainterPath()
        bg_path.addRoundedRect(QRectF(2, 2, w - 4, h - 4), 10, 10)
        glow = self.status.glow if self.status else GlowState.NONE
        if glow == GlowState.GOLD:
            painter.fillPath(bg_path, QBrush(QColor(254, 252, 232, 210)))
            painter.strokePath(bg_path, QPen(QColor(245, 158, 11, 140), 1.5))
        elif glow == GlowState.GREEN:
            painter.fillPath(bg_path, QBrush(QColor(240, 253, 244, 210)))
            painter.strokePath(bg_path, QPen(QColor(76, 175, 80, 140), 1.5))
        else:
            painter.fillPath(bg_path, QBrush(QColor(250, 250, 250, 240)))
            painter.strokePath(bg_path, QPen(QColor(226, 232, 240), 1.0))

        steps = self.status.steps if self.status else [
            StepInfo(i, k.value, name, StepStatus.PENDING)
            for i, k, name, _ in STEP_DEFINITIONS
        ]

        # 2. Draw connecting lines between consecutive nodes
        for idx in range(1, 5):
            cx1, cy1, r1 = self._node_geometry(idx)
            cx2, cy2, r2 = self._node_geometry(idx + 1)
            x_start = cx1 + r1
            x_end = cx2 - r2

            st1 = steps[idx - 1].status
            st2 = steps[idx].status

            pen = QPen()
            pen.setWidthF(3.2)

            if st1 == StepStatus.COMPLETED and st2 == StepStatus.COMPLETED:
                # Step 4 to 5 completed is gold line
                if idx == 4:
                    pen.setColor(COLOR_GOLD)
                else:
                    pen.setColor(COLOR_GREEN)
            elif st1 == StepStatus.COMPLETED and st2 in (StepStatus.ACTIVE, StepStatus.PENDING):
                # Half green, half gray line
                grad = QLinearGradient(x_start, cy1, x_end, cy1)
                grad.setColorAt(0.0, COLOR_GREEN)
                grad.setColorAt(0.5, COLOR_GREEN)
                grad.setColorAt(0.51, COLOR_GRAY)
                grad.setColorAt(1.0, COLOR_GRAY)
                pen.setBrush(QBrush(grad))
            else:
                pen.setColor(QColor(203, 213, 225))

            painter.setPen(pen)
            painter.drawLine(QPointF(x_start, cy1), QPointF(x_end, cy1))

        # 3. Draw circle nodes and labels
        font_num = QFont("Segoe UI", 10, QFont.Bold)
        font_label = QFont("Segoe UI", 8, QFont.Normal)

        chinese_nums = ["①", "②", "③", "④", "⑤"]

        for idx in range(1, 6):
            cx, cy, r = self._node_geometry(idx)
            step_info = steps[idx - 1]
            st = step_info.status
            is_hover = (self._hover_step == idx)

            circle_r = r + (2.0 if is_hover else 0.0)
            rect = QRectF(cx - circle_r, cy - circle_r, circle_r * 2, circle_r * 2)

            if st == StepStatus.COMPLETED:
                if idx == 5:
                    # Gold node for deep synthesis
                    brush = QBrush(COLOR_GOLD)
                    border = QPen(QColor(180, 83, 9), 2.0)
                else:
                    brush = QBrush(COLOR_GREEN)
                    border = QPen(QColor(27, 94, 32), 2.0)
                text_color = Qt.white
                label_color = COLOR_GOLD if idx == 5 else COLOR_GREEN
            elif st == StepStatus.ACTIVE:
                brush = QBrush(COLOR_GREEN_LIGHT)
                border = QPen(COLOR_BLUE, 2.5)
                text_color = COLOR_BLUE
                label_color = COLOR_BLUE
            elif st == StepStatus.FAILED:
                brush = QBrush(COLOR_RED)
                border = QPen(QColor(183, 28, 28), 2.0)
                text_color = Qt.white
                label_color = COLOR_RED
            else:  # PENDING
                if idx == 5 and glow == GlowState.GREEN:
                    # Step 5 is ready to trigger!
                    brush = QBrush(COLOR_GOLD_LIGHT)
                    border = QPen(COLOR_GOLD, 1.8, Qt.DashLine)
                    text_color = COLOR_GOLD
                    label_color = COLOR_GOLD
                else:
                    brush = QBrush(COLOR_GRAY_LIGHT)
                    border = QPen(COLOR_GRAY, 1.5)
                    text_color = COLOR_GRAY
                    label_color = QColor(100, 116, 139)

            painter.setBrush(brush)
            painter.setPen(border)
            painter.drawEllipse(rect)

            # Draw circle number
            painter.setFont(font_num)
            painter.setPen(text_color)
            num_char = str(idx)
            painter.drawText(rect, Qt.AlignCenter, num_char)

            # Draw label below node
            painter.setFont(font_label)
            painter.setPen(label_color)
            label_text = step_info.name
            if is_hover:
                label_text = f"▶ {label_text}"
            label_rect = QRectF(cx - 55, cy + circle_r + 4, 110, 24)
            painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, label_text)

        painter.end()


class MiniStepProgressWidget(QWidget):
    """Compact 5-dot inline indicator for library table row cells."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.status: DocumentPipelineStatus | None = None
        self.setFixedSize(126, 26)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击查看文献 5 步流水线与详细进展")

    def set_pipeline_status(self, status: DocumentPipelineStatus) -> None:
        self.status = status
        self._update_tooltip()
        self.update()

    def _update_tooltip(self) -> None:
        if not self.status:
            return
        glow = self.status.glow
        if glow == GlowState.GOLD:
            hint = "✨ 深度归纳就绪 (已生成 L1 Master 主卡片 · 冒金光)"
        elif glow == GlowState.GREEN:
            hint = f"🟢 基础入库就绪 (已入库 {self.status.card_count} 张卡片 · 冒绿光)"
        else:
            hint = f"⚪ 处理流水线中 (当前第 {self.status.current_step} 步)"
        self.setToolTip(f"{hint}\n点击打开详情工作台")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        cy = h / 2.0

        glow = self.status.glow if self.status else GlowState.NONE
        steps = self.status.steps if self.status else []

        # Pill background with subtle glow tint
        bg_rect = QRectF(1, 2, w - 2, h - 4)
        if glow == GlowState.GOLD:
            painter.fillPath(
                _rounded_path(bg_rect, 10),
                QBrush(QColor(254, 243, 199, 180)),
            )
            painter.strokePath(
                _rounded_path(bg_rect, 10),
                QPen(COLOR_GOLD, 1.0),
            )
        elif glow == GlowState.GREEN:
            painter.fillPath(
                _rounded_path(bg_rect, 10),
                QBrush(QColor(232, 245, 233, 180)),
            )
            painter.strokePath(
                _rounded_path(bg_rect, 10),
                QPen(COLOR_GREEN, 1.0),
            )

        margin_x = 16.0
        step_w = (w - margin_x * 2) / 4.0
        r = 4.2

        # Draw lines
        for i in range(4):
            x1 = margin_x + i * step_w
            x2 = margin_x + (i + 1) * step_w
            st1 = steps[i].status if i < len(steps) else StepStatus.PENDING
            st2 = steps[i + 1].status if i + 1 < len(steps) else StepStatus.PENDING

            pen = QPen()
            pen.setWidthF(1.8)
            if st1 == StepStatus.COMPLETED and st2 == StepStatus.COMPLETED:
                pen.setColor(COLOR_GOLD if i == 3 else COLOR_GREEN)
            else:
                pen.setColor(QColor(203, 213, 225))
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, cy), QPointF(x2, cy))

        # Draw 5 dots
        for i in range(5):
            cx = margin_x + i * step_w
            st = steps[i].status if i < len(steps) else StepStatus.PENDING

            if st == StepStatus.COMPLETED:
                color = COLOR_GOLD if i == 4 else COLOR_GREEN
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(color.darker(120), 0.8))
            elif st == StepStatus.ACTIVE:
                painter.setBrush(QBrush(COLOR_BLUE))
                painter.setPen(QPen(COLOR_BLUE, 1.2))
            elif st == StepStatus.FAILED:
                painter.setBrush(QBrush(COLOR_RED))
                painter.setPen(QPen(COLOR_RED, 1.0))
            else:
                if i == 4 and glow == GlowState.GREEN:
                    painter.setBrush(QBrush(COLOR_GOLD_LIGHT))
                    painter.setPen(QPen(COLOR_GOLD, 1.0, Qt.DashLine))
                else:
                    painter.setBrush(QBrush(QColor(226, 232, 240)))
                    painter.setPen(QPen(COLOR_GRAY, 0.8))

            painter.drawEllipse(QPointF(cx, cy), r, r)

        painter.end()


def _rounded_path(rect: QRectF, radius: float) -> QPainterPath:
    p = QPainterPath()
    p.addRoundedRect(rect, radius, radius)
    return p


class StepActionBar(QWidget):
    """Control panel with 5 explicit action buttons corresponding to the 5 pipeline steps."""

    action_triggered = Signal(int)  # 1..5

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)

        self.btn_step1 = QPushButton("① 从文字提取开始")
        self.btn_step1.setObjectName("Ghost")
        self.btn_step1.setToolTip("重新执行 PDF/CAJ 文本解析与 OCR，重置下游状态")
        self.btn_step1.clicked.connect(lambda: self.action_triggered.emit(1))

        self.btn_step2 = QPushButton("② 从信息提取开始")
        self.btn_step2.setObjectName("Ghost")
        self.btn_step2.setToolTip("重新计算四通道雷达与证据包 (ContextBundle)")
        self.btn_step2.clicked.connect(lambda: self.action_triggered.emit(2))

        self.btn_step3 = QPushButton("③ 从AI快速提取开始")
        self.btn_step3.setObjectName("Ghost")
        self.btn_step3.setToolTip("重新请求大模型生成初版 L2 细节知识卡 (API / 网页版)")
        self.btn_step3.clicked.connect(lambda: self.action_triggered.emit(3))

        self.btn_step4 = QPushButton("④ 提交写入数据库")
        self.btn_step4.setObjectName("Primary")
        self.btn_step4.setToolTip("校验并正式入库已提炼卡片，达成及格线·冒绿光")
        self.btn_step4.clicked.connect(lambda: self.action_triggered.emit(4))

        self.btn_step5 = QPushButton("✨ ⑤ 启动AI深度归纳")
        self.btn_step5.setObjectName("Warning")
        self.btn_step5.setToolTip("跨卡深度归纳生成顶层 L1 Master 主卡片 (网页版 / Codex / Antigravity)·冒金光")
        self.btn_step5.clicked.connect(lambda: self.action_triggered.emit(5))

        for btn in (
            self.btn_step1,
            self.btn_step2,
            self.btn_step3,
            self.btn_step4,
            self.btn_step5,
        ):
            btn.setCursor(Qt.PointingHandCursor)
            layout.addWidget(btn)

        layout.addStretch(1)

    def highlight_next_step(self, current_step: int, glow: GlowState) -> None:
        """Dynamically style the recommended next action button."""
        buttons = [
            self.btn_step1,
            self.btn_step2,
            self.btn_step3,
            self.btn_step4,
            self.btn_step5,
        ]
        for idx, btn in enumerate(buttons, 1):
            if idx == current_step:
                if idx == 5:
                    btn.setObjectName("Warning")
                else:
                    btn.setObjectName("Primary")
            else:
                btn.setObjectName("Ghost")
            btn.style().unpolish(btn)
            btn.style().polish(btn)


class StepProgressDelegate(QStyledItemDelegate):
    """Ultra-fast, hardware-accelerated QStyledItemDelegate for rendering 5-step pipeline progress in QTableWidget."""

    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect
        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, option.palette.highlight())

        data = index.data(Qt.UserRole)
        if not isinstance(data, dict):
            painter.restore()
            return

        glow = str(data.get("glow", "NONE")).upper()
        steps = data.get("steps", [])

        w = float(rect.width())
        h = float(rect.height())
        cy = rect.top() + h / 2.0

        # Background pill with glow tint
        pill_rect = QRectF(rect.left() + 4, rect.top() + 4, w - 8, h - 8)
        if glow == "GOLD":
            painter.fillPath(_rounded_path(pill_rect, 9), QBrush(QColor(254, 243, 199, 190)))
            painter.strokePath(_rounded_path(pill_rect, 9), QPen(COLOR_GOLD, 1.0))
        elif glow == "GREEN":
            painter.fillPath(_rounded_path(pill_rect, 9), QBrush(QColor(232, 245, 233, 190)))
            painter.strokePath(_rounded_path(pill_rect, 9), QPen(COLOR_GREEN, 1.0))

        margin_x = 18.0
        step_w = (w - margin_x * 2) / 4.0
        r = 4.0

        # Draw 4 connecting lines
        for i in range(4):
            x1 = rect.left() + margin_x + i * step_w
            x2 = rect.left() + margin_x + (i + 1) * step_w
            st1 = steps[i].get("status") if i < len(steps) else "PENDING"
            st2 = steps[i + 1].get("status") if i + 1 < len(steps) else "PENDING"

            pen = QPen()
            pen.setWidthF(1.8)
            if st1 == "COMPLETED" and st2 == "COMPLETED":
                pen.setColor(COLOR_GOLD if i == 3 else COLOR_GREEN)
            else:
                pen.setColor(QColor(203, 213, 225))
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, cy), QPointF(x2, cy))

        # Draw 5 dots
        for i in range(5):
            cx = rect.left() + margin_x + i * step_w
            st = steps[i].get("status") if i < len(steps) else "PENDING"

            if st == "COMPLETED":
                color = COLOR_GOLD if i == 4 else COLOR_GREEN
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(color.darker(120), 0.8))
            elif st == "ACTIVE":
                painter.setBrush(QBrush(COLOR_BLUE))
                painter.setPen(QPen(COLOR_BLUE, 1.2))
            elif st == "FAILED":
                painter.setBrush(QBrush(COLOR_RED))
                painter.setPen(QPen(COLOR_RED, 1.0))
            else:
                if i == 4 and glow == "GREEN":
                    painter.setBrush(QBrush(COLOR_GOLD_LIGHT))
                    painter.setPen(QPen(COLOR_GOLD, 1.0, Qt.DashLine))
                else:
                    painter.setBrush(QBrush(QColor(226, 232, 240)))
                    painter.setPen(QPen(COLOR_GRAY, 0.8))

            painter.drawEllipse(QPointF(cx, cy), r, r)

        painter.restore()

    def sizeHint(self, option: Any, index: Any) -> QSize:
        return QSize(140, 36)
