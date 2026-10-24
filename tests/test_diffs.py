from triage import diffs

OLD = """Acme is a company.
== History ==
Founded in 1990.<ref name="a">Book</ref>
== Products ==
Widgets.
{{reflist}}
<references />"""


def test_refs_count_openings_named_and_self_closing():
    new = OLD.replace("Widgets.", 'Widgets and gadgets.<ref>Site</ref><ref name="a" /><REF name=b/>')
    m = diffs.measure(OLD, new)
    assert (m["refs_added"], m["refs_removed"]) == (3, 0)


def test_refs_removed_edited_and_moved():
    removed = diffs.measure(OLD, OLD.replace('<ref name="a">Book</ref>', ""))
    assert (removed["refs_added"], removed["refs_removed"]) == (0, 1)
    edited = diffs.measure(OLD, OLD.replace(">Book<", ">Book, p. 4<"))
    assert (edited["refs_added"], edited["refs_removed"]) == (1, 1)
    ref = '<ref name="a">Book</ref>'
    moved = diffs.measure(OLD, OLD.replace(ref, "").replace("Widgets.", "Widgets." + ref))
    assert (moved["refs_added"], moved["refs_removed"]) == (0, 0)


def test_references_tag_is_not_a_ref():
    m = diffs.measure(OLD, OLD.replace("<references />", "<references/>\n[[Category:Firms]]"))
    assert (m["refs_added"], m["refs_removed"]) == (0, 0)


def test_words_added_and_removed():
    m = diffs.measure(OLD, OLD.replace("Founded in 1990.", "Founded by Jane Doe in 1991."))
    assert (m["words_added"], m["words_removed"]) == (4, 1)
    assert diffs.measure(OLD, OLD) == {
        "words_added": 0,
        "words_removed": 0,
        "refs_added": 0,
        "refs_removed": 0,
        "sections": [],
        "words_removed_by_section": {},
    }


def test_words_removed_by_old_side_section():
    new = OLD.replace("Founded in 1990.", "Founded.").replace("== Products ==\nWidgets.\n", "")
    m = diffs.measure(OLD, new)
    # "in 1990" (with the ref's words untouched) from History; the heading and "Widgets" from Products
    assert m["words_removed_by_section"] == {"History": 2, "Products": 2}
    assert m["words_removed"] == 4
    renamed = diffs.measure(OLD, OLD.replace("== History ==", "== Early years =="))
    assert renamed["words_removed_by_section"] == {"History": 1}  # the old heading's word


def test_sections_touched_lead_and_headings_in_page_order():
    new = OLD.replace("Widgets.", "Widgets, mostly.").replace("Acme is a company.", "Acme is a firm.")
    assert diffs.measure(OLD, new)["sections"] == ["(lead)", "Products"]
    assert diffs.measure(OLD, OLD.replace("Founded", "Started"))["sections"] == ["History"]


def test_renamed_heading_names_both_sections():
    new = OLD.replace("== History ==", "==History and legal issues==")
    assert diffs.measure(OLD, new)["sections"] == ["History", "History and legal issues"]


def test_removed_section_is_named():
    new = OLD.replace("== Products ==\nWidgets.\n", "")
    assert diffs.measure(OLD, new)["sections"] == ["Products"]


def test_edit_diff_is_trimmed_to_the_cap():
    new = OLD + "\n" + "\n".join(f"Line {i} of padding text." for i in range(1000))
    diff, measured = diffs.edit_diff(OLD, new)
    assert len(diff) <= diffs.DIFF_MAX_CHARS + len("\n[truncated]")
    assert diff.endswith("[truncated]")
    assert measured["words_added"] == 5000
    small, _ = diffs.edit_diff(OLD, OLD.replace("Widgets.", "Gadgets."))
    assert "-Widgets." in small and "+Gadgets." in small and "[truncated]" not in small
