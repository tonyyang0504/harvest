"""Public-suffix-aware registrable domains (eTLD+1) for census dedup.

Uses the full Public Suffix List when `HARVEST_PSL_FILE` points to a `public_suffix_list.dat`
(https://publicsuffix.org/list/), otherwise a built-in subset in the same format plus the ccSLD
heuristic (`<generic-label>.<cc>` is a suffix: com.br, co.nz, or.jp ...). Wildcard (`*.ck`) and
exception (`!www.ck`) rules are honoured.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from urllib.parse import urlsplit

_BUILTIN = """
// ICANN second-level registries commonly seen on listing sites
co.uk org.uk me.uk ltd.uk plc.uk net.uk ac.uk gov.uk
com.au net.au org.au edu.au gov.au asn.au id.au
co.nz net.nz org.nz geek.nz school.nz govt.nz
co.jp ne.jp or.jp ac.jp go.jp gr.jp ed.jp lg.jp
com.br net.br org.br gov.br blog.br art.br
com.ar com.mx com.co com.pe com.ec com.uy com.py com.bo com.ve com.gt com.sv com.hn com.ni com.do com.pa com.cu
com.tr net.tr org.tr gen.tr biz.tr av.tr
co.in net.in org.in firm.in gen.in ind.in
com.cn net.cn org.cn gov.cn
com.hk net.hk org.hk com.tw net.tw org.tw idv.tw com.sg net.sg org.sg com.my net.my org.my
co.id or.id web.id my.id biz.id ac.id co.th in.th or.th go.th com.ph net.ph org.ph com.vn net.vn
co.kr or.kr ne.kr go.kr
co.za org.za web.za net.za co.il org.il net.il
com.eg com.sa net.sa com.kw com.qa com.bh com.om co.ae net.ae org.ae com.jo com.lb
com.pk net.pk org.pk com.bd com.np com.lk
co.ke or.ke com.ng co.tz co.ug com.gh co.zw
com.ua in.ua kiev.ua org.ua net.ua
com.ru msk.ru spb.ru net.ru org.ru
com.kz org.kz net.kz com.ge org.ge net.ge com.az com.am com.by com.uz co.uz
com.pl net.pl org.pl com.gr com.cy com.mt
// private suffixes (hosting platforms): each subdomain is a separate site
github.io gitlab.io blogspot.com herokuapp.com appspot.com netlify.app vercel.app pages.dev
azurewebsites.net cloudfront.net web.app firebaseapp.com myshopify.com wixsite.com
githubusercontent.com readthedocs.io webflow.io workers.dev onrender.com fly.dev
// wildcard examples from the PSL
*.ck
!www.ck
"""

_GENERIC_SLD = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "gv", "nic",
                "ltd", "plc", "info", "biz", "web", "nom", "mil"}


def _parse(text: str) -> tuple[set, set, set]:
    rules, wild, exc = set(), set(), set()
    for raw in text.splitlines():
        line = raw.strip().split()[0] if raw.strip() else ""
        if not line or line.startswith("//"):
            continue
        for tok in raw.split():
            tok = tok.strip().lower()
            if not tok or tok.startswith("//"):
                break
            if tok.startswith("!"):
                exc.add(tok[1:])
            elif tok.startswith("*."):
                wild.add(tok[2:])
            else:
                rules.add(tok)
    return rules, wild, exc


@lru_cache(maxsize=1)
def _rules() -> tuple[set, set, set, bool]:
    path = os.environ.get("HARVEST_PSL_FILE")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            r, w, e = _parse(f.read())
        return r, w, e, True
    r, w, e = _parse(_BUILTIN)
    return r, w, e, False


def host_of(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if "://" not in s:
        s = "http://" + s
    host = urlsplit(s).hostname or ""
    host = host.strip(".")
    try:
        host = host.encode("idna").decode("ascii") if any(ord(ch) > 127 for ch in host) else host
    except UnicodeError:
        pass
    return re.sub(r"^www\d*\.", "", host) if host.count(".") >= 2 else host


def public_suffix(host: str) -> str:
    rules, wild, exc, full = _rules()
    labels = host.split(".")
    best = labels[-1]
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if cand in exc:
            return ".".join(labels[i + 1:])
        if cand in rules:
            best = cand if len(cand) > len(best) else best
            break
        parent = ".".join(labels[i + 1:])
        if parent and parent in wild:
            best = cand
            break
    if best == labels[-1] and not full and len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _GENERIC_SLD:
        best = ".".join(labels[-2:])
    return best


# shared hosts where the owner is in the path, not the host: one site per owner (and repo), never one for the whole host
PATH_OWNER_HOSTS = {"raw.githubusercontent.com": 2, "github.com": 1, "gitlab.com": 1, "codeberg.org": 1, "bitbucket.org": 1,
                    "sites.google.com": 2, "docs.google.com": 3, "huggingface.co": 2, "buttondown.com": 1, "medium.com": 1, "linktr.ee": 1}
# shared hosts where each subdomain is a different owner (newsletters, blogs), whether or not a PSL lists them
SUBDOMAIN_OWNER_DOMAINS = {"substack.com", "wordpress.com", "tumblr.com", "beehiiv.com", "ghost.io", "notion.site", "carrd.co", "wixsite.com"}


def path_owner_key(url: str) -> str | None:
    """`https://raw.githubusercontent.com/tech-conferences/conference-data/main/x.json` -> `raw.githubusercontent.com/tech-conferences/conference-data`;
    None for any other host."""
    host = host_of(url)
    n = PATH_OWNER_HOSTS.get(host)
    if not n:
        parent = host.split(".", 1)[1] if host.count(".") >= 2 else ""
        return host if parent in SUBDOMAIN_OWNER_DOMAINS else None
    parts = [x for x in urlsplit(url if "://" in url else "http://" + url).path.split("/") if x]
    if len(parts) < n:
        return None
    return host + "/" + "/".join(p.lower() for p in parts[:n])


def registrable_domain(url_or_host: str) -> str:
    """eTLD+1: `https://m.kolesa.kz/cars` -> `kolesa.kz`, `shop.example.co.uk` -> `example.co.uk`."""
    host = host_of(url_or_host)
    if not host or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return host
    suffix = public_suffix(host)
    rest = host[: -len(suffix)].rstrip(".") if host != suffix else ""
    if not rest:
        return host
    return rest.split(".")[-1] + "." + suffix
