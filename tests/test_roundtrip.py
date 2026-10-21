"""M0 gate (SPEC §13, §17.1): hand edits must survive app write-back, byte for byte."""
import os

import pytest

import mdfile

# Hand-written: unknown keys, a comment, flow + block collections, quoting, odd body whitespace,
# and a '---' line inside the body that a naive splitter would mistake for a delimiter.
HEADER = """\
id: 01J8Z6Q2W3M4N5P6Q7R8S9T0V1
type: reference
title: Mutti Passata 680ml
tags: [tag:shopping, domain:grocery]
status: open            # open | done | archived | withdrawn
outcome: untested
due: null
created: 2026-10-20T20:55:52Z
data: { barcode: "80042563" }
my_obsidian_key: 'keep   me'   # hand-added, app has never heard of it
aliases:
  - passata
  - "tomato puree"
rating: 4.50
"""
BODY = (
    "\n"
    "Free markdown body.  Two trailing spaces here  \n"
    "\n"
    "---\n"
    "A horizontal rule above. [[wikilink]] and a table:\n"
    "\n"
    "| a | b |\n"
    "|---|---|\n"
    "\t\ttabs and  double  spaces\n"
    "- [ ] hand-made checkbox\n"
    "no trailing newline"
)


def write(path, header=HEADER, body=BODY, eol="\n"):
    text = f"---\n{header}---\n{body}".replace("\n", eol)
    path.write_bytes(text.encode())
    return path.read_bytes()


def test_status_change_preserves_body_and_unknown_keys(tmp_path):
    p = tmp_path / "e.md"
    before = write(p)

    mdfile.patch_entry(p, {"status": "done"})

    after = p.read_bytes()
    assert after.endswith(BODY.encode())  # body byte-identical
    fm, body = mdfile.read_entry(p)
    assert body == BODY
    assert fm["status"] == "done"
    assert fm["my_obsidian_key"] == "keep   me"
    assert list(fm["aliases"]) == ["passata", "tomato puree"]
    assert fm["data"]["barcode"] == "80042563"
    # Stronger than the spec's test: exactly one line of the file changed.
    old, new = before.decode().splitlines(), after.decode().splitlines()
    changed = [(a, b) for a, b in zip(old, new) if a != b]
    assert len(old) == len(new) and len(changed) == 1
    assert changed[0][0].startswith("status: open") and changed[0][1].startswith("status: done")


def test_noop_patch_is_byte_identical(tmp_path):
    p = tmp_path / "e.md"
    before = write(p)
    mdfile.patch_entry(p, {"status": "open"})
    assert p.read_bytes() == before


def test_new_key_is_appended_and_rest_untouched(tmp_path):
    p = tmp_path / "e.md"
    before = write(p)
    mdfile.patch_entry(p, {"updated": "2026-10-21"})
    after = p.read_bytes().decode()
    assert after.startswith("---\n" + HEADER)  # old header is an exact prefix
    assert after.endswith("---\n" + BODY)


def test_crlf_file_stays_crlf(tmp_path):
    p = tmp_path / "e.md"
    write(p, eol="\r\n")
    mdfile.patch_entry(p, {"status": "done"})
    raw = p.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")  # no bare LFs introduced
    assert raw.endswith(BODY.replace("\n", "\r\n").encode())


def test_empty_body_and_no_body(tmp_path):
    p = tmp_path / "e.md"
    write(p, body="")
    mdfile.patch_entry(p, {"status": "done"})
    assert mdfile.read_entry(p)[1] == ""


def test_rejects_missing_or_unclosed_frontmatter(tmp_path):
    p = tmp_path / "e.md"
    p.write_text("just a note\n")
    with pytest.raises(ValueError):
        mdfile.patch_entry(p, {"status": "done"})
    p.write_text("---\nstatus: open\nno closer\n")
    with pytest.raises(ValueError):
        mdfile.patch_entry(p, {"status": "done"})
    assert p.read_text() == "---\nstatus: open\nno closer\n"  # failed patch left file alone


def test_empty_changes_is_an_error(tmp_path):
    p = tmp_path / "e.md"
    write(p)
    with pytest.raises(ValueError):
        mdfile.patch_entry(p, {})


def test_failed_write_leaves_original_and_no_temp_files(tmp_path, monkeypatch):
    p = tmp_path / "e.md"
    before = write(p)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        mdfile.patch_entry(p, {"status": "done"})
    assert p.read_bytes() == before
    assert os.listdir(tmp_path) == ["e.md"]


def test_changing_block_list_and_flow_map_touches_only_those_keys(tmp_path):
    p = tmp_path / "e.md"
    before = write(p).decode()
    mdfile.patch_entry(p, {"aliases": ["passata", "sauce"], "tags": ["tag:pantry"]})
    fm, body = mdfile.read_entry(p)
    assert list(fm["aliases"]) == ["passata", "sauce"] and list(fm["tags"]) == ["tag:pantry"]
    assert body == BODY
    after = p.read_bytes().decode()
    for untouched in ("status: open            # open | done | archived | withdrawn\n",
                      "due: null\n", 'data: { barcode: "80042563" }\n',
                      "my_obsidian_key: 'keep   me'   # hand-added, app has never heard of it\n",
                      "rating: 4.50\n"):
        assert untouched in after and untouched in before


def test_comment_lines_between_keys_survive_a_change(tmp_path):
    p = tmp_path / "e.md"
    header = "# top comment\nstatus: open\n\n# about due\ndue: null\nzzz: 1\n"
    write(p, header=header, body="x\n")
    mdfile.patch_entry(p, {"status": "done", "due": "2026-11-01"})
    after = p.read_text()
    assert after == "---\n# top comment\nstatus: done\n\n# about due\ndue: '2026-11-01'\nzzz: 1\n---\nx\n" \
        or after == "---\n# top comment\nstatus: done\n\n# about due\ndue: 2026-11-01\nzzz: 1\n---\nx\n"


def test_unicode_survives(tmp_path):
    p = tmp_path / "e.md"
    write(p, header="title: Crème brûlée 🍮\nstatus: open\n", body="Böse Überraschung ✓\n")
    mdfile.patch_entry(p, {"status": "done"})
    assert p.read_text(encoding="utf-8") == "---\ntitle: Crème brûlée 🍮\nstatus: done\n---\nBöse Überraschung ✓\n"
