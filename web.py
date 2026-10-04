"""HTML for the browse UI. Pure functions that return strings; routes live in app.py."""
from __future__ import annotations

import html

from core import TYPES

e = html.escape

CSS = """
:root{--bg:#f6f7f5;--ink:#1d2430;--mute:#5d6877;--rule:#d9dde3;--acc:#1b6b86;--chip:#e6edf0;--warn:#9a5b00}
@media(prefers-color-scheme:dark){:root{--bg:#14181e;--ink:#e4e8ee;--mute:#98a3b3;--rule:#2b323c;--acc:#6bb8d1;--chip:#222a34;--warn:#e0a24a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 Charter,"Iowan Old Style",Georgia,serif}
main{max-width:46rem;margin:0 auto;padding:1.25rem 1rem 4rem}
a{color:var(--acc)}h1{font:600 1.5rem system-ui,sans-serif;margin:.2rem 0 1rem}
h1 a{color:inherit;text-decoration:none}
form.s{display:flex;gap:.5rem;margin-bottom:1rem}
input[type=text],textarea{flex:1;font:inherit;padding:.55rem .7rem;border:1px solid var(--rule);background:transparent;color:inherit;border-radius:4px}
textarea{width:100%;resize:vertical}
button{font:600 .9rem system-ui,sans-serif;padding:.5rem .9rem;border:1px solid var(--acc);background:var(--acc);color:var(--bg);border-radius:4px;cursor:pointer}
button.q{background:transparent;color:var(--acc)}
button.danger{border-color:#c0392b;color:#c0392b;background:transparent}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.e{padding:.9rem 0;border-top:1px solid var(--rule)}
.e h2{font:600 1.1rem Charter,Georgia,serif;margin:0}.e h2 a{text-decoration:none}
.m{font:.82rem system-ui,sans-serif;color:var(--mute);margin:.15rem 0 .3rem}
.chip{display:inline-block;font:.78rem system-ui,sans-serif;background:var(--chip);padding:.05rem .45rem;border-radius:3px;margin:0 .25rem .25rem 0;text-decoration:none}
.p{color:var(--warn)}pre.b{white-space:pre-wrap;font:inherit;margin:1rem 0}
nav{font:.9rem system-ui,sans-serif;margin-bottom:1rem}
label{display:block;font:.85rem system-ui,sans-serif;color:var(--mute);margin:.8rem 0 .2rem}
.field{margin-bottom:.5rem}
.pager{display:flex;gap:1rem;justify-content:center;margin-top:1.5rem;font:.9rem system-ui,sans-serif}
"""


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>'
            f"<style>{CSS}</style><main><h1><a href='/'>Breadcrumbs</a></h1>{body}</main></html>")


def chips(tags: list[str]) -> str:
    return "".join(f'<a class="chip" href="/?tag={e(t)}">{e(t)}</a>' for t in tags)


def card(r: dict) -> str:
    flag = ' <span class="p">· awaiting review</span>' if r["status"] == "proposed" else ""
    outcome = f" · {r['outcome']}" if r["outcome"] else ""
    meta = f'{r["type"]}{outcome} · {r["created_at"][:10]} · {e(r["author"])}{flag}'
    summ = f'<div>{e(r["summary"])}</div>' if r["summary"] else ""
    return (f'<div class="e"><h2><a href="/e/{r["id"]}">{e(r["title"])}</a></h2>'
            f'<div class="m">{meta}</div>{summ}<div>{chips(r["tags"])}</div></div>')


def home(rows: list[dict], q: str, tag: str, type_: str, page: int = 0,
         has_prev: bool = False, has_next: bool = False) -> str:
    heading = "Recent" if not (q or tag or type_) else f"{len(rows)} result{'s' if len(rows) != 1 else ''}"
    types = "".join(f'<a class="chip" href="/?type={t}">{t}</a>' for t in sorted(TYPES))
    form = (f'<form class="s"><input type="text" name="q" value="{e(q)}" '
            f'placeholder="Search recipes, discoveries, dead ends" aria-label="Search">'
            f'<button>Search</button></form><div>{types}</div>')
    empty = "<p>Nothing here yet. Save something from any connected client and it will show up.</p>"

    def page_url(p: int) -> str:
        params = [f"page={p}"]
        if q: params.append(f"q={e(q)}")
        if tag: params.append(f"tag={e(tag)}")
        if type_: params.append(f"type={e(type_)}")
        return "/?" + "&".join(params)

    pager = ""
    if has_prev or has_next:
        prev = f'<a href="{page_url(page - 1)}">← Previous</a>' if has_prev else '<span style="opacity:.35">← Previous</span>'
        nxt = f'<a href="{page_url(page + 1)}">Next →</a>' if has_next else '<span style="opacity:.35">Next →</span>'
        pager = f'<div class="pager">{prev}{nxt}</div>'

    return form + f'<div class="m">{heading}</div>' + ("".join(card(r) for r in rows) or empty) + pager


def entry(x: dict) -> str:
    src = (f'<div class="m">Source: <a href="{e(x["source_url"])}">{e(x["source_url"])}</a></div>'
           if x["source_url"] else "")
    ctx = f'<div class="m">Context: {e(x["source_context"])}</div>' if x.get("source_context") else ""
    ver = f'verified {x["last_verified"][:10]}' if x["last_verified"] else "not yet verified"
    outcome = f" · {x['outcome']}" if x["outcome"] else ""
    edit_link = f'<a href="/e/{x["id"]}/edit" style="font:.85rem system-ui,sans-serif">Edit</a>'
    act_buttons = []
    if x["status"] != "approved":
        act_buttons.append(("approve", "Approve and mark verified", ""))
    act_buttons.append(("archive", "Archive", "q"))
    act_buttons.append(("reject", "Reject", "danger"))
    acts = " ".join(
        f'<form method="post" action="/e/{x["id"]}/act" style="display:inline">'
        f'<input type="hidden" name="do" value="{v}"><button class="{cls}">{label}</button></form>'
        for v, label, cls in act_buttons)
    return (f'<nav><a href="/">All entries</a> · {edit_link}</nav>'
            f'<h2 style="font-size:1.4rem;margin:.2rem 0">{e(x["title"])}</h2>'
            f'<div class="m">{x["type"]}{outcome} · by {e(x["author"])} · {x["created_at"][:10]} · '
            f'{x["status"]} · {ver} · confidence {x["confidence"]:.1f}</div>'
            f'<div>{chips(x["tags"])}</div>{src}{ctx}<pre class="b">{e(x["body"])}</pre>{acts}')


def edit_form(x: dict) -> str:
    tags_val = ", ".join(x.get("tags") or [])
    def field(label_text: str, name: str, val: str, textarea: bool = False) -> str:
        esc_val = e(val or "")
        inp = (f'<textarea name="{name}" rows="8">{esc_val}</textarea>' if textarea
               else f'<input type="text" name="{name}" value="{esc_val}">')
        return f'<div class="field"><label>{label_text}</label>{inp}</div>'
    return (f'<nav><a href="/e/{x["id"]}">{e(x["title"])}</a></nav>'
            f'<h2 style="font-size:1.3rem;margin:.2rem 0">Edit entry</h2>'
            f'<form method="post" action="/e/{x["id"]}/edit">'
            + field("Title", "title", x["title"])
            + field("Body", "body", x["body"], textarea=True)
            + field("Summary (one line)", "summary", x.get("summary") or "")
            + field("Outcome", "outcome", x.get("outcome") or "")
            + field("Tags (comma-separated)", "tags", tags_val)
            + field("Source URL", "source_url", x.get("source_url") or "")
            + field("Source context", "source_context", x.get("source_context") or "")
            + f'<button type="submit">Save changes</button> '
            + f'<a href="/e/{x["id"]}" style="margin-left:.5rem;font:.9rem system-ui,sans-serif">Cancel</a>'
            + '</form>')


def login(client_name: str, req: str, error: str = "") -> str:
    err = f'<p class="p">{e(error)}</p>' if error else ""
    return (f'<h2 style="font-size:1.3rem;margin:.2rem 0">Connect {e(client_name)}</h2>'
            f"<p>{e(client_name)} is asking to read and write your Breadcrumbs. "
            f"Enter your Breadcrumbs key to allow it.</p>{err}"
            f'<form method="post" action="/login" class="s">'
            f'<input type="hidden" name="req" value="{e(req)}">'
            f'<input type="password" name="key" placeholder="Your key" aria-label="Your key" '
            f'autocomplete="current-password" required '
            f'style="flex:1;font:inherit;padding:.55rem .7rem;border:1px solid var(--rule);'
            f'background:transparent;color:inherit;border-radius:4px">'
            f"<button>Allow</button></form>"
            f'<p class="m">Only approve this if you started the connection yourself.</p>')
