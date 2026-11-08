"""Read public employer job feeds and rank titles against the pilot preferences."""

import asyncio
import html
import re
from datetime import date, datetime
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
FULL_TIME_OFFICE = re.compile(
    r"\b(?:work|working|be)\b[^.!?\n]{0,90}\boffice\b[^.!?\n]{0,48}\b(?:5|five)\s+days\s*(?:/|a\s+|per\s+)week\b", re.I
)
GENERAL_REMOTE_POLICY = re.compile(r"\bfor\s+(?:(?:us|u\.s\.)[- ]based\s+)?roles\s+open\s+to\s+remote\s+work\b", re.I)
ALL_ROLES_REMOTE = re.compile(r"^\W*all (?:of )?our (?:roles|positions) are remote\b", re.I)
RESTRICTION = re.compile(r"\b(only|must|within|eligible|authorized|based|limited|available)\b", re.I)
TIME_ZONE_REQUIREMENT = re.compile(
    r"\b(?:must|require[ds]?|only)\b[^.!?\n]{0,100}\b(?:located|based|reside|live|within|in)\b"
    r"[^.!?\n]{0,100}\b(?:time[ -]?zones?|(?:PST|PDT|MST|MDT|CST|CDT|EST|EDT|IST)\b)", re.I
)
US_REMOTE = re.compile(r"\b(?:remote\s*[-–—,:(/|]*\s*(?:us|usa|united states)\b|(?:us|usa|united states)\s*[-–—,:(/|]*\s*remote\b)", re.I)
ROLE = re.compile(
    r"\b(?:software|full[ -]?stack|front[ -]?end|back[ -]?end|platform|data|analytics|"
    r"machine learning|ml|ai|applied ai|infrastructure|site reliability|devops|"
    r"solutions|security|quality|automation|systems?|cloud) (?:development |test )?"
    r"(?:engineer(?:ing)?|develop(?:er|ment)|scientist)\b|\b(?:"
    r"software developer|developer|data scientist|data analyst|mlops|sde|swe|sre|research engineer)\b", re.I
)
EARLY = re.compile(r"\b(intern(?:ship)?|new grad(?:uate)?|early career|university grad(?:uate)?|entry[ -]level|junior|associate|graduate|apprentice|level 1|engineer (?:i|1)|sde[- ]?1)\b", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|intermediate|mid[ -]level|staff|principal|manager|director|head|lead|architect|distinguished|vp|engineer (?:iii|iv|v|[3-5])|sde[- ]?[345])\b", re.I)
EARLY_SCORE = 90
YEARS = re.compile(
    r"(?<![\d.])\b(?P<minimum>\d{1,2})\s*(?:(?:[-–]|to)\s*\d{1,2}|\+|or more)?\s*years?\s+"
    r"(?:of\s+(?:[^\n.!?;:]{0,75}\s+)?experience\b|experience\b|(?:working|building|developing|designing|managing)\b)", re.I
)
EXPERIENCE_MONTHS = re.compile(r"\b\d{1,2}\s*\+?\s*months?\s+of\s+(?:professional\s+|relevant\s+|industry\s+)?experience\b", re.I)
EDUCATION = re.compile(
    r"\b(?:(?:currently|must be) (?:pursuing|enrolled(?: in)?) [^.!?;]{0,65}\b(?:degree|program|college|university)"
    r"|\d+\s+years? of (?:university|college) education)\b", re.I
)
DEGREE = re.compile(r"\b(?:degree|graduat\w*)\b", re.I)
GRAD_DATE = re.compile(r"\b(?:by|between|before|after|in|through|from|expected|class of)\b[^.!?;\n]{0,36}\b20\d{2}\b", re.I)
MINIMUM_HEADING = re.compile(r"(?:minimum|basic|required) (?:requirements|qualifications|experience|skills)|requirements|required|must have(?: (?:skills|qualifications|requirements))?", re.I)
PREFERRED_HEADING = re.compile(r"preferred(?: (?:requirements|qualifications|experience|skills))?|nice to have|bonus points", re.I)
OTHER_HEADING = re.compile(r"(?:about (?:us|the role|the team)|responsibilities|benefits|compensation|in-office expectations|what (?:you.ll do|we offer|we.re looking for)|equal opportunity)", re.I)
COMPANY_BACKGROUND = re.compile(r"^(?:we|our (?:company|team)|the company) (?:have|has|bring|brings|offer|offers)\b", re.I)
APPLICATION_DEADLINE = re.compile(
    r"\b(?:application deadline|applications? clos(?:e|es)|apply by|closing date)\b[^.!?\n]{0,75}"
    r"(?:\b20\d{2}-\d{2}-\d{2}\b|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}\b)", re.I
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


def required_timezone(description):
    lines = re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", description[:18000])
    return next((line.strip() for line in lines if len(line) < 300 and TIME_ZONE_REQUIREMENT.search(line)), "")


def geography(location, workplace, description):
    source = f"{location} · {workplace}" if workplace else location
    remote = bool(REMOTE.search(source))
    lines = re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", description[:18000])
    timezone = required_timezone(description)

    def evidence_with_timezone(value):
        if timezone and timezone.casefold() not in value.casefold():
            return f"{value} · {timezone}" if value else timezone
        return value

    office = next((line for line in lines if len(line) < 420 and FULL_TIME_OFFICE.search(line)), None)
    if office:
        return ("india" if INDIA.search(location) or INDIA.search(office) else "other"), evidence_with_timezone(office)
    if INDIA.search(location):
        india_remote = bool(REMOTE.search(workplace)) or any(
            INDIA.search(part) and REMOTE.search(part) for part in re.split(r"[;|]", location)
        )
        if not india_remote and not re.search(r"\b(hybrid|on[- ]?site|in[- ]office)\b", source, re.I):
            policy = next((line for line in lines if len(line) < 420 and ALL_ROLES_REMOTE.search(line)), None)
            if policy:
                return "india_remote", evidence_with_timezone(f"{location} · {policy}")
        return ("india_remote" if india_remote else "india"), evidence_with_timezone(source)
    if WORLDWIDE.search(location):
        return "india_remote", evidence_with_timezone(source)
    if US_REMOTE.search(source):
        return "us_remote", evidence_with_timezone(source)

    candidates = [line for line in lines if len(line) < 420 and REGIONAL_TEXT.search(line)
                  and not GENERAL_REMOTE_POLICY.search(line)]
    us_only = next((line for line in candidates if US.search(line) and
                    (US_REMOTE.search(line) or (RESTRICTION.search(line) and (remote or REMOTE.search(line))))), None)
    if us_only:
        return "us_remote", evidence_with_timezone(us_only)
    for line in candidates:
        if (WORLDWIDE.search(line) or INDIA.search(line)) and REMOTE.search(line):
            if remote or not location or re.fullmatch(r"remote|anywhere|global", location, re.I):
                return "india_remote", evidence_with_timezone(line)
    if remote and US.search(location):
        return "us_remote", evidence_with_timezone(source)
    if remote or not location:
        return "unclear", evidence_with_timezone(source or "No location was supplied by the employer.")
    return "other", evidence_with_timezone(source)


def assess(title, department, description, region):
    if not ROLE.search(title):
        if not (EARLY.search(title) and ROLE.search(department)):
            return 0, ""
    if SENIOR.search(title):
        return 0, ""
    experience = []
    for evidence in eligibility_evidence(description, limit=None):
        excerpt = evidence["text"]
        if evidence["label"] in {"Preferred", "Application deadline"} or re.match(r"^(?:preferred|nice to have|bonus|ideally)\b", excerpt, re.I):
            continue
        for match in YEARS.finditer(excerpt):
            prefix = excerpt[max(0, match.start() - 30):match.start()]
            if not re.search(r"\b(?:up to|at most|no more than|less than|maximum of)\s*$", prefix, re.I):
                experience.append(int(match["minimum"]))
    if EARLY.search(title):
        if any(years >= 2 for years in experience):
            minimum = max(experience)
            return 55, f"Early-career title; posting mentions {minimum}+ years of experience. Check requirements."
        return EARLY_SCORE, "Relevant role with an early-career title"
    if any(years >= 3 for years in experience):
        return 0, ""
    return 55, "Relevant role. Check the experience requirements."


def eligibility_evidence(description, limit=4):
    """Keep explicit excerpts and the employer's qualification labels when present."""
    section, evidence, seen = "From the posting", [], set()
    for line in description.splitlines():
        heading = line.strip(" #*•●:\t")
        if MINIMUM_HEADING.fullmatch(heading):
            section = "Required"
            continue
        if PREFERRED_HEADING.fullmatch(heading):
            section = "Preferred"
            continue
        if OTHER_HEADING.fullmatch(heading):
            section = "From the posting"
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            excerpt = sentence.strip(" \t*•●")
            if not excerpt or len(excerpt) > 420 or excerpt in seen or COMPANY_BACKGROUND.search(excerpt):
                continue
            if APPLICATION_DEADLINE.search(excerpt):
                evidence.append({"label": "Application deadline", "text": excerpt})
                seen.add(excerpt)
            elif (YEARS.search(excerpt) or EXPERIENCE_MONTHS.search(excerpt) or EDUCATION.search(excerpt)
                    or (DEGREE.search(excerpt) and GRAD_DATE.search(excerpt))):
                evidence.append({"label": section, "text": excerpt})
                seen.add(excerpt)
    priority = {"Application deadline": 0, "Required": 1, "From the posting": 2, "Preferred": 3}
    return sorted(evidence, key=lambda item: priority[item["label"]])[:limit]


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
    if provider == "greenhouse":
        job["deadline_at"] = employer_deadline(raw.get("application_deadline"))
    if provider == "smartrecruiters" and (raw.get("experienceLevel") or {}).get("id") in {"mid_senior_level", "director", "executive"}:
        job.update(score=0, reason="")
    return job


def employer_deadline(value):
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        stamp = datetime.fromisoformat(value)
        return stamp.isoformat() if stamp.tzinfo else None
    except ValueError:
        return None


def build_job(external_id, title, department, location, workplace, description, url, posted=None, apply_url=None):
    title, department, location = clean(title), clean(department), clean(location)
    description = clean(description)[:30000]
    region, evidence = geography(location, clean(workplace), description)
    score, reason = assess(title, department, description, region)
    return dict(external_id=external_id, title=title[:220], department=department[:250],
                location=location[:350], description=description, url=url, apply_url=public_url(apply_url) or url,
                canonical_url=canonical_url(url), posted_at=posted, deadline_at=None, region=region,
                evidence=evidence[:500], score=score, reason=reason)


async def fetch_board(client, company, limit):
    provider = company["provider"]
    url = PROVIDERS[provider].format(board=quote(company["board"], safe=""))
    async with limit:
        try:
            if provider == "smartrecruiters":
                rows = await smartrecruiters_postings(client, url)
            else:
                payload = await fetch_json(client, url)
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


async def fetch_json(client, url, params=None):
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            retry_after = exc.response.headers.get("Retry-After")
            if attempt or exc.response.status_code not in {429, 502, 503, 504}:
                raise
            if retry_after and (not retry_after.isdecimal() or len(retry_after) > 2 or int(retry_after) > 4):
                raise
            await asyncio.sleep(int(retry_after) if retry_after else 0.4)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if attempt:
                raise
            await asyncio.sleep(0.4)


async def smartrecruiters_postings(client, url):
    rows = []
    limit = 100
    while True:
        page = await fetch_json(client, url, {"limit": limit, "offset": len(rows)})
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


