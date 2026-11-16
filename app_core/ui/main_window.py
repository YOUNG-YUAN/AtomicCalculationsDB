"""V2 Material 3 frameless window and view wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from db.id_generator import classify_id
from db.sqlite_manager import SQLiteManager
from models.gateway import ModelGateway
from parser.card_quality import sanitize_card_fields
from ui.dialogs import (
    DocumentDetailDialog,
    KeywordDetailDialog,
    KnowledgeCardEditDialog,
    KnowledgeDetailDialog,
)
from ui.icon_assets import app_icon, logo_pixmap
from ui.m3_theme import load_stylesheet
from ui.views.explore import ExploreView
from ui.views.library import LibraryView
from ui.views.night import NightView
from ui.views.settings import SettingsView
from ui.views.sync_mcp import SyncMCPView
from ui.widgets import CardDrawer, NavRail, Toast, repolish
from ui.workers import CardRevisionWorker
from updater.patch_manager import PatchManager, UpdateError
from updater.updater import read_version


class WindowControlButton(QPushButton):
    """Chrome-style caption button drawn with a crisp geometric icon."""

    _TOOLTIPS = {
        "minimize": "最小化",
        "maximize": "最大化",
        "restore": "向下还原",
        "close": "关闭",
    }

    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.setText("")
        self.setFixedSize(46, 36)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("CloseButton" if kind == "close" else "WindowButton")
        self.setToolTip(self._TOOLTIPS.get(kind, ""))
        self.setAccessibleName(self._TOOLTIPS.get(kind, ""))

    def set_kind(self, kind: str) -> None:
        if kind == self.kind:
            return
        self.kind = kind
        self.setToolTip(self._TOOLTIPS.get(kind, ""))
        self.setAccessibleName(self._TOOLTIPS.get(kind, ""))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        hover = self.underMouse()
        color = QColor("#FFFFFF") if self.kind == "close" and hover else QColor("#5F6368")
        pen = QPen(color)
        pen.setWidthF(1.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        if self.kind == "minimize":
            painter.drawLine(QPoint(round(cx - 5), round(cy)), QPoint(round(cx + 5), round(cy)))
        elif self.kind == "maximize":
            painter.drawRect(QRectF(cx - 5, cy - 5, 10, 10))
        elif self.kind == "restore":
            painter.drawRect(QRectF(cx - 5, cy - 3, 8, 8))
            painter.drawLine(QPoint(round(cx - 2), round(cy - 5)), QPoint(round(cx + 5), round(cy - 5)))
            painter.drawLine(QPoint(round(cx + 5), round(cy - 5)), QPoint(round(cx + 5), round(cy + 2)))
        else:
            painter.drawLine(QPoint(round(cx - 5), round(cy - 5)), QPoint(round(cx + 5), round(cy + 5)))
            painter.drawLine(QPoint(round(cx - 5), round(cy + 5)), QPoint(round(cx + 5), round(cy - 5)))
        painter.end()


class TitleBar(QWidget):
    search_changed = Signal(str)
    search_submitted = Signal(str)
    update_requested = Signal()
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, version: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(66)
        self._drag_offset: QPoint | None = None
        self._press_global = QPoint()
        self._press_local = QPoint()
        self._drag_started = False
        self._started_maximized = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 12, 6)
        layout.setSpacing(10)
        logo = QLabel()
        logo.setObjectName("LogoBadge")
        logo.setFixedSize(30, 30)
        logo.setScaledContents(True)
        logo.setPixmap(logo_pixmap(64))
        logo.setAlignment(Qt.AlignCenter)
        logo.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(logo)
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel("AtomicCalculationDB")
        title.setObjectName("AppTitle")
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        subtitle = QLabel(f"Atomic Data Knowledge Base · v{version}")
        subtitle.setObjectName("AppSubtitle")
        subtitle.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)
        layout.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("SearchBox")
        self.search.setPlaceholderText("🔍 搜索离子 / 方法 / 过程，或输入 KNW-、DOC-、MASTER- ID")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(480)
        self.search.textChanged.connect(self.search_changed)
        self.search.returnPressed.connect(
            lambda: self.search_submitted.emit(self.search.text())
        )
        layout.addWidget(self.search)
        self.update_button = QPushButton(f"📦 v{version} · 导入更新包")
        self.update_button.setObjectName("UpdateCapsule")
        self.update_button.setToolTip("导入 .zip 更新包并在运行中自动重启升级")
        self.update_button.setCursor(Qt.PointingHandCursor)
        self.update_button.clicked.connect(self.update_requested)
        layout.addWidget(self.update_button)
        self.minimize_button = WindowControlButton("minimize")
        self.minimize_button.clicked.connect(self.minimize_requested)
        self.maximize_button = WindowControlButton("maximize")
        self.maximize_button.clicked.connect(self.maximize_requested)
        self.close_button = WindowControlButton("close")
        self.close_button.clicked.connect(self.close_requested)
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            layout.addWidget(button)

    def set_maximized_state(self, maximized: bool) -> None:
        self.maximize_button.set_kind("restore" if maximized else "maximize")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_local = event.position().toPoint()
            self._drag_offset = (
                self._press_global - self.window().frameGeometry().topLeft()
            )
            self._started_maximized = self.window().isMaximized()
            self._drag_started = False
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is None or not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        global_pos = event.globalPosition().toPoint()
        if self._started_maximized and not self._drag_started:
            if (global_pos - self._press_global).manhattanLength() <= 4:
                event.accept()
                return
            self._drag_started = True
            window = self.window()
            ratio_x = self._press_local.x() / max(1, self.width())
            normal_geometry = getattr(window, "_normal_geometry", None)
            if normal_geometry is None or not normal_geometry.isValid():
                normal_geometry = window.normalGeometry()
            window.showNormal()
            if normal_geometry.isValid() and normal_geometry.width() > 0:
                window.setGeometry(normal_geometry)
            self.set_maximized_state(False)
            restored_width = (
                normal_geometry.width()
                if normal_geometry.isValid() and normal_geometry.width() > 0
                else window.width()
            )
            new_x = int(global_pos.x() - restored_width * ratio_x)
            new_y = int(global_pos.y() - self._press_local.y())
            window.move(new_x, new_y)
            self._drag_offset = global_pos - window.pos()
            event.accept()
            return
        self.window().move(global_pos - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        self._drag_started = False
        self._started_maximized = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.maximize_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    external_data_changed = Signal()

    def __init__(
        self,
        base_dir: str | Path,
        config: Any,
        db: SQLiteManager,
        *,
        gateway: ModelGateway,
        retriever: Any,
        worker: Any,
        fetcher: Any,
        packager: Any,
        sync_manager: Any,
        mcp_server: Any,
        pipeline: Any,
        watcher: Any = None,
        qdrant: Any = None,
        runtime_bridge: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.base_dir = Path(base_dir)
        self.workspace_dir = self.base_dir / "workspace"
        self.config = config
        self.db = db
        self.gateway = gateway
        self.retriever = retriever
        self.worker = worker
        self.fetcher = fetcher
        self.packager = packager
        self.sync_manager = sync_manager
        self.mcp_server = mcp_server
        self.pipeline = pipeline
        self.watcher = watcher
        self.qdrant = qdrant
        self.runtime_bridge = runtime_bridge
        self.patch = PatchManager(self.base_dir)
        self._relaunching = False
        self._card_workers: list[CardRevisionWorker] = []
        self._normal_geometry = None
        version = read_version(self.base_dir / "app_core").get("version", "2.3.10")

        self.setWindowTitle("AtomicCalculationDB")
        self.setWindowIcon(app_icon())
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(1360, 860)
        self.setMinimumSize(1120, 720)

        self._root = QWidget()
        self._root.setObjectName("WindowRoot")
        self.setCentralWidget(self._root)
        root_layout = QVBoxLayout(self._root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = TitleBar(version)
        root_layout.addWidget(self.title_bar)
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(12, 0, 12, 12)
        body_layout.setSpacing(12)
        root_layout.addWidget(body, 1)

        self.nav = NavRail(version)
        body_layout.addWidget(self.nav)
        self.stack = QStackedWidget()
        self.stack.setObjectName("PageStack")
        body_layout.addWidget(self.stack, 1)

        self.explore_view = ExploreView(db, retriever, str(self.workspace_dir), config)
        self.night_view = NightView(
            db, worker, str(self.workspace_dir), config, pipeline=pipeline
        )
        self.library_view = LibraryView(
            db, fetcher, str(self.workspace_dir), config,
            watcher=watcher, pipeline=pipeline,
        )
        self.sync_view = SyncMCPView(
            db, packager, sync_manager, mcp_server, qdrant,
            str(self.base_dir), config,
        )
        self.settings_view = SettingsView(
            db, config, gateway, str(self.base_dir), str(self.workspace_dir)
        )
        for view in (
            self.explore_view,
            self.night_view,
            self.library_view,
            self.sync_view,
            self.settings_view,
        ):
            self.stack.addWidget(view)

        self.drawer = CardDrawer(self._root)
        self.drawer.open_document.connect(self.open_document)
        self.drawer.edit_requested.connect(self.edit_card)
        self.drawer.ai_requested.connect(self.ai_edit_card)
        self.drawer.delete_requested.connect(self.delete_card)
        self.drawer.restore_requested.connect(self.restore_card)
        self.toast = Toast(self._root)
        self._grip = QSizeGrip(self._root)
        self._grip.setFixedSize(18, 18)
        self._grip.setStyleSheet("background: transparent;")
        self._drawer_anim: QPropertyAnimation | None = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(260)
        self._search_timer.timeout.connect(self._commit_search)

        self._wire()
        self._install_shortcuts()
        self._install_tray()
        self._update_window_state()
        self.nav.set_current(0)
        self._refresh_all()

    # ------------------------------------------------------------------ wiring
    def _wire(self) -> None:
        self.title_bar.search_changed.connect(self._on_search_text)
        self.title_bar.search_submitted.connect(self._on_search_submit)
        self.title_bar.update_requested.connect(self._import_update)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self.toggle_maximize)
        self.title_bar.close_requested.connect(self.close)
        self.nav.nav_changed.connect(self.navigate)
        self.explore_view.open_card.connect(self.open_card)
        self.explore_view.open_document.connect(self.open_document)
        self.explore_view.open_keyword.connect(self.open_keyword)
        self.explore_view.open_hit.connect(self.open_hit)
        self.explore_view.seed_requested.connect(self._seed_demo)
        self.explore_view.toast.connect(self.toast.show_message)
        self.explore_view.query_changed.connect(self._set_search_text)
        self.library_view.open_document.connect(self.open_document)
        self.library_view.toast.connect(self.toast.show_message)
        self.library_view.data_changed.connect(self._refresh_all)
        self.night_view.open_card.connect(self.open_card)
        self.night_view.toast.connect(self.toast.show_message)
        self.sync_view.toast.connect(self.toast.show_message)
        self.sync_view.data_changed.connect(self._refresh_all)
        self.settings_view.toast.connect(self.toast.show_message)
        self.settings_view.data_changed.connect(self._refresh_all)
        self.settings_view.models_changed.connect(self._rebuild_models)
        self.settings_view.update_requested.connect(self._import_update)
        self.external_data_changed.connect(self._refresh_all)

    def _install_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip("AtomicCalculationDB · 原子物理知识库")
        menu = QMenu(self)
        show_action = QAction("打开主窗口", self)
        show_action.triggered.connect(self._restore_window)
        update_action = QAction("导入更新包…", self)
        update_action.triggered.connect(self._import_update)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(show_action)
        menu.addAction(update_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._restore_window()
            if reason == QSystemTrayIcon.DoubleClick
            else None
        )
        tray.show()
        self.tray = tray

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # -------------------------------------------------------------- hot update
    def _import_update(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择更新包", str(self.base_dir), "更新包 (*.zip)"
        )
        if not path:
            return
        try:
            info = self.patch.validate(path)
            self.patch.stage(path)
        except UpdateError as exc:
            QMessageBox.critical(self, "更新包无效", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "更新包已校验就绪",
            f"更新包 v{info.version}（build {info.build}，{info.entries} 个文件）已暂存。\n\n"
            "更新包已校验就绪，正在自动重启并应用更新；若启动异常将自动回滚。\n现在重启吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.perform_hot_restart()
        else:
            self.toast.show_message("更新包已暂存，下次启动时自动应用")

    def perform_hot_restart(self) -> None:
        """Release SQLite/Qdrant locks, then relaunch the shell process."""
        if self._relaunching:
            return
        self._relaunching = True
        self.toast.show_message("正在安全关闭数据库并重启…")
        QApplication.processEvents()
        try:
            self.worker.stop(graceful=False)
        except Exception:
            pass
        try:
            self.sync_view.shutdown()
        except Exception:
            pass
        try:
            if self.runtime_bridge is not None:
                self.runtime_bridge.stop()
        except Exception:
            pass
        try:
            if self.qdrant is not None:
                self.qdrant.close()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self.patch.relaunch()

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence.Find, self).activated.connect(self._focus_search)
        QShortcut(QKeySequence("Esc"), self).activated.connect(self._handle_escape)

    def _focus_search(self) -> None:
        self.title_bar.search.setFocus()
        self.title_bar.search.selectAll()

    def _handle_escape(self) -> None:
        if self.drawer.isVisible():
            self.close_drawer()
        else:
            self.title_bar.search.clear()

    # ------------------------------------------------------------------- pages
    def navigate(self, index: int) -> None:
        if not 0 <= index <= 4:
            return
        self.stack.setCurrentIndex(index)
        self.nav.set_current(index)
        page = self.stack.widget(index)
        if hasattr(page, "refresh"):
            page.refresh()

    def _refresh_all(self) -> None:
        for view in (
            self.explore_view,
            self.night_view,
            self.library_view,
            self.sync_view,
            self.settings_view,
        ):
            if hasattr(view, "refresh"):
                view.refresh()

    # ------------------------------------------------------------------ search
    def _on_search_text(self, text: str) -> None:
        self._search_timer.start()

    def _on_search_submit(self, text: str) -> None:
        self._search_timer.stop()
        self._commit_search()

    def _commit_search(self) -> None:
        text = self.title_bar.search.text().strip()
        if text and classify_id(text):
            hit = self.retriever.lookup(text)
            if hit is not None:
                self.open_hit(hit)
                return
        self.navigate(0)
        self.explore_view.submit_query(text)

    def _set_search_text(self, text: str) -> None:
        self.title_bar.search.blockSignals(True)
        self.title_bar.search.setText(text)
        self.title_bar.search.blockSignals(False)

    # ----------------------------------------------------------------- drawer
    def open_card(self, knw_id: str) -> None:
        card = self.db.get_knowledge_card(knw_id)
        if not card:
            self.toast.show_message(f"找不到知识卡：{knw_id}")
            return
        self.drawer.show_card(card)
        self._show_drawer()

    def _show_drawer(self) -> None:
        self.drawer.setFixedHeight(max(360, self._root.height() - 84))
        target_x = self._root.width() - self.drawer.width() - 10
        self.drawer.move(self._root.width(), 72)
        self.drawer.show()
        self.drawer.raise_()
        self._drawer_anim = QPropertyAnimation(self.drawer, b"pos", self)
        self._drawer_anim.setDuration(220)
        self._drawer_anim.setStartValue(QPoint(self._root.width(), 72))
        self._drawer_anim.setEndValue(QPoint(target_x, 72))
        self._drawer_anim.start()

    def close_drawer(self) -> None:
        self.drawer.setVisible(False)
        self.drawer.closed.emit()

    # --------------------------------------------------------------- card edit
    def edit_card(self, knw_id: str) -> None:
        card = self.db.get_knowledge_card(knw_id)
        if not card:
            self.toast.show_message(f"找不到知识卡：{knw_id}")
            return
        dialog = KnowledgeCardEditDialog(card, self)
        if not dialog.exec():
            return
        values = dialog.values()
        values["knw_id"] = knw_id
        values["keywords"] = [
            str(item.get("term_en"))
            for item in card.get("keywords") or []
            if item.get("term_en")
        ]
        values["is_vectorized"] = 0
        values["is_user_verified"] = 1
        values, _notes = sanitize_card_fields(values)
        try:
            self.db.upsert_knowledge_card(values)
            self.pipeline.reindex_card(knw_id)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.open_card(knw_id)
        self._refresh_all()
        self.toast.show_message("知识卡已保存，检索索引已更新")

    def ai_edit_card(self, knw_id: str, instruction: str) -> None:
        if self.gateway is None or not getattr(
            getattr(self.gateway, "fast_client", None), "available", False
        ):
            self.drawer.set_ai_busy(False)
            self.toast.show_message("快速提取模型未配置，无法执行 AI 修改")
            return
        if any(worker.isRunning() for worker in self._card_workers):
            self.drawer.set_ai_busy(False)
            self.toast.show_message("上一项 AI 修改仍在进行中")
            return
        worker = CardRevisionWorker(self.pipeline, knw_id, instruction)
        worker.finished_card.connect(self._card_revised)
        worker.failed.connect(self._card_revision_failed)
        worker.finished.connect(lambda: self._cleanup_card_worker(worker))
        self._card_workers.append(worker)
        self.drawer.set_ai_busy(True)
        self.toast.show_message("正在根据证据和修改意见重新提炼…")
        worker.start()

    def _card_revised(self, card: object) -> None:
        self.drawer.set_ai_busy(False)
        if isinstance(card, dict) and self.drawer.isVisible():
            if self.drawer.card.get("knw_id") == card.get("knw_id"):
                self.drawer.show_card(card)
        self._refresh_all()
        self.toast.show_message("AI 已按修改意见更新知识卡")

    def _card_revision_failed(self, message: str) -> None:
        self.drawer.set_ai_busy(False)
        self.toast.show_message(f"AI 修改失败：{message}")

    def _cleanup_card_worker(self, worker: CardRevisionWorker) -> None:
        if worker in self._card_workers:
            self._card_workers.remove(worker)
        worker.deleteLater()

    def delete_card(self, knw_id: str) -> None:
        card = self.db.get_knowledge_card(knw_id)
        if not card:
            return
        answer = QMessageBox.question(
            self,
            "删除知识卡",
            f"确定删除 {knw_id} 吗？\n\n"
            f"{(card.get('title_zh') or '')[:120]}\n\n"
            "该操作会同时删除 SQLite 记录、关联证据与右侧检索向量，且不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.db.delete_knowledge_card(knw_id)
            if self.qdrant is not None:
                try:
                    self.qdrant.delete(knw_id)
                except Exception:
                    pass
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.close_drawer()
        self._refresh_all()
        self.toast.show_message(f"已删除知识卡 {knw_id}")

    def restore_card(self, knw_id: str) -> None:
        if not self.db.restore_card_from_audit_suggestion(knw_id):
            self.toast.show_message("这张卡没有待恢复的审计删除建议")
            return
        card = self.db.get_knowledge_card(knw_id)
        if card and self.drawer.isVisible():
            self.drawer.show_card(card)
        self._refresh_all()
        self.toast.show_message(f"已恢复知识卡 {knw_id}")

    # ------------------------------------------------------------------ dialogs
    def open_hit(self, hit) -> None:
        if getattr(hit, "kind", "") in ("card", "master"):
            self.open_card(hit.id)
        elif getattr(hit, "kind", "") == "document":
            self.open_document(hit.id)
        elif getattr(hit, "kind", "") == "keyword":
            self.open_keyword(hit.id)
        elif getattr(hit, "kind", "") == "evidence":
            if hit.card:
                self.open_card(hit.card["knw_id"])
            else:
                self.toast.show_message("证据块缺少父卡片")

    def open_document(self, doc_id: str) -> None:
        DocumentDetailDialog(
            self.db, self.workspace_dir, doc_id,
            parent=self, pipeline=self.pipeline,
        ).exec()

    def open_keyword(self, kw_id: str) -> None:
        KeywordDetailDialog(self.db, self.workspace_dir, kw_id, parent=self).exec()

    # ------------------------------------------------------------------- models
    def _rebuild_models(self) -> None:
        try:
            gateway = ModelGateway(self.config, self.workspace_dir)
            retriever = self.retriever.__class__(
                self.db,
                qdrant=self.qdrant,
                embedder=gateway.embedding,
                reranker=gateway.reranker,
            )
        except Exception as exc:
            self.toast.show_message(f"模型网关重建失败：{exc}")
            return
        self.gateway = gateway
        self.retriever = retriever
        self.explore_view.retriever = retriever
        self.explore_view.refresh()
        self.pipeline.gateway = gateway
        self.pipeline.consensus.registry = gateway.registry
        self.mcp_server.retriever = retriever
        if self.runtime_bridge is not None:
            self.runtime_bridge.retriever = retriever
        if self.sync_manager is not None:
            self.sync_manager.vectorizer = lambda card: (
                gateway.embedding.embed_query(
                    str(card.get("search_payload") or card.get("summary_zh") or "")
                )
                if getattr(gateway.embedding, "available", False)
                and str(card.get("search_payload") or card.get("summary_zh") or "")
                else None
            )
        self.settings_view.gateway = gateway
        if hasattr(self.settings_view, "endpoint_pool"):
            self.settings_view.endpoint_pool = gateway.pool
        if hasattr(self.settings_view, "model_pool_panel"):
            self.settings_view.model_pool_panel.pool = gateway.pool
            self.settings_view.model_pool_panel.gateway = gateway
        self.settings_view.refresh()
        self.toast.show_message("模型网关已重建")

    def _seed_demo(self) -> None:
        from db.demo_v2 import seed_demo_v2

        counts = seed_demo_v2(self.db)
        self._refresh_all()
        self.toast.show_message(
            f"已载入示例：{counts['documents']} 篇文献、{counts['cards']} 张卡片"
        )

    # --------------------------------------------------------------- window UX
    def toggle_maximize(self) -> None:
        target = not self.isMaximized()
        if target:
            self._normal_geometry = self.geometry()
            self.showMaximized()
        else:
            self.showNormal()
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
        self._update_window_state(target)
        QTimer.singleShot(0, self._update_window_state)

    def _update_window_state(self, maximized: bool | None = None) -> None:
        if not hasattr(self, "_root"):
            return
        if maximized is None:
            maximized = self.isMaximized()
        self._root.setProperty("acdbMaximized", "true" if maximized else "false")
        repolish(self._root)
        self.title_bar.set_maximized_state(maximized)
        self._grip.setVisible(not maximized)
        QTimer.singleShot(0, self._reposition_overlays)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._update_window_state)

    def _reposition_overlays(self) -> None:
        if hasattr(self, "_grip"):
            self._grip.move(self._root.width() - 22, self._root.height() - 22)
            self._grip.raise_()
        if hasattr(self, "toast"):
            self.toast.reposition()
            self.toast.raise_()
        if hasattr(self, "drawer") and self.drawer.isVisible():
            self.drawer.setFixedHeight(max(360, self._root.height() - 84))
            self.drawer.move(self._root.width() - self.drawer.width() - 10, 72)
            self.drawer.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self.isMaximized() and self.isVisible():
            self._normal_geometry = self.geometry()
        QTimer.singleShot(0, self._reposition_overlays)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._reposition_overlays)

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.sync_view.shutdown()
        except Exception:
            pass
        try:
            if self.worker.status().state in ("RUNNING", "SLOW_BRAIN"):
                self.worker.stop(graceful=False)
        except Exception:
            pass
        super().closeEvent(event)


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(load_stylesheet())
