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
input[type=text]{flex:1;font:inherit;padding:.55rem .7rem;border:1px solid var(--rule);background:transparent;color:inherit;border-radius:4px}
button{font:600 .9rem system-ui,sans-serif;padding:.5rem .9rem;border:1px solid var(--acc);background:var(--acc);color:var(--bg);border-radius:4px;cursor:pointer}
button.q{background:transparent;color:var(--acc)}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.e{padding:.9rem 0;border-top:1px solid var(--rule)}
.e h2{font:600 1.1rem Charter,Georgia,serif;margin:0}.e h2 a{text-decoration:none}
.m{font:.82rem system-ui,sans-serif;color:var(--mute);margin:.15rem 0 .3rem}
.chip{display:inline-block;font:.78rem system-ui,sans-serif;background:var(--chip);padding:.05rem .45rem;border-radius:3px;margin:0 .25rem .25rem 0;text-decoration:none}
.p{color:var(--warn)}pre.b{white-space:pre-wrap;font:inherit;margin:1rem 0}
nav{font:.9rem system-ui,sans-serif;margin-bottom:1rem}
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


def home(rows: list[dict], q: str, tag: str, type_: str) -> str:
    heading = "Recent" if not (q or tag or type_) else f"{len(rows)} found"
    types = "".join(f'<a class="chip" href="/?type={t}">{t}</a>' for t in sorted(TYPES))
    form = (f'<form class="s"><input type="text" name="q" value="{e(q)}" '
            f'placeholder="Search recipes, discoveries, dead ends" aria-label="Search">'
            f'<button>Search</button></form><div>{types}</div>')
    empty = "<p>Nothing here yet. Save something from any connected client and it will show up.</p>"
    return form + f'<div class="m">{heading}</div>' + ("".join(card(r) for r in rows) or empty)


def entry(x: dict) -> str:
    src = (f'<div class="m">Source: <a href="{e(x["source_url"])}">{e(x["source_url"])}</a></div>'
           if x["source_url"] else "")
    ver = f'verified {x["last_verified"][:10]}' if x["last_verified"] else "not yet verified"
    outcome = f" · {x['outcome']}" if x["outcome"] else ""
    acts = "".join(
        f'<form method="post" action="/e/{x["id"]}/act" style="display:inline">'
        f'<input type="hidden" name="do" value="{v}"><button class="{"" if v == "approve" else "q"}">{label}</button></form> '
        for v, label in (("approve", "Approve and mark verified"), ("reject", "Reject")))
    return (f'<nav><a href="/">All entries</a></nav>'
            f'<h2 style="font-size:1.4rem;margin:.2rem 0">{e(x["title"])}</h2>'
            f'<div class="m">{x["type"]}{outcome} · by {e(x["author"])} · {x["created_at"][:10]} · '
            f'{x["status"]} · {ver} · confidence {x["confidence"]:.1f}</div>'
            f'<div>{chips(x["tags"])}</div>{src}<pre class="b">{e(x["body"])}</pre>{acts}')


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
