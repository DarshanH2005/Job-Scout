import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import scout.app as app_module
from scout.feeds import build_job, eligibility_evidence, fetch_board, geography, normalize
from scout.gmail import Gmail, incoming_reply
from scout.research import public_candidates
from scout.store import Store


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "test.sqlite3"
    with TestClient(app_module.create_app(path, seed=False), base_url="http://127.0.0.1:8765") as client:
        yield client, Store(path)


def result(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def register(client, **overrides):
    return result(client.post("/api/companies", json={
        "name": "Sample Company", "url": "https://job-boards.greenhouse.io/sample",
        "provider": "greenhouse", "board": "sample", **overrides,
    }), 201)


def scan(client, path="/api/scans"):
    result(client.post(path), 202)
    for _ in range(100):
        progress = result(client.get("/api/state"))["scan"]
        if progress["state"] != "running":
            return progress
        time.sleep(.01)
    pytest.fail("The scan did not finish.")


def sample(job_id="23", title="Junior Software Engineer"):
    return build_job(job_id, title, "Engineering", "Remote - India", "",
                     "Build backend APIs.", f"https://job-boards.greenhouse.io/sample/jobs/{job_id}")


def saved_job(client):
    return result(client.post("/api/jobs", json={
        "company": "Sample Company", "title": "Junior Data Engineer",
        "url": "https://sample.example/careers/12", "location": "India", "description": "Build pipelines.",
    }), 201)


def create_draft(client, job, channel="linkedin"):
    return result(client.post(f"/api/jobs/{job['id']}/draft", json={
        "contact": {"name": "Jane Example", "source": "https://sample.example/bio/jane"},
        "channel": channel, "evidence": "I built a SQL pipeline.",
    }), 201)


def action(client, item, name):
    return result(client.post(f"/api/outreach/{item['id']}/events", json={"action": name}))


def test_new_jobs_failure_recovery_and_source_coverage(desk, monkeypatch):
    client, store = desk
    register(client)
    register(client, name="Manual Company", provider="", board="")
    rows, failure = [sample()], None

    async def fetch(_client, _company, _limit):
        return rows, failure

    monkeypatch.setattr(app_module, "fetch_board", fetch)
    baseline = scan(client)
    assert (baseline["checked"], baseline["unsupported"], baseline["fresh"]) == (1, 1, 0)
    original = result(client.get("/api/jobs"))["items"][0]
    rows = [sample(), sample("24")]
    assert scan(client)["fresh"] == 1
    assert len(result(client.get("/api/jobs"))["items"]) == 2
    assert len(result(client.get("/api/jobs?latest=true"))["items"]) == 1
    failure = "HTTP 503"
    assert scan(client)["failed"] == 1
    retained = result(client.get("/api/jobs"))["items"]
    assert len(retained) == 2
    assert all(row["source_status"] == "failed" for row in retained)
    assert store.job(original["id"])["first_seen"] == original["first_seen"]
    failure, rows = None, []
    assert scan(client)["checked"] == 1
    assert len(result(client.get("/api/jobs"))["items"]) == 2
    assert scan(client)["checked"] == 1
    assert result(client.get("/api/jobs"))["items"] == []


def test_retry_checks_only_failed_feeds_and_keeps_full_baseline(desk, monkeypatch):
    client, _ = desk
    register(client)
    register(client, name="Healthy Company", board="healthy")
    calls = []
    retry_mode = False
    fail_sample = False

    async def fetch(_client, company, _limit):
        calls.append(company["board"])
        if company["board"] == "sample":
            if fail_sample:
                return [], "HTTP 503"
            return [sample(), sample("25")] if retry_mode else [sample()], None
        return [sample("44"), sample("45")] if fail_sample else [sample("44")], None

    monkeypatch.setattr(app_module, "fetch_board", fetch)
    scan(client)
    assert client.post("/api/scans/retry-failed").status_code == 409
    fail_sample = True
    full = scan(client)
    assert full["failed"] == 1 and full["checked"] == 1
    assert result(client.get("/api/state"))["stats"]["failed_feeds"] == 1
    calls.clear()
    fail_sample, retry_mode = False, True
    retry = scan(client, "/api/scans/retry-failed")
    assert calls == ["sample"]
    assert retry["scope"] == "retry" and retry["total"] == retry["checked"] == 1
    state = result(client.get("/api/state"))
    assert state["full_scan"]["id"] == full["id"] and state["stats"]["failed_feeds"] == 0
    recent = result(client.get("/api/jobs?latest=true"))["items"]
    assert {row["company"] for row in recent} == {"Sample Company", "Healthy Company"}
    assert all(row["new_scan"] == full["id"] for row in recent)


def test_saved_link_draft_and_independent_application(desk):
    client, _store = desk
    job = saved_job(client)
    duplicate = result(client.post("/api/jobs", json={
        "company": "Sample Company", "title": "Junior Data Engineer",
        "url": job["url"] + "?utm_source=test#apply",
    }), 201)
    assert job["id"] == duplicate["id"]
    item = create_draft(client, job)
    assert item["state"] == "draft" and item["sent_at"] is None
    assert create_draft(client, job)["id"] == item["id"]
    assert "I built a SQL pipeline." in item["draft"]
    assert result(client.get(f"/api/jobs/{job['id']}/people"))["people"][0]["name"] == "Jane Example"
    applied = result(client.patch(f"/api/jobs/{job['id']}", json={"applied": True}))
    assert applied["applied_at"]
    assert result(client.get(f"/api/outreach/{item['id']}"))["state"] == "draft"
    sent = action(client, item, "sent")
    assert sent["state"] == "waiting" and sent["due_at"]
    action(client, item, "sent")
    assert len(result(client.get(f"/api/outreach/{item['id']}"))["events"]) == 1
    assert action(client, item, "replied")["due_at"] is None
    assert action(client, item, "offered")["state"] == "offered"
    assert action(client, item, "submitted")["state"] == "submitted"
    assert result(client.get("/api/outreach"))["items"][0]["applied_at"]


def test_location_evidence_and_provider_dates():
    assert geography("Bengaluru", "Remote", "")[0] == "india_remote"
    assert geography("Hyderabad", "Hybrid", "")[0] == "india"
    assert geography("Bangalore, India; Remote, Poland", "", "")[0] == "india"
    assert geography("Remote", "", "Candidates must be based in the United States.")[0] == "us_remote"
    assert geography("Remote", "", "")[0] == "unclear"
    assert geography("Worldwide remote", "", "")[0] == "india_remote"
    assert geography("Remote", "", "Join us. We are a remote based team.")[0] == "unclear"
    posting = normalize("greenhouse", {
        "id": 1, "title": "Junior Software Engineer", "location": {"name": "Remote"},
        "absolute_url": "https://job-boards.greenhouse.io/sample/jobs/1",
        "updated_at": "2026-02-01", "content": "&lt;p&gt;Build API&lt;/p&gt;",
    })
    assert posting["posted_at"] is None
    assert posting["description"] == "Build API"
    assert normalize("ashby", {"id": 1, "isListed": False}) is None
    assert build_job("1", "Software Engineer", "", "India", "Remote",
                     "7+ years of proven experience as an engineer.", "https://example.com/job")["score"] == 0


def test_candidate_requires_linked_evidence():
    rows = [
        {"title": "Jane Example — Staff Engineer", "url": "https://conf.example/jane", "description": "Jane is a staff engineer at Sample Company."},
        {"title": "How We Build — Engineering", "url": "https://blog.example/how", "description": "Sample Company engineers."},
        {"title": "John Example — Engineer", "url": "https://www.linkedin.com/in/example", "description": "Engineer at Sample Company."},
    ]
    assert [person["name"] for person in public_candidates(rows, "Sample Company", "data engineering")] == ["Jane Example"]


def message(id, at, sender, subject, labels=None, extra=None):
    return {"id": id, "internalDate": str(at), "labelIds": labels or [],
            "payload": {"headers": [{"name": key, "value": val} for key, val in
                                    {"From": sender, "Subject": subject, **(extra or {})}.items()]}}


def test_gmail_clears_follow_up_but_linkedin_email_is_a_hint(desk, monkeypatch):
    client, store = desk
    job = saved_job(client)
    gmail_item = action(client, create_draft(client, job, "gmail"), "sent")
    linkedin_item = action(client, create_draft(client, job), "sent")
    store.set_setting("gmail_account", "me@example.com")
    store.set_setting("linkedin_hints", "1")
    store.write("UPDATE outreach SET thread_id='thread',mail_account='me@example.com',mail_anchor=100,mail_peer='jane@example.com' WHERE id=?",
                (gmail_item["id"],))

    async def token(_self):
        return "me@example.com", "fake-token"

    async def thread(_self, _token, _id):
        return {"messages": [
            message("ooto", 101, "jane@example.com", "Out of office", extra={"Auto-Submitted": "auto-replied"}),
            message("own", 102, "me@example.com", "Sent", labels=["SENT"]),
            message("real", 103, "Jane <jane@example.com>", "Re: Referral"),
        ]}

    async def get(_path, _token, _params=None):
        if _path == "/labels":
            return {"labels": [{"id": "label", "name": "Job Scout LinkedIn"}]}
        if _path == "/messages":
            return {"messages": [{"id": "hint"}]}
        return message("hint", 104, "messages-noreply@linkedin.com", "Jane sent you a message")

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "thread", thread)
    monkeypatch.setattr(Gmail, "_get", staticmethod(get))
    assert result(client.post("/api/gmail/check")) == {"replies": 1}
    assert result(client.post("/api/gmail/check")) == {"replies": 0}
    replied = result(client.get(f"/api/outreach/{gmail_item['id']}"))
    assert replied["state"] == "replied" and replied["due_at"] is None
    assert result(client.get(f"/api/outreach/{linkedin_item['id']}"))["state"] == "waiting"
    assert len(result(client.get("/api/outreach"))["hints"]) == 1
    assert not incoming_reply(message("bounce", 105, "mailer-daemon@example.com", "Undeliverable"), 100, "jane@example.com")


def test_local_request_guard(desk):
    client, _ = desk
    response = client.post("/api/scans", headers={"Origin": "https://outside.example"})
    assert response.status_code == 403


def test_integration_settings_do_not_return_credentials(desk, monkeypatch):
    client, _ = desk
    secrets = {}
    monkeypatch.delenv("SCOUT_BRAVE_KEY", raising=False)
    monkeypatch.setattr(app_module.credentials, "get", lambda name: secrets.get(name, ""))
    monkeypatch.setattr(app_module.credentials, "put", lambda name, value: secrets.update({name: value}))
    key = "test-credential-for-fixture"
    assert result(client.put("/api/integrations/brave", json={"value": key})) == {"configured": True}
    state = client.get("/api/state")
    assert result(state)["search_configured"]
    assert key not in state.text
    assert client.put("/api/integrations/google", json={"value": '{"web":{}}'}).status_code == 400
    assert result(client.put("/api/integrations/brave", json={"value": ""})) == {"configured": False}


def test_smartrecruiters_rejects_incomplete_snapshot():
    calls = []

    def response(request):
        offset = request.url.params["offset"]
        calls.append(offset)
        item = {"id": "123", "name": "Junior Data Engineer", "company": {"identifier": "Sample"},
                "location": {"fullLocation": "Bengaluru, India", "remote": True}}
        return httpx.Response(200, json={"content": [item] if offset == "0" else [], "totalFound": 2})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            return await fetch_board(client, {"provider": "smartrecruiters", "board": "Sample"}, asyncio.Semaphore(1))

    jobs, error = asyncio.run(read())
    assert calls == ["0", "1"]
    assert not jobs and error


def test_shortlist_experience_and_posting_age(desk):
    client, store = desk
    junior = saved_job(client)
    old = result(client.post("/api/jobs", json={
        "company": "Sample Company", "title": "Software Engineer 1",
        "url": "https://sample.example/careers/old",
    }), 201)
    experienced = result(client.post("/api/jobs", json={
        "company": "Sample Company", "title": "Software Engineer 3",
        "url": "https://sample.example/careers/experienced",
    }), 201)
    store.write("UPDATE jobs SET posted_at='2000-01-01T00:00:00Z' WHERE id=?", (old["id"],))
    assert {row["id"] for row in result(client.get("/api/jobs?early_only=true"))["items"]} == {junior["id"], old["id"]}
    assert [row["id"] for row in result(client.get("/api/jobs?early_only=true&recent_days=7"))["items"]] == [junior["id"]]
    assert experienced["score"] == 0
    ranged = build_job("2", "Software Engineer", "", "India", "", "2–4 years of experience.", "https://sample.example/2")
    assert ranged["score"] > 0


def test_posting_requirements_take_precedence_over_general_remote_policy():
    boilerplate = "For US-based roles open to remote work, we hire only in certain states."
    office = "Are able to work in our San Francisco office 5 days/week."
    assert geography("San Francisco Office", "On-site", boilerplate + "\n" + office) == ("other", office)
    assert geography("San Francisco Office", "", boilerplate)[0] == "other"
    assert geography("Remote - US", "", boilerplate)[0] == "us_remote"
    assert build_job("3", "Software Engineer (L3)", "", "India", "Remote",
                     "5-7 years experience writing production-grade code.", "https://sample.example/3")["score"] == 0


def test_explicit_all_remote_policy_uses_the_postings_india_location():
    policy = "All of our roles are remote, however some roles have location-based eligibility requirements."
    assert geography("Bangalore, India", "", policy) == ("india_remote", "Bangalore, India · " + policy)
    assert geography("Bangalore, India", "Hybrid", policy)[0] == "india"
    assert geography("Bangalore, India", "", "Some of our roles are remote.")[0] == "india"
    assert geography("Bangalore, India", "", "Not all of our roles are remote.")[0] == "india"


@pytest.mark.parametrize(("retry_after", "expected"), [("0", 2), ("120", 1)])
def test_temporary_feed_retry_respects_delay(retry_after, expected):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"jobs": []})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_board(client, {"provider": "greenhouse", "board": "sample"}, asyncio.Semaphore(1))

    _, error = asyncio.run(read())
    assert len(requests) == expected
    assert (error is None) == (expected == 2)


def test_follow_up_keeps_request_and_send_history(desk):
    client, _ = desk
    item = create_draft(client, saved_job(client))
    path = f"/api/outreach/{item['id']}"
    assert client.patch(path + "/follow-up", json={"text": "Too early."}).status_code == 409
    original = "My edited request."
    sent = result(client.post(path + "/events", json={"action": "sent", "message": original}))
    draft = result(client.patch(path + "/follow-up", json={"text": "Thanks for taking a look."}))
    assert draft["draft"] == original and draft["follow_up_draft"] == "Thanks for taking a look."
    assert draft["due_at"] == sent["due_at"]
    payload = {"action": "followed_up", "message": "My edited follow-up.", "expected_due_at": sent["due_at"]}
    follow_up = result(client.post(path + "/events", json=payload))
    assert follow_up["draft"] == original
    assert follow_up["follow_up_draft"] == ""
    assert follow_up["due_at"] > sent["due_at"]
    assert [event["message"] for event in follow_up["events"]] == ["My edited follow-up.", original]
    assert client.post(path + "/events", json=payload).status_code == 409
    assert action(client, item, "replied")["due_at"] is None


def test_schema_migration_keeps_existing_conversations_and_jobs(desk):
    client, store = desk
    item = action(client, create_draft(client, saved_job(client)), "sent")
    with store.connect() as conn:
        conn.execute("ALTER TABLE events DROP COLUMN message")
        conn.execute("ALTER TABLE outreach DROP COLUMN follow_up_draft")
        conn.execute("ALTER TABLE outreach DROP COLUMN conversation_url")
        conn.execute("ALTER TABLE jobs DROP COLUMN hidden")
        conn.execute("ALTER TABLE scans DROP COLUMN scope")
        conn.execute("INSERT INTO scans(started_at,state,total) VALUES('2026-01-01','completed',0)")
    store.initialize(seed=False)
    store.initialize(seed=False)
    existing = store.outreach(item["id"])
    assert existing["state"] == "waiting" and existing["draft"] == item["draft"]
    assert existing["follow_up_draft"] == ""
    assert existing["conversation_url"] == ""
    assert existing["events"][0]["kind"] == "sent"
    assert store.job(item["job_id"])["hidden"] == 0
    assert store.last_full_scan()["scope"] == "full"


def test_hidden_job_survives_scan_without_losing_saved_application_or_referral(desk, monkeypatch):
    client, _ = desk
    register(client)

    async def fetch(_client, _company, _limit):
        return [sample()], None

    monkeypatch.setattr(app_module, "fetch_board", fetch)
    scan(client)
    job = result(client.get("/api/jobs"))["items"][0]
    item = create_draft(client, job)
    path = f"/api/jobs/{job['id']}"
    original = result(client.patch(path, json={"saved": True, "applied": True}))
    hidden = result(client.patch(path, json={"hidden": True}))
    assert hidden["saved"] and hidden["applied_at"] == original["applied_at"]
    assert result(client.get("/api/jobs"))["items"] == []
    assert result(client.get("/api/state"))["stats"]["hidden"] == 1
    assert result(client.get("/api/state"))["stats"]["matches"] == 0
    assert result(client.get("/api/jobs?view=saved"))["items"][0]["hidden"] == 1
    scan(client)
    assert result(client.get("/api/jobs?view=hidden"))["items"][0]["id"] == job["id"]
    assert result(client.get(f"/api/outreach/{item['id']}"))["state"] == "draft"
    assert result(client.patch(path, json={"hidden": False}))["hidden"] == 0
    assert result(client.get("/api/jobs"))["items"][0]["id"] == job["id"]


def test_conversation_link_is_separate_from_contact_source_and_draft(desk):
    client, _ = desk
    item = create_draft(client, saved_job(client))
    path = f"/api/outreach/{item['id']}"
    assert client.patch(path + "/conversation-link", json={"url": "javascript:alert(1)"}).status_code == 400
    thread = "https://www.linkedin.com/messaging/thread/example/"
    linked = result(client.patch(path + "/conversation-link", json={"url": thread}))
    assert linked["conversation_url"] == thread and linked["source"] == item["source"]
    assert linked["draft"] == item["draft"] and linked["state"] == "draft" and linked["events"] == []
    assert result(client.get(path))["conversation_url"] == thread
    assert result(client.patch(path + "/conversation-link", json={"url": ""}))["conversation_url"] == ""


def test_job_detail_lists_only_its_referrals(desk):
    client, _ = desk
    first = saved_job(client)
    second = result(client.post("/api/jobs", json={
        "company": first["company"], "title": "Software Engineer, Intern", "url": "https://sample.example/intern",
    }), 201)
    assert result(client.get(f"/api/jobs/{first['id']}"))["referrals"] == []
    closed = create_draft(client, first, "gmail")
    active = action(client, create_draft(client, first), "sent")
    action(client, closed, "closed")
    create_draft(client, second)
    detail = result(client.get(f"/api/jobs/{first['id']}"))
    assert [item["id"] for item in detail["referrals"]] == [active["id"], closed["id"]]
    assert detail["referrals"][0]["due_at"] == active["due_at"]
    assert detail["referrals"][0]["person"] == "Jane Example"
    assert "draft" not in detail["referrals"][0]
    flagged = result(client.patch(f"/api/jobs/{first['id']}", json={"hidden": True}))
    assert flagged["referrals"] == detail["referrals"]


def test_job_detail_quotes_required_and_preferred_eligibility(desk):
    client, _ = desk
    minimum = "Degree obtained by summer 2027, with no more than 18 months of professional experience."
    preferred = "At least 1 year of university education, or equivalent work experience."
    description = "\n".join((
        "About us", "Our company has 20 years of experience.", "Minimum requirements", minimum,
        "Preferred qualifications", preferred, "Responsibilities", "Help the engineering team.",
    ))
    job = result(client.post("/api/jobs", json={
        "company": "Sample Company", "title": "Software Engineer, New Grad",
        "url": "https://sample.example/new-grad", "description": description,
    }), 201)
    assert result(client.get(f"/api/jobs/{job['id']}"))["eligibility"] == [
        {"label": "Required", "text": minimum}, {"label": "Preferred", "text": preferred},
    ]
    assert eligibility_evidence("Our company has 20 years of experience.") == []
