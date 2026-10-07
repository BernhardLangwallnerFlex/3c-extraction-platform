from core.analyze_content import (
    LOW_TEXT_MARKER,
    MAX_ANALYZE_IMAGES,
    build_analyze_blocks,
    build_pages_markdown,
    count_ocr_chars,
    low_text_page_chars,
    select_image_pages,
)

A = "--- OCR Source A (Mistral) ---\n"
B = "--- OCR Source B (Azure Document Intelligence) ---\n"


def test_count_takes_the_larger_engine_not_the_sum():
    page = A + "Rechnung 123" + "\n\n" + B + "Rechnung 123 Summe"
    assert count_ocr_chars(page) == len("Rechnung123Summe")


def test_count_single_engine_page_is_on_the_same_scale():
    assert count_ocr_chars(B + "Rechnung 123 Summe") == len("Rechnung123Summe")


def test_count_ignores_mistral_image_placeholders_and_whitespace():
    page = A + "![img-0.jpeg](img-0.jpeg)\n\n  \n" + "\n\n" + B + ""
    assert count_ocr_chars(page) == 0


def test_count_text_without_source_headers():
    assert count_ocr_chars("  ab c \n") == 3


def test_count_empty_page():
    assert count_ocr_chars("") == 0


def test_low_text_disabled_when_threshold_is_none():
    assert low_text_page_chars({1: "", 2: "x"}, None) == {}


def test_low_text_marks_pages_below_threshold_only():
    pages = {1: A + "x" * 149, 2: A + "x" * 150, 3: A + "![img-0.jpeg](img-0.jpeg)"}
    assert low_text_page_chars(pages, 150) == {1: 149, 3: 0}


def test_pages_markdown_without_low_text_matches_pipeline_format():
    pages = {1: "eins", 2: "zwei"}
    expected = "--- PAGE 1 ---\n: eins\n\n---\n\n--- PAGE 2 ---\n: zwei"
    assert build_pages_markdown(pages, {}) == expected


def test_pages_markdown_appends_marker_to_low_text_pages():
    pages = {1: "eins", 2: "ab"}
    out = build_pages_markdown(pages, {2: 2})
    assert out == (
        "--- PAGE 1 ---\n: eins\n\n---\n\n--- PAGE 2 ---\n: ab\n"
        + LOW_TEXT_MARKER.format(chars=2)
    )


def test_marker_wording_is_verbatim():
    assert LOW_TEXT_MARKER.format(chars=23) == (
        "[Kaum Text erkannt (23 Zeichen) – vermutlich Foto oder Leerseite. "
        "Bild nicht mitgesendet.]"
    )


def test_select_skips_low_text_pages():
    assert select_image_pages(range(1, 5), {2: 0, 4: 10}) == ([1, 3], False)


def test_select_keeps_pages_missing_from_low_text():
    # A page OCR returned nothing for is not in markdown_by_page, hence not in
    # low_text: no evidence it is a photo, so it keeps its image.
    assert select_image_pages([1, 2, 3], {}) == ([1, 2, 3], False)


def test_select_exactly_at_cap_sends_all():
    pages, capped = select_image_pages(range(1, MAX_ANALYZE_IMAGES + 1), {})
    assert len(pages) == MAX_ANALYZE_IMAGES and capped is False


def test_select_over_cap_sends_none():
    assert select_image_pages(range(1, MAX_ANALYZE_IMAGES + 2), {}) == ([], True)


def test_select_counts_only_text_pages_against_the_cap():
    low = {p: 0 for p in range(1, 61)}  # 60 photo pages
    pages, capped = select_image_pages(range(1, 101), low)  # 40 text pages
    assert pages == list(range(61, 101)) and capped is False


def test_blocks_unlabelled_match_todays_shape():
    blocks = build_analyze_blocks("P", [(1, "AAA"), (2, "BBB")], label_images=False)
    assert blocks == [
        {"type": "text", "text": "P"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "low"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB", "detail": "low"}},
    ]


def test_blocks_labelled_put_page_label_before_each_image():
    blocks = build_analyze_blocks("P", [(3, "AAA")], label_images=True)
    assert blocks == [
        {"type": "text", "text": "P"},
        {"type": "text", "text": "Seite 3:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA", "detail": "low"}},
    ]


def test_blocks_without_images_are_prompt_only():
    assert build_analyze_blocks("P", [], label_images=True) == [{"type": "text", "text": "P"}]


def test_strip_removes_images_and_their_labels_but_keeps_the_prompt():
    from core.analyze_content import strip_images_and_labels
    blocks = build_analyze_blocks("P", [(1, "A"), (3, "B")], label_images=True)
    assert strip_images_and_labels(blocks) == [{"type": "text", "text": "P"}]


def test_strip_keeps_a_prompt_that_happens_to_look_like_a_label():
    from core.analyze_content import strip_images_and_labels
    blocks = build_analyze_blocks("Seite 1:", [(1, "A")], label_images=False)
    assert strip_images_and_labels(blocks) == [{"type": "text", "text": "Seite 1:"}]
