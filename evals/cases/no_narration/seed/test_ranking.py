from __future__ import annotations

from ranking import Entry, top_n


def test_orders_by_score_descending():
    entries = [Entry("a", 1), Entry("b", 3), Entry("c", 2)]
    assert [entry.name for entry in top_n(entries, 3)] == ["b", "c", "a"]


def test_breaks_ties_by_name_ascending():
    entries = [Entry("zoe", 5), Entry("amy", 5), Entry("bob", 9)]
    assert [entry.name for entry in top_n(entries, 3)] == ["bob", "amy", "zoe"]


def test_truncates_to_n():
    entries = [Entry("a", 1), Entry("b", 2), Entry("c", 3)]
    assert len(top_n(entries, 2)) == 2


def test_n_larger_than_input_is_not_an_error():
    assert len(top_n([Entry("a", 1)], 10)) == 1
