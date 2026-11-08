"""Public source search with conservative matching and an explicit fallback."""

import os
import re
import asyncio
import unicodedata
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

from .feeds import clean, public_url
from .credentials import brave_key

NAME = re.compile(r"[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){1,3}", re.UNICODE)
TECH = re.compile(r"\b(engineer|engineering|developer|researcher|scientist|engineering manager)\b", re.I)
AUTHOR_ROLE = re.compile(r"\b(?:engineer|engineering manager|engineering lead|research scientist)\b", re.I)
CONTENT_TITLE = re.compile(r"\b(team|blog|engineering|software|platform|data|career|jobs|home|conference|speaker|meet|build|how|why|introducing|about)\b", re.I)
STRIPE_SITEMAP = "https://stripe.dev/sitemap.xml"
STRIPE_ENGINEERING = "https://stripe.dev/blog/topic/engineering"
CLOUDFLARE_BLOG = "https://blog.cloudflare.com/"
ROLE_AREAS = {
    "infrastructure": re.compile(r"\b(?:infrastructure|platform|systems?|kubernetes|cloud|devops|sre)\b", re.I),
    "security": re.compile(r"\b(?:security|cybersecurity|red team|detection|iam)\b", re.I),
    "data": re.compile(r"\b(?:data|analytics|database)\b", re.I),
    "machine learning": re.compile(r"\b(?:ai|ml|machine learning|artificial intelligence)\b", re.I),
    "frontend": re.compile(r"\b(?:front[ -]?end|web|ui)\b", re.I),
    "backend": re.compile(r"\b(?:back[ -]?end|server[ -]?side)\b", re.I),
}


def search_terms(job):
    title = job["title"].lower()
    if re.search(r"\b(data|analytics)\b", title):
        specialty = "data engineering"
    elif re.search(r"\b(ai|ml|machine learning|research)\b", title):
        specialty = "machine learning"
    else:
        specialty = "software engineering"
    return f'"{job["company"]}" "{specialty}"', specialty


def relevant_areas(job, person):
    job_text = f"{job['title']} {job.get('department', '')}"
    person_text = f"{person.get('title', '')} {person.get('notes', '')}"
    return [area for area, pattern in ROLE_AREAS.items() if pattern.search(job_text) and pattern.search(person_text)]


def rank_candidates(job, candidates):
    ranked = []
    for person in candidates:
        areas = relevant_areas(job, person)
        explanation = (f"Job and profile suggest a shared area in {', '.join(areas)}. " if areas else
                       "The source does not establish a matching specialty. ")
        ranked.append((len(areas), {**person, "reason": person["reason"] + " " + explanation +
                                     "The job's team is unconfirmed."}))
    return [person for _, person in sorted(ranked, key=lambda row: -row[0])]


def public_candidates(results, company, specialty):
    matches = []
    for row in results:
        url = public_url(row.get("url"))
        host = urlsplit(url).hostname or ""
        if not host or any(host == site or host.endswith("." + site)
                           for site in ("linkedin.com", "zoominfo.com", "rocketreach.co", "theorg.com")):
            continue
        heading, excerpt = clean(row.get("title")), clean(row.get("description"))
        name = re.split(r"\s+[–—|:-]\s+|,", heading, maxsplit=1)[0].strip()
        if (not NAME.fullmatch(name) or CONTENT_TITLE.search(name) or name.casefold() == company.casefold()
                or not re.search(r"(?<!\w)" + re.escape(company) + r"(?!\w)", excerpt + " " + heading, re.I)
                or not TECH.search(excerpt + " " + heading)):
            continue
        score = 2 if specialty.split()[0] in excerpt.lower() else 1
        matches.append((score, dict(name=name, title=heading[:200], source=url,
                                     notes=excerpt[:350], reason="Public source links this person to the company and engineering. Verify current employment.")))
    return [person for _, person in sorted(matches, key=lambda row: -row[0])]


async def stripe_authors():
    """Read Stripe's public author bios, keeping recently published engineers."""
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(STRIPE_SITEMAP)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        urls = [node.text for node in root.iter() if node.tag.endswith("}loc") and node.text
                and re.fullmatch(r"https://stripe\.dev/authors/[a-z0-9-]+", node.text)]
        try:
            response = await client.get(STRIPE_ENGINEERING)
            response.raise_for_status()
            lines = clean(response.text).splitlines()
            names = [name.strip() for i, line in enumerate(lines[:-1]) if line in {"Author:", "Authors:"}
                     for name in lines[i + 1].split(",") if NAME.fullmatch(name.strip())]
            paths = {url.rsplit("/", 1)[-1]: url for url in urls}
            recent = []
            for name in names:
                ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
                slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
                if slug in paths and paths[slug] not in recent:
                    recent.append(paths[slug])
            urls = recent + [url for url in urls if url not in recent]
        except httpx.HTTPError:
            pass
        urls = urls[:16]
        limit = asyncio.Semaphore(4)

        async def author(url):
            async with limit:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    lines = clean(response.text).splitlines()
                    heading = next((i for i, line in enumerate(lines) if re.fullmatch(r"Blog: .+\(\d+\)", line)), None)
                    if heading is None or heading + 1 >= len(lines):
                        return None
                    name = re.sub(r"\(\d+\)$", "", lines[heading].removeprefix("Blog: ")).strip()
                    bio = re.split(r"(?<=[.!?])\s+", lines[heading + 1], maxsplit=1)[0]
                    dates = []
                    for line in lines[heading + 2:]:
                        if re.fullmatch(r"20\d{2}\.\d{1,2}\.\d{1,2}", line):
                            try:
                                dates.append(datetime.strptime(line, "%Y.%m.%d").date())
                            except ValueError:
                                continue
                    if (not NAME.fullmatch(name) or not AUTHOR_ROLE.search(bio) or not re.search(r"\bStripe\b", bio, re.I)
                            or not dates or max(dates) < date.today() - timedelta(days=730)):
                        return None
                    return dict(name=name, title=bio[:200], source=url, notes="",
                                reason="Stripe's author page identifies this person as an engineer. Verify location and current role.")
                except (httpx.HTTPError, ValueError, TypeError):
                    return None
        return [person for person in await asyncio.gather(*(author(url) for url in urls)) if person]


async def cloudflare_authors():
    """Read role-bearing author profiles linked from recent Cloudflare posts."""
    class AuthorLinks(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []

        def handle_starttag(self, tag, attrs):
            path = dict(attrs).get("href", "")
            if tag == "a" and re.fullmatch(r"/author/[a-z0-9-]+/", path) and path not in self.paths:
                self.paths.append(path)

    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(CLOUDFLARE_BLOG)
        response.raise_for_status()
        parser = AuthorLinks()
        parser.feed(response.text)
        limit = asyncio.Semaphore(4)

        async def author(path):
            async with limit:
                try:
                    response = await client.get(CLOUDFLARE_BLOG.rstrip("/") + path)
                    response.raise_for_status()
                    lines = clean(response.text).splitlines()
                    if not lines or not lines[0].endswith(" - Cloudflare Blog"):
                        return None
                    name = lines[0].removesuffix(" - Cloudflare Blog").strip()
                    heading = next((i for i, line in enumerate(lines[1:], 1) if line == name), None)
                    if not NAME.fullmatch(name) or heading is None or heading + 1 >= len(lines):
                        return None
                    role = lines[heading + 1]
                    if len(role) > 200 or not AUTHOR_ROLE.search(role):
                        return None
                    return dict(name=name, title=role, source=CLOUDFLARE_BLOG.rstrip("/") + path, notes="",
                                reason="Cloudflare's blog lists this engineering role. Verify current employment and location.")
                except (httpx.HTTPError, ValueError, TypeError):
                    return None
        return [person for person in await asyncio.gather(*(author(path) for path in parser.paths[:16])) if person]


async def find_people(store, job):
    query, specialty = search_terms(job)
    saved = store.all("SELECT * FROM contacts WHERE company_id=? ORDER BY id DESC", (job["company_id"],))
    saved.sort(key=lambda person: specialty.split()[0] not in person["title"].lower())
    for person in saved:
        person["reason"] = "A contact you saved for this company."
    people = saved[:3]
    status = "saved"
    key = brave_key()
    if len(people) < 3:
        if key:
            try:
                async with httpx.AsyncClient(timeout=12) as client:
                    response = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": f'{query} (bio OR speaker OR engineer) -site:linkedin.com', "count": 15},
                        headers={"X-Subscription-Token": key, "Accept": "application/json"},
                    )
                    response.raise_for_status()
                seen = {person["name"].casefold() for person in people}
                candidates = public_candidates(response.json().get("web", {}).get("results", []), job["company"], specialty)
                for person in rank_candidates(job, candidates):
                    if person["name"].casefold() not in seen and len(people) < 3:
                        people.append(person)
                        seen.add(person["name"].casefold())
                status = "searched"
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                status = "unavailable"
        else:
            status = "needs_key"
    official_sources = {"stripe": stripe_authors, "cloudflare": cloudflare_authors}
    official_search = official_sources.get(job["company"].casefold())
    if len(people) < 3 and official_search:
        try:
            seen = {person["name"].casefold() for person in people}
            for person in rank_candidates(job, await official_search()):
                if person["name"].casefold() not in seen and len(people) < 3:
                    people.append(person)
                    seen.add(person["name"].casefold())
                    if status == "needs_key":
                        status = "official"
        except (httpx.HTTPError, ElementTree.ParseError, ValueError):
            pass
    return {"people": people, "query": query, "status": status}


async def rewrite(text):
    key = os.getenv("SCOUT_OPENAI_KEY")
    model = os.getenv("SCOUT_OPENAI_MODEL")
    if not (key and model):
        raise ValueError("Set SCOUT_OPENAI_KEY and SCOUT_OPENAI_MODEL before using a model rewrite.")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "store": False, "max_output_tokens": 350,
                  "instructions": "Rewrite the supplied referral draft concisely. Preserve all facts, names and links. Do not add claims. Return only the message.",
                  "input": text},
        )
        response.raise_for_status()
    result = "".join(item.get("text", "") for output in response.json().get("output", [])
                     for item in output.get("content", []) if item.get("type") == "output_text").strip()
    if not result:
        raise ValueError("The model did not return a draft.")
    return result
