"""
Markdown-with-frontmatter files: the source of truth (SPEC §5, §13).

Round-trip contract:
  * the body is never parsed or re-rendered; it is carried through as the exact original string
  * ruamel.yaml's round-trip loader locates each key's lines; only the lines of keys named in
    `changes` are replaced, so unknown keys, comments, ordering, quoting and blank lines are
    never re-rendered. The patched result is re-parsed and verified before it is written
  * writes are temp file + rename, so a crash never leaves a half-written entry
No regex anywhere in here.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_DELIM = "---"


def _yaml() -> YAML:
    y = YAML()  # round-trip mode (typ="rt") is the default
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap long lines
    y.indent(mapping=2, sequence=4, offset=2)  # matches the layout in SPEC §5/§6
    return y


@dataclass
class MdFile:
    opener: str  # the exact opening "---" line, including its line ending
    header: str  # raw YAML text between the delimiters
    closer: str  # the exact closing "---" line, including its line ending
    body: str  # everything after the closer, verbatim

    @property
    def eol(self) -> str:
        return "\r\n" if self.opener.endswith("\r\n") else "\n"

    def frontmatter(self) -> CommentedMap:
        data = _yaml().load(self.header) if self.header.strip() else CommentedMap()
        if not isinstance(data, CommentedMap):
            raise ValueError("frontmatter must be a YAML mapping")
        return data

    def render(self) -> str:
        return self.opener + self.header + self.closer + self.body


def split(text: str) -> MdFile:
    """Split raw file text into frontmatter and body without altering a single character."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _DELIM:
        raise ValueError("file has no frontmatter (must start with a '---' line)")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _DELIM:
            return MdFile(opener=lines[0], header="".join(lines[1:i]), closer=lines[i],
                          body="".join(lines[i + 1:]))
    raise ValueError("frontmatter is not closed (missing second '---' line)")


def read_text(path: str | Path) -> str:
    # newline="" disables newline translation so CRLF files come back byte-for-byte.
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def read_entry(path: str | Path) -> tuple[CommentedMap, str]:
    """Return (frontmatter, body). Treat the frontmatter as read-only; change files via patch_entry."""
    md = split(read_text(path))
    return md.frontmatter(), md.body


def _dump_key(key: str, value, eol: str, comment: str | None = None, column: int = 0) -> str:
    """Render one top-level `key: value` entry the way it would appear in the file."""
    one = CommentedMap()
    one[key] = value
    if comment and not isinstance(value, (dict, list)):
        one.yaml_add_eol_comment(comment, key, column=column)
    buf = io.StringIO()
    _yaml().dump(one, buf)
    out = buf.getvalue()
    return out if eol == "\n" else out.replace("\n", eol)


def _splice(md: MdFile, fm: CommentedMap, changes: dict) -> str:
    """Return a new header with only the changed keys' lines replaced (or appended)."""
    lines = md.header.splitlines(keepends=True)
    keys = list(fm.keys())
    spans: list[tuple[int, int, str]] = []  # (first line, end line, replacement), original coords
    appended = ""
    for k, v in changes.items():
        if k in fm:
            if fm[k] == v:
                continue  # already the requested value: leave the bytes alone
            i = keys.index(k)
            first = fm.lc.key(k)[0]
            end = fm.lc.key(keys[i + 1])[0] if i + 1 < len(keys) else len(lines)
            # Comments/blank lines after the value belong to the gap, not to this key.
            while end - 1 > first and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
                end -= 1
            # Keep a trailing inline comment (e.g. "status: open   # open | done | ...").
            tok = fm.ca.items.get(k, [None] * 3)[2]
            inline = tok is not None and tok.start_mark.line == first and tok.value.startswith("#")
            note = tok.value.rstrip("\r\n") if inline and end - first == 1 else None
            spans.append((first, end, _dump_key(k, v, md.eol, note, tok.column if note else 0)))
        else:
            appended += _dump_key(k, v, md.eol)
    for first, end, text in sorted(spans, reverse=True):  # bottom-up keeps earlier indices valid
        lines[first:end] = [text]
    return "".join(lines) + appended


def patch_entry(path: str | Path, changes: dict) -> None:
    """Set the given top-level frontmatter keys and write the file back atomically.

    Only the lines of the changed keys are rewritten; every other key, comment, blank line and
    the entire body stay exactly as they were. If nothing actually changes, the file is not
    touched. A changed key's whole value is replaced (a nested edit re-renders that key only).
    """
    if not changes:
        raise ValueError("no changes given")
    md = split(read_text(path))
    fm = md.frontmatter()
    header = _splice(md, fm, changes)
    if header == md.header:
        return
    patched = MdFile(md.opener, header, md.closer, md.body)
    # Safety net: the result must parse, hold the requested values, and be otherwise identical.
    check = patched.frontmatter()
    expected = {**{k: fm[k] for k in fm}, **changes}
    if list(check.keys()) != list(expected.keys()) or any(check[k] != expected[k] for k in expected):
        raise ValueError("refusing to write: patched frontmatter does not match the intended change")
    _atomic_write(Path(path), patched.render())


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)  # the atomic commit point
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
