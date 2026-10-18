from rag.ingest import chunk_markdown


def test_chunks_keep_their_heading() -> None:
    md = "# Leave policy\n\nStaff get 28 days.\n\n## Sick leave\n\nUp to 6 months on full pay."
    chunks = chunk_markdown(md)
    assert [(c.heading, c.text) for c in chunks] == [
        ("Leave policy", "Staff get 28 days."),
        ("Sick leave", "Up to 6 months on full pay."),
    ]


def test_long_sections_split_with_overlap() -> None:
    para = "neno " * 1000  # 5,000 characters of Swahili filler
    chunks = chunk_markdown(f"# Big\n\n{para}", max_chars=1800, overlap=200)
    assert len(chunks) >= 3
    assert all(len(c.text) <= 1800 for c in chunks)
    assert chunks[0].text[-200:] == chunks[1].text[:200]


def test_no_empty_chunks() -> None:
    assert chunk_markdown("# Only a heading\n\n\n") == []
