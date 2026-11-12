"""Public source search with conservative matching and an explicit fallback."""

import os
import re
import asyncio
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from .feeds import INDIA, clean, public_url
from .credentials import brave_key

NAME = re.compile(r"[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){1,3}", re.UNICODE)
TECH = re.compile(r"\b(engineer|engineering|developer|researcher|scientist|engineering manager)\b", re.I)
AUTHOR_ROLE = re.compile(r"\b(?:engineer|engineering manager|engineering lead|research scientist)\b", re.I)
CONTENT_TITLE = re.compile(r"\b(team|blog|engineering|software|platform|data|career|jobs|home|conference|speaker|meet|build|how|why|introducing|about)\b", re.I)
STRIPE_SITEMAP = "https://stripe.dev/sitemap.xml"
STRIPE_ENGINEERING = "https://stripe.dev/blog/topic/engineering"
CLOUDFLARE_BLOG = "https://blog.cloudflare.com/"
DATADOG_SPEAKERS = "https://dash.datadoghq.com/sessions/speakers/"
AFFIRM_FEED = "https://tech.affirm.com/feed"
ABNORMAL_STORIES = "https://abnormal.ai/careers/stories"
ABNORMAL_SITEMAP = "https://abnormal.ai/sitemap.xml"
CROSSREF_WORKS = "https://api.crossref.org/works"
CROSSREF_TOPIC = re.compile(r"\b(?:machine learning|deep learning|neural|language models?|artificial intelligence|generative ai|graph learning|gnns?|data science|data mining|fraud detection|computer vision)\b", re.I)
OFFICIAL_CACHE_SECONDS = 15 * 60
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
    if re.search(r"\bdata scien(?:ce|tist)\b", title):
        specialty = "data science"
    elif re.search(r"\b(data|analytics)\b", title):
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
        evidence = "cited paper" if person.get("title") == "AI / data science paper author" else "profile"
        explanation = (f"Job and {evidence} suggest a shared area in {', '.join(areas)}. " if areas else
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


async def datadog_speakers():
    """Use recent conference speaker cards with a stated Datadog engineering role."""
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(DATADOG_SPEAKERS)
        response.raise_for_status()
    lines = clean(response.content.decode("utf-8", errors="replace")).splitlines()
    heading = next((re.fullmatch(r"(20\d{2}) Speakers(?: \| DASH by Datadog)?", line) for line in lines
                    if re.fullmatch(r"(20\d{2}) Speakers(?: \| DASH by Datadog)?", line)), None)
    if not heading or not date.today().year - 2 <= int(heading[1]) <= date.today().year:
        return []
    year = heading[1]
    people, seen = [], set()
    for i, line in enumerate(lines[1:], 1):
        role = re.fullmatch(r"(.{1,180}?)\s*\|\s*Datadog", line, re.I)
        name = lines[i - 1]
        if not role or not NAME.fullmatch(name) or not AUTHOR_ROLE.search(role[1]) or name.casefold() in seen:
            continue
        bio = lines[i + 1] if i + 1 < len(lines) else ""
        if not re.search(r"\bDatadog\b", bio, re.I):
            bio = ""
        bio = re.split(r"(?<=[.!?])\s+", bio, maxsplit=1)[0][:350]
        people.append(dict(name=name, title=role[1], source=DATADOG_SPEAKERS, notes=bio,
                           reason=f"Datadog's {year} conference listed this engineering role. Verify current employment and location."))
        seen.add(name.casefold())
    return people


async def affirm_authors():
    """Read named authors and their stated technical teams from Affirm's public feed."""
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        response = await client.get(AFFIRM_FEED)
        response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    people, seen = [], set()
    today = datetime.now(timezone.utc).date()
    for item in root.findall("./channel/item")[:30]:
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", "")).date()
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
        if not today - timedelta(days=730) <= published <= today:
            continue
        link = urlsplit(item.findtext("link", ""))
        if link.scheme != "https" or link.hostname != "tech.affirm.com" or not link.path.strip("/"):
            continue
        source = urlunsplit(("https", "tech.affirm.com", link.path, "", ""))
        content = next((node.text or "" for node in item if node.tag.endswith("}encoded")), "")
        lines = clean(content).splitlines()
        byline = re.fullmatch(r"Authors?:\s*(.+),\s*Affirm", lines[0], re.I) if lines else None
        if not byline:
            continue
        parts = [part.strip() for part in byline[1].split(",")]
        if len(parts) < 2 or len(parts) > 9:
            continue
        team = parts[-1]
        if not re.search(r"\b(?:engineering|platform|infrastructure|developer productivity|data)\b", team, re.I):
            continue
        names = parts[:-1]
        if not all(NAME.fullmatch(name) and not CONTENT_TITLE.search(name) for name in names):
            continue
        title = clean(item.findtext("title", ""))[:200]
        for name in names:
            if name.casefold() in seen:
                continue
            people.append(dict(name=name, title=f"Author · {team}"[:200], source=source,
                               notes=f"Affirm tech blog: {title}"[:350],
                               reason=f"Affirm credited this person and team in a {published.isoformat()} post. Verify their current role and location."))
            seen.add(name.casefold())
    first_by_team, remaining, teams = [], [], set()
    for person in people:
        team = " ".join(person["title"].casefold().replace("&", "and").split())
        (remaining if team in teams else first_by_team).append(person)
        teams.add(team)
    return first_by_team + remaining


async def abnormal_engineers():
    """Find recent named engineering interviews on Abnormal AI's own site."""
    class StoryLinks(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []

        def handle_starttag(self, tag, attrs):
            path = dict(attrs).get("href", "")
            if tag == "a" and re.fullmatch(r"/careers/stories/[a-z0-9-]+/?", path) and path not in self.paths:
                self.paths.append(path)

    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        urls = []
        try:
            response = await client.get(ABNORMAL_STORIES)
            response.raise_for_status()
            parser = StoryLinks()
            parser.feed(response.text)
            urls.extend("https://abnormal.ai" + path for path in parser.paths[:8])
        except httpx.HTTPError:
            pass
        try:
            response = await client.get(ABNORMAL_SITEMAP)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            paths = [node.text for node in root.iter() if node.tag.endswith("}loc") and node.text
                     and re.fullmatch(r"https://abnormal\.ai/careers/stories/[a-z0-9-]+/?", node.text)]
            for index in range(min(5, len(paths))):
                urls.extend((paths[index], paths[-index - 1]))
        except (httpx.HTTPError, ElementTree.ParseError):
            pass
        urls = list(dict.fromkeys(urls))[:18]
        limit = asyncio.Semaphore(3)

        async def story(url):
            async with limit:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    lines = clean(response.text).splitlines()
                    if not lines or not re.fullmatch(r"Meet .+ \| Abnormal AI", lines[0]):
                        return None
                    name = lines[0][5:-len(" | Abnormal AI")].strip()
                    if not re.fullmatch(r"[A-Z][\w.'’\-]*(?:\s+[A-Z][\w.'’\-]*){1,3}", name):
                        return None
                    heading = next((i for i, line in enumerate(lines) if line == f"Meet {name}"), None)
                    if not heading or not re.fullmatch(r"Engineering\s*·\s*[A-Za-z]+\s+20\d{2}", lines[heading - 1]):
                        return None
                    published = datetime.strptime(lines[heading - 1].split("·")[1].strip(), "%B %Y").date()
                    if not date.today() - timedelta(days=730) <= published <= date.today():
                        return None
                    body = lines[heading + 1:heading + 18]
                    role = next((line for line in body if re.search(
                        r"\b(?:software|platform|backend|security|lead|systems?|data)\s+engineer\b|\bengineering manager\b", line, re.I)
                        and (name.casefold() in line.casefold() or re.search(r"\bI\s+(?:am|joined|work)\b", line, re.I))), None)
                    if not role:
                        return None
                    notes = " ".join(dict.fromkeys([body[0][:170], role[:180]]))[:350] if body else ""
                    return dict(name=name, title="Engineering career story", source=url, notes=notes,
                                reason=f"Abnormal AI published this engineering interview in {published:%B %Y}. Verify current employment and location.")
                except (httpx.HTTPError, ValueError, TypeError, IndexError):
                    return None
        return [person for person in await asyncio.gather(*(story(url) for url in urls)) if person]


async def crossref_authors(job):
    """Find named authors with an explicit employer affiliation in recent technical papers."""
    company = job["company"].strip()
    if len(company) < 4:
        return []
    today = date.today()
    async with httpx.AsyncClient(timeout=12, follow_redirects=False,
                                 headers={"User-Agent":"JobScout/1.0 (https://github.com/DarshanH2005/Job-Scout)"}) as client:
        response = await client.get(CROSSREF_WORKS, params={"query.affiliation":company,
            "query.bibliographic":"machine learning", "filter":f"from-pub-date:{today - timedelta(days=730)},until-pub-date:{today}",
            "rows":15})
        response.raise_for_status()
        items = response.json()["message"]["items"]
    if not isinstance(items, list):
        raise ValueError("Crossref returned an unexpected result.")
    people, seen = [], set()
    for work in items[:15]:
        try:
            if not isinstance(work, dict):
                continue
            title = clean((work.get("title") or [""])[0])
            doi = str(work.get("DOI") or "")
            parts = (work.get("published") or {}).get("date-parts", [[]])[0]
            published = date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1)
            if (not CROSSREF_TOPIC.search(title) or re.search(r"\b(?:retraction|retracted|withdrawn|erratum|corrigendum)\b", title, re.I)
                    or not re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", doi, re.I)
                    or not today - timedelta(days=730) <= published <= today):
                continue
            source = CROSSREF_WORKS + "/" + quote(doi, safe="")
            paper_url = "https://doi.org/" + quote(doi, safe="/;()_.-")
            for author in (work.get("author") or [])[:30]:
                if not isinstance(author, dict):
                    continue
                name = clean(" ".join((author.get("given") or "", author.get("family") or "")))
                affiliations = author.get("affiliation") or []
                if not isinstance(affiliations, list):
                    continue
                affiliation = next((clean(aff.get("name")) for aff in affiliations if isinstance(aff, dict)
                                    and re.search(r"(?<!\w)" + re.escape(company) + r"(?!\w)", clean(aff.get("name")), re.I)), "")
                if not NAME.fullmatch(name) or CONTENT_TITLE.search(name) or not affiliation or name.casefold() in seen:
                    continue
                people.append(dict(name=name, title="AI / data science paper author", source=source, paper_url=paper_url,
                                   notes=f"{affiliation[:140]} · {title[:190]}",
                                   reason=f"A {published.year} paper lists this person with {company}. Verify current role and location."))
                seen.add(name.casefold())
        except (ValueError, TypeError, IndexError, AttributeError):
            continue
    def diverse(rows):
        first, remaining, papers = [], [], set()
        for person in rows:
            (remaining if person["source"] in papers else first).append(person)
            papers.add(person["source"])
        return first + remaining
    if job.get("region") in {"india", "india_remote"}:
        india = [person for person in people if INDIA.search(person["notes"])]
        other = [person for person in people if not INDIA.search(person["notes"])]
        return diverse(india) + diverse(other)
    return diverse(people)


class ResearchCache:
    def __init__(self):
        self.entries = {}
        self.locks = {company: asyncio.Lock() for company in ("stripe", "cloudflare", "datadog", "affirm", "abnormal ai")}
        self.crossref_lock = asyncio.Lock()
        self.crossref_next = 0.0

    async def authors(self, company, search):
        async with self.locks.setdefault(company, asyncio.Lock()):
            cached = self.entries.get(company)
            if cached and cached[0] > time.monotonic():
                return cached[1], True
            people = await search()
            if people:
                self.entries[company] = (time.monotonic() + OFFICIAL_CACHE_SECONDS, people)
            return people, False

    async def publications(self, key, search):
        # Crossref's public pool allows one concurrent request and five per second.
        async with self.crossref_lock:
            cached = self.entries.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1], True
            wait = self.crossref_next - time.monotonic()
            if wait > 1:
                return [], False  # A recent rate limit or outage is still cooling down.
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                result = await self.authors(key, search)
                if not result[0]:
                    self.entries[key] = (time.monotonic() + 300, [])
                return result
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    self.crossref_next = time.monotonic() + 30
                raise
            finally:
                self.crossref_next = max(self.crossref_next, time.monotonic() + .25)


async def find_people(store, job, cache=None):
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
    official_sources = {"stripe": stripe_authors, "cloudflare": cloudflare_authors,
                        "datadog": datadog_speakers, "affirm": affirm_authors, "abnormal ai": abnormal_engineers}
    company = job["company"].casefold()
    official_search = official_sources.get(company)
    official_cached = False
    if len(people) < 3 and official_search:
        try:
            seen = {person["name"].casefold() for person in people}
            if cache:
                authors, official_cached = await cache.authors(company, official_search)
            else:
                authors = await official_search()
            for person in rank_candidates(job, authors):
                if person["name"].casefold() not in seen and len(people) < 3:
                    people.append(person)
                    seen.add(person["name"].casefold())
                    if status == "needs_key":
                        status = "official"
        except (httpx.HTTPError, ElementTree.ParseError, ValueError):
            pass
    publication_cached = False
    if len(people) < 3 and specialty in {"data science", "machine learning"}:
        try:
            key = f"crossref:{company}:{specialty}:{job.get('region', '')}"
            if cache:
                authors, publication_cached = await cache.publications(key, lambda: crossref_authors(job))
            else:
                authors = await crossref_authors(job)
            seen = {person["name"].casefold() for person in people}
            for person in rank_candidates(job, authors):
                if person["name"].casefold() not in seen and len(people) < 3:
                    people.append(person)
                    seen.add(person["name"].casefold())
                    if status in {"needs_key", "unavailable"}:
                        status = "publications"
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            pass
    for person in people:
        if person.get("id"):
            person["prior_conversations"] = store.contact_conversations(person["id"], exclude_job_id=job["id"])
    return {"people": people, "query": query, "status": status, "official_cached": official_cached,
            "publication_cached": publication_cached}


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
