import json
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QPushButton

from config import AppConfig
from ui.controls import WheelSafeComboBox, WheelSafeSpinBox
from ui.dialogs import DocumentDetailDialog, KnowledgeCardEditDialog
from ui.widgets import KnowledgeCard
from db.demo_v2 import seed_demo_v2
from main_entry import _init_services
from ui.main_window import MainWindow, apply_theme


def test_v2_main_window_smoke(qapp, tmp_path: Path):
    base = tmp_path / "release"
    (base / "app_core").mkdir(parents=True)
    (base / "app_core" / "version.json").write_text(
        json.dumps({"version": "2.0.0", "build": "test", "schema_ver": 2}),
        encoding="utf-8",
    )
    (base / "workspace").mkdir()
    config = AppConfig.load(base / "workspace" / "config.yaml")
    services = _init_services(base, config)
    seed_demo_v2(services["db"])
    apply_theme(qapp)
    window = MainWindow(
        base,
        config,
        services["db"],
        gateway=services["gateway"],
        retriever=services["retriever"],
        worker=services["worker"],
        fetcher=services["fetcher"],
        packager=services["packager"],
        sync_manager=services["sync_manager"],
        mcp_server=services["mcp_server"],
        pipeline=services["pipeline"],
        watcher=services["watcher"],
        qdrant=services["qdrant"],
    )
    window.show()
    for _ in range(30):
        qapp.processEvents()
        time.sleep(0.03)

    assert window.stack.count() == 5
    assert window.explore_view.card_grid.count() >= 5
    cards = [
        item for item in window.explore_view._card_widgets
        if isinstance(item, KnowledgeCard)
    ]
    assert cards and all(card.height() == 286 for card in cards)
    assert window.title_bar.maximize_button.text() == ""
    assert window.title_bar.maximize_button.kind == "maximize"
    window._update_window_state(True)
    assert window.title_bar.maximize_button.kind == "restore"
    window._update_window_state(False)
    assert window.title_bar.maximize_button.kind == "maximize"
    settings = window.settings_view
    assert hasattr(settings, "model_pool_panel")
    assert hasattr(settings, "mineru_card")
    assert hasattr(settings, "mineru_command")
    assert isinstance(settings.mineru_tier, WheelSafeComboBox)
    assert isinstance(settings.mineru_ocr, WheelSafeComboBox)
    assert isinstance(settings.mineru_device, WheelSafeComboBox)
    assert isinstance(settings.mineru_timeout, WheelSafeSpinBox)
    assert settings.mineru_tier.maximumWidth() == 220
    assert settings.mineru_ocr.maximumWidth() == 220
    assert settings.mineru_device.maximumWidth() == 180
    assert settings.mineru_timeout.maximumWidth() == 140
    assert not hasattr(settings, "model_fields")
    assert not hasattr(settings, "embed_provider")
    assert not hasattr(settings, "rerank_provider")
    for index in range(5):
        window.navigate(index)
        qapp.processEvents()
    assert window.night_view.status_pill.text() in ("空闲", "运行中", "深度归纳", "已停止")
    library = window.library_view
    assert library.table.rowCount() >= 4
    assert "全库本地修复与清洗" in library.local_clean_button.text()
    assert "AI 快速理解" in library.incremental_button.text()
    library._reprocess_eta.reset()
    library._reprocess_progress("阶段 4/5 · MiniLM 语义编码 32/3648", 0.5)
    assert library.reprocess_progress.value() == 50
    assert "预计剩余" in library.reprocess_eta.text()
    first_doc_id = str(library.table.item(0, 0).data(Qt.UserRole))
    detail = DocumentDetailDialog(
        services["db"], base / "workspace", first_doc_id,
        pipeline=services["pipeline"],
    )
    assert detail.reprocess_manager is not None
    assert "从AI快速提取开始" in detail.incremental_button.text()
    detail.close()
    services["db"].add_token_usage(first_doc_id, 42, 7)
    library.refresh()
    assert "总输入: 42" in library.token_label.text()
    library.table.item(0, 4).setText("Edited Journal")
    for _ in range(10):
        qapp.processEvents()
    assert services["db"].get_document(first_doc_id)["journal"] == "Edited Journal"

    window.navigate(0)
    window.explore_view.submit_query("Fe XVII MCDHF")
    for _ in range(50):
        qapp.processEvents()
        time.sleep(0.03)
    assert window.explore_view.card_grid.count() >= 1

    card_id = services["db"].list_knowledge_cards(limit=1)[0]["knw_id"]
    window.open_card(card_id)
    qapp.processEvents()
    assert window.drawer.isVisible()
    button_texts = {
        button.text()
        for button in window.drawer.findChildren(QPushButton)
    }
    assert "✏️ 手动编辑" in button_texts
    assert "✨ AI 修改" in button_texts
    assert "🗑️ 删除卡片" in button_texts
    window.drawer._toggle_ai_panel()
    qapp.processEvents()
    assert window.drawer.ai_panel is not None and window.drawer.ai_panel.isVisible()
    window.drawer.set_ai_busy(True)
    assert not window.drawer.ai_send_button.isEnabled()
    window.drawer.set_ai_busy(False)
    dialog = KnowledgeCardEditDialog(services["db"].get_knowledge_card(card_id))
    dialog.title_edit.setText("编辑后的卡片标题")
    assert dialog.values()["title_zh"] == "编辑后的卡片标题"
    dialog.close()
    window.close_drawer()

    # Maximize icon state and drag-to-restore.
    window.toggle_maximize()
    for _ in range(10):
        qapp.processEvents()
    assert window.isMaximized()
    assert window.title_bar.maximize_button.kind == "restore"
    local = QPointF(200, 30)
    global_pos = QPointF(window.title_bar.mapToGlobal(QPoint(200, 30)))
    press = QMouseEvent(
        QEvent.MouseButtonPress, local, global_pos,
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
    )
    window.title_bar.mousePressEvent(press)
    move = QMouseEvent(
        QEvent.MouseMove, QPointF(212, 34), QPointF(global_pos.x() + 12, global_pos.y() + 4),
        Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
    )
    window.title_bar.mouseMoveEvent(move)
    for _ in range(5):
        qapp.processEvents()
    assert not window.isMaximized()
    assert window.title_bar.maximize_button.kind == "maximize"
    release = QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(212, 34),
        QPointF(global_pos.x() + 12, global_pos.y() + 4),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
    )
    window.title_bar.mouseReleaseEvent(release)
    window.showNormal()

    window.close()
    qapp.processEvents()
    services["qdrant"].close()
    services["db"].close()
