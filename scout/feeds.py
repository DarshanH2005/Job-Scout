"""Read public employer job feeds and rank titles against the pilot preferences."""

import asyncio
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import httpx

PROVIDERS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{board}/postings",
}
REGIONS = {
    "india_remote": "Remote from India",
    "india": "India-based",
    "us_remote": "U.S. remote",
    "unclear": "Eligibility unclear",
    "other": "Other location",
}
INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|gurugram|gurgaon|pune|mumbai|chennai|noida|delhi|kolkata|ahmedabad)\b", re.I)
US = re.compile(r"\b(?:(?i:united states|north america|us-only)|U\.?S\.?A?\.?)\b")
WORLDWIDE = re.compile(r"\b(worldwide|work from anywhere in the world|globally remote|remote globally|remote anywhere in the world)\b", re.I)
REMOTE = re.compile(r"\b(remote|work from home|distributed)\b", re.I)
REGIONAL_TEXT = re.compile(r"\b(remote|eligible|authorized|reside|based|locations?|anywhere|work from)\b", re.I)
RESTRICTION = re.compile(r"\b(only|must|within|eligible|authorized|based|limited|available)\b", re.I)
US_REMOTE = re.compile(r"\b(?:remote\s*[-–—,:(/|]*\s*(?:us|usa|united states)\b|(?:us|usa|united states)\s*[-–—,:(/|]*\s*remote\b)", re.I)
ROLE = re.compile(
    r"\b(?:software|full[ -]?stack|front[ -]?end|back[ -]?end|platform|data|analytics|"
    r"machine learning|ml|ai|applied ai|infrastructure|site reliability|devops|"
    r"solutions|security|quality|automation|systems?|cloud) (?:development |test )?"
    r"(?:engineer(?:ing)?|develop(?:er|ment)|scientist)\b|\b(?:"
    r"software developer|developer|data scientist|data analyst|mlops|sde|swe|sre|research engineer)\b", re.I
)
EARLY = re.compile(r"\b(intern(?:ship)?|new grad(?:uate)?|early career|university grad(?:uate)?|entry[ -]level|junior|associate|graduate|apprentice|level 1|engineer i|sde[- ]?1)\b", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|intermediate|mid[ -]level|staff|principal|manager|director|head|lead|architect|distinguished|vp|engineer (?:iii|iv|v)|sde[- ]?[345])\b", re.I)
YEARS = re.compile(
    r"(?<![\d.])(?:[3-9]|[1-3]\d)\s*(?:[-–]\s*\d{1,2}|\+|or more)?\s*years?\s+"
    r"(?:of\s+(?:[^\n.!?;:]{0,75}\s+)?experience\b|(?:working|building|developing|designing|managing)\b)", re.I
)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "li", "div", "br", "section", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "li", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(value):
    value = str(value or "")
    if "<" in value or "&lt;" in value:
        parser = PlainText()
        parser.feed(html.unescape(value))
        value = "".join(parser.parts)
    else:
        value = html.unescape(value)
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


def public_url(value):
    value = str(value or "").strip()
    try:
        parts = urlsplit(value)
        if parts.scheme in ("https", "http") and parts.hostname and not (parts.username or parts.password):
            return value
    except ValueError:
        pass
    return ""


def canonical_url(value):
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    tracking = {"gh_src", "lever-source", "source", "ref", "referrer"}
    query = "&".join(
        item for item in parts.query.split("&") if item and not
        (item.split("=")[0].lower().startswith("utm_") or item.split("=")[0].lower() in tracking)
    )
    host = "job-boards.greenhouse.io" if parts.netloc.lower() == "boards.greenhouse.io" else parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def board_from_job_url(value):
    parts = urlsplit(value)
    host = parts.hostname
    path = parts.path.strip("/").split("/")
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and len(path) >= 3 and path[1] == "jobs":
        return "greenhouse", path[0], path[2]
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and parts.path == "/embed/job_app":
        query = parse_qs(parts.query)
        if query.get("for") and query.get("token"):
            return "greenhouse", query["for"][0], query["token"][0]
    if host in ("jobs.lever.co", "jobs.ashbyhq.com") and len(path) >= 2 and all(path[:2]):
        return ("lever" if host == "jobs.lever.co" else "ashby"), path[0], path[1]
    if host == "jobs.smartrecruiters.com" and len(path) >= 2:
        match = re.match(r"(\d+)(?:-|$)", path[1])
        if match:
            return "smartrecruiters", path[0], match.group(1)
    return None


def geography(location, workplace, description):
    source = f"{location} · {workplace}" if workplace else location
    remote = bool(REMOTE.search(source))
    if INDIA.search(location):
        india_remote = bool(REMOTE.search(workplace)) or any(
            INDIA.search(part) and REMOTE.search(part) for part in re.split(r"[;|]", location)
        )
        return ("india_remote" if india_remote else "india"), source
    if WORLDWIDE.search(location):
        return "india_remote", source
    if US_REMOTE.search(source):
        return "us_remote", source

    lines = re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", description[:18000])
    candidates = [line for line in lines if len(line) < 420 and REGIONAL_TEXT.search(line)]
    us_only = next((line for line in candidates if US.search(line) and
                    (US_REMOTE.search(line) or (RESTRICTION.search(line) and (remote or REMOTE.search(line))))), None)
    if us_only:
        return "us_remote", us_only
    for line in candidates:
        if (WORLDWIDE.search(line) or INDIA.search(line)) and REMOTE.search(line):
            if remote or not location or re.fullmatch(r"remote|anywhere|global", location, re.I):
                return "india_remote", line
    if remote and US.search(location):
        return "us_remote", source
    if remote or not location:
        return "unclear", source or "No location was supplied by the employer."
    return "other", source


def assess(title, department, description, region):
    if not ROLE.search(title):
        if not (EARLY.search(title) and ROLE.search(department)):
            return 0, ""
    if SENIOR.search(title):
        return 0, ""
    if EARLY.search(title):
        return 90, "Relevant role with an early-career title"
    if YEARS.search(description[:16000]):
        return 0, ""
    return 55, "Relevant role. Check the experience requirements."


def normalize(provider, raw):
    if provider == "greenhouse":
        title = raw.get("title")
        location = (raw.get("location") or {}).get("name")
        department = ", ".join(x.get("name", "") for x in raw.get("departments") or [])
        url, description = raw.get("absolute_url"), raw.get("content")
        posted, workplace = raw.get("first_published"), ""
    elif provider == "lever":
        categories = raw.get("categories") or {}
        title = raw.get("text")
        location = ", ".join(dict.fromkeys(filter(None, [categories.get("location"), *(categories.get("allLocations") or [])])))
        department = categories.get("team") or categories.get("department")
        url, description = raw.get("hostedUrl") or raw.get("applyUrl"), raw.get("descriptionPlain") or raw.get("description")
        if raw.get("lists"):
            description = "\n".join(filter(None, [description, *(item.get("content") for item in raw["lists"])]))
        posted, workplace = None, raw.get("workplaceType")
    elif provider == "smartrecruiters":
        title = raw.get("name")
        department = (raw.get("department") or {}).get("label") or (raw.get("function") or {}).get("label")
        place = raw.get("location") or {}
        location = place.get("fullLocation") or ", ".join(filter(None, [place.get("city"), place.get("region"), place.get("country")]))
        workplace = "Hybrid" if place.get("hybrid") else "Remote" if place.get("remote") else ""
        posted = raw.get("releasedDate")
        description = ""
        company = (raw.get("company") or {}).get("identifier")
        posting = raw.get("id")
        url = f"https://jobs.smartrecruiters.com/{quote(company, safe='')}/{quote(str(posting), safe='')}" if company and posting else ""
    else:
        title, department = raw.get("title"), raw.get("team") or raw.get("department")
        location = ", ".join(dict.fromkeys(filter(None, [raw.get("location"), *(x.get("location") for x in raw.get("secondaryLocations") or [])])))
        url, description = raw.get("jobUrl") or raw.get("applyUrl"), raw.get("descriptionPlain") or raw.get("descriptionHtml")
        posted, workplace = raw.get("publishedAt"), raw.get("workplaceType") or ("Remote" if raw.get("isRemote") else "")
        if raw.get("isListed") is False:
            return None
    if not public_url(url) or not title or raw.get("id") is None:
        return None
    job = build_job(str(raw["id"]), title, department, location, workplace, description, url, posted, raw.get("applyUrl"))
    if provider == "smartrecruiters" and (raw.get("experienceLevel") or {}).get("id") in {"mid_senior_level", "director", "executive"}:
        job.update(score=0, reason="")
    return job


def build_job(external_id, title, department, location, workplace, description, url, posted=None, apply_url=None):
    title, department, location = clean(title), clean(department), clean(location)
    description = clean(description)[:30000]
    region, evidence = geography(location, clean(workplace), description)
    score, reason = assess(title, department, description, region)
    return dict(external_id=external_id, title=title[:220], department=department[:250],
                location=location[:350], description=description, url=url, apply_url=public_url(apply_url) or url,
                canonical_url=canonical_url(url), posted_at=posted, region=region,
                evidence=evidence[:500], score=score, reason=reason)


async def fetch_board(client, company, limit):
    provider = company["provider"]
    url = PROVIDERS[provider].format(board=quote(company["board"], safe=""))
    async with limit:
        try:
            if provider == "smartrecruiters":
                rows = await smartrecruiters_postings(client, url)
            else:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                rows = payload if provider == "lever" else payload["jobs"]
            if not isinstance(rows, list):
                raise ValueError("The provider returned an unexpected feed format.")
            jobs = [job for row in rows if (job := normalize(provider, row))]
            if rows and not jobs and not (provider == "ashby" and all(row.get("isListed") is False for row in rows)):
                raise ValueError("The feed contained no readable postings.")
            return jobs, None
        except httpx.HTTPStatusError as exc:
            return [], f"Employer feed returned HTTP {exc.response.status_code}."
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return [], "Employer feed could not be read."
        finally:
            await asyncio.sleep(0.15)


async def smartrecruiters_postings(client, url):
    rows = []
    limit = 100
    while True:
        response = await client.get(url, params={"limit": limit, "offset": len(rows)})
        response.raise_for_status()
        page = response.json()
        postings, total = page["content"], page["totalFound"]
        if not isinstance(postings, list) or not isinstance(total, int) or not 0 <= total <= 20000:
            raise ValueError("Unexpected SmartRecruiters page.")
        if not postings and len(rows) < total:
            raise ValueError("Incomplete SmartRecruiters snapshot.")
        rows.extend(postings)
        if len(rows) >= total:
            if len({row["id"] for row in rows}) != len(rows):
                raise ValueError("SmartRecruiters results shifted during pagination.")
            return rows


