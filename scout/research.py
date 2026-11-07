"""Public source search with conservative matching and an explicit fallback."""

import os
import re
from urllib.parse import urlsplit

import httpx

from .feeds import clean, public_url
from .credentials import brave_key

NAME = re.compile(r"[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){1,3}", re.UNICODE)
TECH = re.compile(r"\b(engineer|engineering|developer|researcher|scientist|engineering manager)\b", re.I)
CONTENT_TITLE = re.compile(r"\b(team|blog|engineering|software|platform|data|career|jobs|home|conference|speaker|meet|build|how|why|introducing|about)\b", re.I)


def search_terms(job):
    title = job["title"].lower()
    if re.search(r"\b(data|analytics)\b", title):
        specialty = "data engineering"
    elif re.search(r"\b(ai|ml|machine learning|research)\b", title):
        specialty = "machine learning"
    else:
        specialty = "software engineering"
    return f'"{job["company"]}" "{specialty}"', specialty


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
                for person in public_candidates(response.json().get("web", {}).get("results", []), job["company"], specialty):
                    if person["name"].casefold() not in seen and len(people) < 3:
                        people.append(person)
                        seen.add(person["name"].casefold())
                status = "searched"
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                status = "unavailable"
        else:
            status = "needs_key"
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
