"""Read public employer job feeds and rank titles against the pilot preferences."""

import asyncio
import html
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import httpx

PROVIDERS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{board}/postings",
    "workday": "workday",
}
REGIONS = {
    "india_remote": "Remote from India",
    "india": "India-based",
    "us_remote": "U.S. remote",
    "unclear": "Eligibility unclear",
    "other": "Other location",
}


class WorkdaySnapshotError(ValueError):
    """A Workday response cannot be treated as a complete source snapshot."""


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
EARLY = re.compile(r"\b(intern(?:ship)?|new grad(?:uate)?|early career|university grad(?:uate)?|entry[ -]level|junior|associate|graduate|apprentice|level 1|(?:engineer|scientist|analyst) (?:i|1)|sde[- ]?1)\b", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|intermediate|mid[ -]level|staff|principal|manager|mgr\.?|director|head|lead|architect|distinguished|vp|smts|lmts|pmts|(?:engineer|scientist|analyst) (?:iii|iv|v|[3-5])|sde[- ]?[345])\b", re.I)
SENIOR_DESCRIPTION = re.compile(
    r"\b(?:we(?:'|’)re|we are)\s+(?:looking for|seeking|hiring)\s+(?:an?\s+)?(?:experienced\s+)?"
    r"(?:technical lead\b|(?:senior|sr\.?|staff|principal|lead|engineering manager|director)\b"
    r"[^.!?\n]{0,90}?\b(?:engineer(?:ing)?|developer|scientist|analyst|manager|lead)\b)", re.I
)
EARLY_SCORE = 90
YEARS = re.compile(
    r"(?<![\d.])\b(?P<minimum>\d{1,2})\s*(?:(?:[-–]|to)\s*\d{1,2}|\+|or more)?\s*years?\s+"
    r"(?:of\s+(?:[^\n.!?;:]{0,75}\s+)?experience\b|experience\b|(?:working|building|developing|designing|managing)\b)", re.I
)
YEARS_IN_ROLE = re.compile(
    r"(?<![\d.])\b(?P<minimum>\d{1,2})\s*(?:(?:[-–]|to)\s*\d{1,2}|\+|or more)?\s*years?\s+in\s+"
    r"(?:a[n]?\s+)?(?:[\w&/-]+[ ,/-]+){0,5}?(?:engineering|operations|technology|development|support|"
    r"analytics|programming|software|security|data|design|testing|research)\b", re.I
)
DEGREE_EXPERIENCE = re.compile(r"\b(?:bachelor'?s?|master'?s?|ph\.?d\.?)\s+with\s+(\d{1,2})\s*\+?", re.I)
EXPERIENCE_MONTHS = re.compile(r"\b\d{1,2}\s*\+?\s*months?\s+of\s+(?:professional\s+|relevant\s+|industry\s+)?experience\b", re.I)
EDUCATION = re.compile(
    r"\b(?:(?:currently|must be) (?:pursuing|enrolled(?: in)?) [^.!?;]{0,65}\b(?:degree|program|college|university)"
    r"|\d+\s+years? of (?:university|college) education)\b", re.I
)
DEGREE = re.compile(r"\b(?:degree|graduat\w*)\b", re.I)
ACADEMIC_DEGREE = re.compile(r"\b(?:bachelor(?:['’]s)?|master(?:['’]s)?|doctoral|ph\.?d\.?|b\.?tech|m\.?tech)\s+degree\b", re.I)
GRAD_DATE = re.compile(r"\b(?:by|between|before|after|in|through|from|expected|class of)\b[^.!?;\n]{0,36}\b20\d{2}\b", re.I)
GRAD_YEAR_MENTION = re.compile(r"\b(?:graduat\w*|class of)\b[^.!?;\n]{0,140}\b20\d{2}\b", re.I)
MINIMUM_HEADING = re.compile(r"(?:minimum|basic|required) (?:requirements|qualifications|experience|skills)|requirements|required|must have(?: (?:skills|qualifications|requirements))?", re.I)
PREFERRED_HEADING = re.compile(r"preferred(?: (?:requirements|qualifications|experience|skills))?|nice to have|bonus points", re.I)
OTHER_HEADING = re.compile(r"(?:about (?:us|the role|the team)|responsibilities|benefits|compensation|in-office expectations|what (?:you.ll do|we offer|we.re looking for)|equal opportunity)", re.I)
COMPANY_BACKGROUND = re.compile(r"^(?:we|our (?:company|team)|the company) (?:have|has|bring|brings|offer|offers)\b", re.I)
APPLICATION_DEADLINE = re.compile(
    r"\b(?:application deadline|applications? clos(?:e|es)|apply by|closing date)\b[^.!?\n]{0,75}"
    r"(?:\b20\d{2}-\d{2}-\d{2}\b|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}\b)", re.I
)
WORK_AUTHORIZATION = re.compile(
    r"\b(?:work authori[sz]ation|authori[sz]ation to work|authori[sz]ed to work|eligible to work|"
    r"right to work|work permits?|visa sponsorship|immigration sponsorship)\b"
    r"|\bsponsor(?:s|ing|ship)?\b[^\n]{0,90}\b(?:visas?|immigration|international candidates|work authori[sz]ation)\b"
    r"|\b(?:visas?|immigration)\b[^\n]{0,90}\bsponsor(?:s|ed|ing|ship)?\b"
    r"|\b(?:we|employer|company)\s+(?:do(?:es)? not|cannot|will not|do(?:es)?|will|may)\s+sponsor\s+(?:any\s+)?(?:applicants|candidates|employees)\b",
    re.I,
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


def workday_base(board):
    match = re.fullmatch(r"([A-Za-z0-9_-]+)\.(wd[0-9]+)\.([A-Za-z0-9_-]+)", board)
    if not match:
        raise ValueError("Use a Workday identifier like adobe.wd5.external_experienced.")
    tenant, shard, site = match.groups()
    host = f"https://{tenant}.{shard}.myworkdayjobs.com"
    return f"{host}/wday/cxs/{tenant}/{site}", f"{host}/{site}"


def workday_path(value):
    if not isinstance(value, str) or not value.startswith("/job/"):
        raise ValueError("Invalid Workday posting path.")
    path = urlsplit(value)
    if path.scheme or path.netloc or path.query or path.fragment or any(part in {".", ".."} for part in unquote(path.path).split("/")):
        raise ValueError("Invalid Workday posting path.")
    if not re.fullmatch(r"/job/[A-Za-z0-9_~.%()!,$'@+\-/]+", value):
        raise ValueError("Invalid Workday posting path.")
    return value


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
    if re.fullmatch(r"\d+\s+locations?", location, re.I):
        return "unclear", evidence_with_timezone(source)
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
    if SENIOR_DESCRIPTION.search(description):
        return 0, "The posting describes a senior or lead position."
    experience = []
    for evidence in eligibility_evidence(description, limit=None):
        excerpt = evidence["text"]
        if evidence["label"] in {"Preferred", "Application deadline"} or re.match(r"^(?:preferred|nice to have|bonus|ideally)\b", excerpt, re.I):
            continue
        experience.extend(experience_minima(excerpt))
    if EARLY.search(title):
        if any(years >= 2 for years in experience):
            minimum = max(experience)
            return 55, f"Early-career title; posting mentions {minimum}+ years of experience. Check requirements."
        return EARLY_SCORE, "Relevant role with an early-career title"
    if any(years >= 3 for years in experience):
        return 0, ""
    return 55, "Relevant role. Check the experience requirements."


def experience_minima(excerpt):
    """Find explicit minimums; degree alternatives use the least demanding option."""
    degree_options = DEGREE_EXPERIENCE.findall(excerpt)
    if len(degree_options) >= 2 and re.search(r"\byears?\b[^.\n]{0,100}\bexperience\b", excerpt, re.I):
        return [min(map(int, degree_options))]
    minima = []
    for pattern in (YEARS, YEARS_IN_ROLE):
        for match in pattern.finditer(excerpt):
            prefix = excerpt[max(0, match.start() - 30):match.start()]
            if not re.search(r"\b(?:up to|at most|no more than|less than|maximum of)\s*$", prefix, re.I):
                minima.append(int(match["minimum"]))
    return minima


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
            elif (YEARS.search(excerpt) or YEARS_IN_ROLE.search(excerpt) or experience_minima(excerpt)
                    or EXPERIENCE_MONTHS.search(excerpt) or EDUCATION.search(excerpt) or ACADEMIC_DEGREE.search(excerpt)
                    or (DEGREE.search(excerpt) and GRAD_DATE.search(excerpt)) or GRAD_YEAR_MENTION.search(excerpt)):
                evidence.append({"label": section, "text": excerpt})
                seen.add(excerpt)
    priority = {"Application deadline": 0, "Required": 1, "From the posting": 2, "Preferred": 3}
    return sorted(evidence, key=lambda item: priority[item["label"]])[:limit]


def graduation_warning(description, graduation_year):
    """Flag explicit year differences without deciding whether the user qualifies."""
    if not graduation_year or not GRAD_YEAR_MENTION.search(description):
        return None
    mentions = []
    for item in eligibility_evidence(description, limit=None):
        excerpt = item["text"]
        if item["label"] in {"Preferred", "Application deadline"} or re.match(r"^\W*(?:preferred|nice to have|bonus|ideally)\b", excerpt, re.I):
            continue
        match = GRAD_YEAR_MENTION.search(excerpt)
        if not match:
            continue
        clause = match.group()
        if re.search(r"\b(?:before|after|by|prior|earlier|later|not|without|unless|any year|regardless)\b", excerpt, re.I):
            return None
        years = [int(value) for value in re.findall(r"\b20\d{2}\b", clause)]
        if not years:
            continue
        is_range = bool(re.search(r"\bbetween\b", clause, re.I) or
                        re.search(r"\b20\d{2}\s*(?:-|–|—|to|through)\s*(?:[A-Za-z]+\s+)?20\d{2}\b", clause, re.I))
        if graduation_year in years or (is_range and min(years) <= graduation_year <= max(years)):
            return None
        mentions.append(excerpt)
    return {"saved_year": graduation_year, "excerpt": mentions[0]} if mentions else None


def work_authorization_evidence(description, limit=3):
    """Keep the employer's paragraph intact, including any sponsorship caveats."""
    excerpts, seen = [], set()
    for line in description.splitlines():
        excerpt = line.strip(" \t*•●")
        if not excerpt or len(excerpt) > 1800 or excerpt in seen or not WORK_AUTHORIZATION.search(excerpt):
            continue
        excerpts.append(excerpt)
        seen.add(excerpt)
        if len(excerpts) >= limit:
            break
    return excerpts


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


def normalize_workday_rows(rows, public_base):
    jobs = []
    for row in rows:
        path = workday_path(row["externalPath"])
        title = clean(row.get("title"))
        remote = "Remote" if str(row.get("remoteType", "")).casefold() == "remote" else ""
        job = build_job(path, title, "", row.get("locationsText"), remote, "", public_base + path)
        if not title:
            job["_missing_title"] = True
        jobs.append(job)
    return jobs


def normalize_rows(provider, rows):
    return [job for row in rows if (job := normalize(provider, row))]


def feed_failure(exc):
    """Label an employer-side HTTP error so a temporary edge outage does not read as a retired board."""
    status = exc.response.status_code
    if status == 429 or status >= 500:
        return f"Employer feed temporarily unavailable (HTTP {status})."
    return f"Employer feed returned HTTP {status}."


async def fetch_board(client, company, limit):
    provider = company["provider"]
    async with limit:
        try:
            if provider == "workday":
                base, public_base = workday_base(company["board"])
                rows = await workday_postings(client, base)
                jobs = await asyncio.to_thread(normalize_workday_rows, rows, public_base)
                # Resolve ambiguous early-career locations first, then relevant India listings.
                ambiguous = [i for i, job in enumerate(jobs) if job["score"] >= EARLY_SCORE
                             and re.fullmatch(r"\d+\s+locations?", job["location"], re.I)]
                india = [i for i, job in enumerate(jobs) if job["score"] > 0
                         and job["region"] in {"india", "india_remote"}]
                candidates = list(dict.fromkeys(ambiguous + india))[:20]
                detail_limit = asyncio.Semaphore(2)

                async def enrich(index):
                    async with detail_limit:
                        try:
                            original = jobs[index]
                            description, location, workplace = await workday_detail(client, company["board"], original["external_id"])
                            if not workplace and original["evidence"] == f'{original["location"]} · Remote':
                                workplace = "Remote"
                            jobs[index] = await asyncio.to_thread(build_job, original["external_id"], original["title"], original["department"],
                                                                  location or original["location"], workplace, description, original["url"])
                        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                            pass  # Keep the honest, unclear summary; opening the role can retry the detail.
                await asyncio.gather(*(enrich(index) for index in candidates))
                return jobs, None
            url = PROVIDERS[provider].format(board=quote(company["board"], safe=""))
            if provider == "smartrecruiters":
                rows = await smartrecruiters_postings(client, url)
            else:
                payload = await fetch_json(client, url)
                rows = payload if provider == "lever" else payload["jobs"]
            if not isinstance(rows, list):
                raise ValueError("The provider returned an unexpected feed format.")
            jobs = await asyncio.to_thread(normalize_rows, provider, rows)
            if rows and not jobs and not (provider == "ashby" and all(row.get("isListed") is False for row in rows)):
                raise ValueError("The feed contained no readable postings.")
            if provider == "smartrecruiters":
                candidates = [i for i, job in enumerate(jobs) if job["score"] > 0
                              and job["region"] in {"india", "india_remote"}]
                candidates.sort(key=lambda i: -jobs[i]["score"])
                detail_limit = asyncio.Semaphore(2)

                async def enrich_smart(index):
                    async with detail_limit:
                        try:
                            original = jobs[index]
                            description, workplace = await smartrecruiters_detail(client, company["board"], original["external_id"])
                            if not workplace and original["evidence"] == f'{original["location"]} · Remote':
                                workplace = "Remote"
                            jobs[index] = await asyncio.to_thread(build_job, original["external_id"], original["title"], original["department"],
                                                                  original["location"], workplace, description, original["url"],
                                                                  original["posted_at"], original["apply_url"])
                        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                            pass  # Preserve the summary when an individual detail is unavailable.
                await asyncio.gather(*(enrich_smart(i) for i in candidates[:20]))
            return jobs, None
        except httpx.HTTPStatusError as exc:
            return [], feed_failure(exc)
        except WorkdaySnapshotError as exc:
            return [], str(exc)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return [], "Employer feed could not be read."
        finally:
            await asyncio.sleep(0.15)


async def fetch_json(client, url, params=None, json_body=None):
    for attempt in range(2):
        try:
            response = await client.post(url, json=json_body) if json_body is not None else await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            retry_after = exc.response.headers.get("Retry-After")
            status = exc.response.status_code
            # Cloudflare reports an unreachable origin as 521-526, so every 5xx is worth one retry.
            if attempt or (status != 429 and status < 500):
                raise
            if retry_after and (not retry_after.isdecimal() or len(retry_after) > 2 or int(retry_after) > 4):
                raise
            await asyncio.sleep(int(retry_after) if retry_after else 0.4)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if attempt:
                raise
            await asyncio.sleep(0.4)


async def workday_detail(client, board, external_id):
    base, public_base = workday_base(board)
    path = workday_path(external_id)
    info = (await fetch_json(client, base + path))["jobPostingInfo"]
    if canonical_url(info["externalUrl"]) != canonical_url(public_base + path):
        raise ValueError("The employer posting did not match.")
    description = clean(info.get("jobDescription"))[:30000]
    if not description:
        raise ValueError("The employer posting contained no description.")
    additional = info.get("additionalLocations") or []
    if not isinstance(additional, list) or not all(isinstance(value, str) for value in additional):
        raise ValueError("The employer posting locations could not be read.")
    locations = [clean(value) for value in [info.get("location"), *additional] if value]
    location = ", ".join(dict.fromkeys(locations))
    workplace = "Remote" if str(info.get("remoteType", "")).casefold() == "remote" else ""
    return description, location, workplace


async def smartrecruiters_detail(client, board, external_id):
    url = PROVIDERS["smartrecruiters"].format(board=quote(board, safe="")) + "/" + quote(external_id, safe="")
    detail = await fetch_json(client, url)
    if (str(detail["id"]) != external_id or detail.get("active") is False
            or detail["company"]["identifier"].casefold() != board.casefold()):
        raise ValueError("The employer posting did not match.")
    sections = detail["jobAd"]["sections"]
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        title, text = clean(section.get("title")), clean(section.get("text"))
        if text:
            parts.append("\n".join(filter(None, (title, text))))
    description = "\n".join(parts)[:30000]
    if not description:
        raise ValueError("The employer posting contained no description.")
    location = detail.get("location") or {}
    workplace = "Hybrid" if location.get("hybrid") else "Remote" if location.get("remote") else ""
    return description, workplace


async def workday_postings(client, base):
    rows, expected, first_paths = [], None, None
    async def page_at(offset):
        return await fetch_json(client, base + "/jobs", json_body={
            "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
    while True:
        page = await page_at(len(rows))
        postings, total = page["jobPostings"], page["total"]
        if not isinstance(postings, list) or not isinstance(total, int) or total < 0:
            raise WorkdaySnapshotError("Workday returned an invalid page.")
        if total >= 2000:
            raise WorkdaySnapshotError("Workday's result count is capped; this feed cannot be scanned completely.")
        if expected is None:
            expected = total
            first_paths = [workday_path(row["externalPath"]) for row in postings]
        elif total not in (0, expected):
            raise WorkdaySnapshotError("Workday pagination was incomplete or changed. Retry this feed.")
        if (not postings and len(rows) < expected) or len(rows) + len(postings) > expected:
            raise WorkdaySnapshotError("Workday pagination was incomplete or changed. Retry this feed.")
        rows.extend(postings)
        if len(rows) == expected:
            if len({workday_path(row["externalPath"]) for row in rows}) != len(rows):
                raise WorkdaySnapshotError("Workday pagination was incomplete or changed. Retry this feed.")
            if len(rows) > len(first_paths):
                first = await page_at(0)
                if (first.get("total") != expected or not isinstance(first.get("jobPostings"), list)
                        or [workday_path(row["externalPath"]) for row in first["jobPostings"]] != first_paths):
                    raise WorkdaySnapshotError("Workday pagination was incomplete or changed. Retry this feed.")
            return rows


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


