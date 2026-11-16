import time
from pathlib import Path

from config import AppConfig
from db.hybrid_retriever import HybridRetriever
from db.sqlite_manager import SQLiteManager
from ui.views.explore import ExploreView


def _pagination_corpus(tmp_path: Path, count: int = 12):
    db = SQLiteManager(tmp_path / "workspace", embedding_signature="test::dim=4")
    doc = db.upsert_document({"title": "Pagination corpus"})
    ids: list[str] = []
    for index in range(count):
        ids.append(
            db.upsert_knowledge_card(
                {
                    "card_level": "L2",
                    "card_type": "STRAT",
                    "title_zh": f"分页卡片 {index:02d}",
                    "summary_zh": "用于验证探索中心分页的测试卡片。",
                    "premises_and_conditions": "仅用于测试。",
                    "supporting_doc_ids": [doc],
                    "search_payload": "pagination test card active space",
                    "authority_score": 1.0,
                    "recency_year": 2024,
                }
            )
        )
    return db, ids


def test_search_page_browses_all_cards_with_exact_total(tmp_path: Path):
    db, ids = _pagination_corpus(tmp_path)
    retriever = HybridRetriever(db)
    page1, total = retriever.search_page("", page=1, page_size=5)
    page2, total2 = retriever.search_page("", page=2, page_size=5)
    page3, total3 = retriever.search_page("", page=3, page_size=5)
    assert total == total2 == total3 == 12
    assert len(page1) == len(page2) == 5 and len(page3) == 2
    seen = {hit.id for hit in page1 + page2 + page3}
    assert seen == set(ids)
    db.close()


def test_search_page_query_is_stable_and_cached(tmp_path: Path):
    db, ids = _pagination_corpus(tmp_path)
    retriever = HybridRetriever(db)
    first, total = retriever.search_page("pagination", page=1, page_size=5)
    third, total_again = retriever.search_page("pagination", page=3, page_size=5)
    assert total == total_again == 12
    assert len(first) == 5 and len(third) == 2
    assert not ({hit.id for hit in first} & {hit.id for hit in third})
    assert {hit.id for hit in first + third} <= set(ids)
    cached = retriever._page_cache
    assert len(cached) == 1
    retriever.clear_search_cache()
    assert not retriever._page_cache
    db.close()


def test_page_window_keeps_nine_pages_around_current():
    assert ExploreView._page_window(12, 195) == list(range(8, 17))
    assert ExploreView._page_window(1, 195) == list(range(1, 10))
    assert ExploreView._page_window(195, 195) == list(range(187, 196))
    assert ExploreView._page_window(2, 5) == [1, 2, 3, 4, 5]


def _wait_until(qapp, predicate, attempts: int = 80) -> bool:
    for _ in range(attempts):
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.03)
    return predicate()


def test_explore_page_controls_navigate(qapp, tmp_path: Path):
    db, _ids = _pagination_corpus(tmp_path, 25)
    retriever = HybridRetriever(db)
    config = AppConfig({"explore": {"page_size": 10}})
    view = ExploreView(db, retriever, str(tmp_path / "workspace"), config)
    view.show()
    assert _wait_until(qapp, lambda: view.total_results == 25)
    content_layout = view.scroll.widget().layout()
    assert (
        content_layout.indexOf(view.keyword_card)
        < content_layout.indexOf(view.page_bar)
        < content_layout.indexOf(view.card_container)
    )
    assert view.page_bar.isVisible()
    assert view.page_size == 10
    assert view.page_status_label.text() == "第 1 / 3 页"
    assert "1–10 / 共 25 条" in view.page_range_label.text()

    view.page_next_button.click()
    assert _wait_until(qapp, lambda: view.page_status_label.text() == "第 2 / 3 页")
    assert view.current_page == 2
    assert "11–20 / 共 25 条" in view.page_range_label.text()

    view.page_input.setText("3")
    view.page_jump_button.click()
    assert _wait_until(qapp, lambda: view.page_status_label.text() == "第 3 / 3 页")
    assert view.current_page == 3
    assert not view.page_next_button.isEnabled()
    view.close()
    db.close()
