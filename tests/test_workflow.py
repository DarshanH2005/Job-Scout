import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import scout.app as app_module
from scout.feeds import build_job, eligibility_evidence, fetch_board, geography, graduation_warning, normalize, work_authorization_evidence
from scout.gmail import Gmail, SEND_SCOPE, SCOPE, incoming_reply
from scout.research import ResearchCache, abnormal_engineers, affirm_authors, cloudflare_authors, datadog_speakers, public_candidates, rank_candidates, stripe_authors
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


def test_gmail_direct_send_and_follow_up_track_the_original_thread(desk, monkeypatch):
    client, _ = desk
    item = create_draft(client, saved_job(client), "gmail")
    path = f"/api/outreach/{item['id']}"
    result(client.patch(path + "/contact-handoff", json={"email": "Jane@example.com"}))
    sent = []
    thread_messages = [{"labelIds": ["SENT"], "internalDate": "1", "payload": {"headers": [
        {"name": "Subject", "value": "Referral request for Junior Data Engineer"},
        {"name": "Message-ID", "value": "<first@example.com>"},
    ]}}]

    async def token(self, send=False):
        return "me@example.com", "fake-token"

    async def thread(self, token, thread_id):
        assert thread_id == "example-thread"
        return {"messages": thread_messages}

    async def send(token, message, thread_id=None):
        assert token == "fake-token"
        sent.append((thread_id, message))
        return {"threadId": "example-thread", "id": f"sent-{len(sent)}"}

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "thread", thread)
    monkeypatch.setattr(Gmail, "send_message", staticmethod(send))
    request = {"account": "me@example.com", "recipient": "Jane@example.com", "text": "Hi Jane, may I ask for a referral?"}
    result(client.post(path + "/gmail/send", json={**request, "recipient": "different@example.com"}), 409)
    item = result(client.post(path + "/gmail/send", json=request))
    assert (item["state"], item["thread_id"], item["mail_peer"]) == ("waiting", "example-thread", "jane@example.com")
    assert item["events"][0]["message"] == request["text"]
    assert sent[0][1]["To"] == "Jane@example.com"
    assert sent[0][1].get_content().strip() == request["text"]
    result(client.post(path + "/gmail/send", json=request), 409)
    follow_up = {**request, "recipient": "jane@example.com", "expected_due_at": item["due_at"], "text": "Following up."}
    thread_messages.append({"id":"other-one", "internalDate":str(item["mail_anchor"] + 1), "payload":{"headers":[
        {"name":"From", "value":"recruiter@example.com"}]}})
    assert "Another sender" in result(client.post(path + "/gmail/send", json=follow_up), 409)["detail"]
    assert len(sent) == 1
    thread_messages.pop()
    item = result(client.post(path + "/gmail/send", json=follow_up))
    assert sent[1][0] == "example-thread" and sent[1][1]["In-Reply-To"] == "<first@example.com>"
    assert item["events"][0]["kind"] == "followed_up"
    result(client.post(path + "/gmail/send", json=follow_up), 409)
    thread_messages.append({"internalDate": str(item["mail_anchor"] + 1), "payload": {"headers": [
        {"name": "From", "value": "Jane <jane@example.com>"},
    ]}})
    replied = result(client.post(path + "/gmail/send", json={**follow_up, "expected_due_at": item["due_at"]}))
    assert replied["state"] == "replied" and replied["due_at"] is None and len(sent) == 2


def test_initial_gmail_send_can_schedule_a_reply_aware_follow_up(desk, monkeypatch):
    from datetime import datetime, timedelta, timezone

    client, store = desk
    item = create_draft(client, saved_job(client), "gmail")
    path = f"/api/outreach/{item['id']}"
    result(client.patch(path + "/contact-handoff", json={"email":"Jane@example.com"}))
    store.set_setting("gmail_account", "me@example.com")
    store.set_setting("gmail_send_enabled", "1")
    sent = []
    automatic_reply = [True]

    async def token(self, send=False):
        return "me@example.com", "fake-token"

    async def send(token, message, thread_id=None):
        sent.append(message.get_content().strip())
        return {"id":"sent-one", "threadId":"thread-one"}

    async def thread(self, token, thread_id):
        assert thread_id == "thread-one"
        headers = [{"name":"From", "value":"jane@example.com"}]
        if automatic_reply[0]:
            headers.append({"name":"Auto-Submitted", "value":"auto-replied"})
        return {"messages":[{"id":"reply-one", "internalDate":str(int(time.time() * 1000) + 10000),
                "payload":{"headers":headers}}]}

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "send_message", staticmethod(send))
    monkeypatch.setattr(Gmail, "thread", thread)
    request = {"account":"me@example.com", "recipient":"Jane@example.com", "text":"Initial request."}
    result(client.post(path + "/gmail/send", json={**request, "auto_follow_up":{"text":" ", "days":5}}), 400)
    result(client.post(path + "/gmail/send", json={**request, "auto_follow_up":{"text":"Reminder", "days":0}}), 422)
    assert sent == []
    before = datetime.now(timezone.utc)
    item = result(client.post(path + "/gmail/send", json={**request,
                  "auto_follow_up":{"text":"Just following up.", "days":5}}))
    assert sent == ["Initial request."] and item["state"] == "waiting"
    assert item["schedule_id"] and item["scheduled_message"] == "Just following up."
    assert item["mail_peer"] == "jane@example.com" and item["scheduled_send_at"] == item["due_at"]
    assert before + timedelta(days=5) <= datetime.fromisoformat(item["due_at"]) <= datetime.now(timezone.utc) + timedelta(days=5)
    assert result(client.post("/api/gmail/check")) == {"replies":0}
    item = result(client.get(path))
    assert item["state"] == "waiting" and item["schedule_id"] and "automatic" in item["scheduled_issue"]
    automatic_reply[0] = False
    assert result(client.post("/api/gmail/check")) == {"replies":1}
    item = result(client.get(path))
    assert item["state"] == "replied" and item["schedule_id"] is None and sent == ["Initial request."]


def test_unconfirmed_initial_send_does_not_schedule_automatic_follow_up(desk, monkeypatch):
    client, store = desk
    item = create_draft(client, saved_job(client), "gmail")
    path = f"/api/outreach/{item['id']}"
    result(client.patch(path + "/contact-handoff", json={"email":"jane@example.com"}))
    store.set_setting("gmail_account", "me@example.com")
    store.set_setting("gmail_send_enabled", "1")
    attempts = []

    async def token(self, send=False):
        return "me@example.com", "fake-token"

    async def timeout(*args):
        attempts.append(True)
        raise httpx.ReadTimeout("Gmail did not confirm")

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "send_message", staticmethod(timeout))
    request = {"account":"me@example.com", "recipient":"jane@example.com", "text":"Initial request.",
               "auto_follow_up":{"text":"Saved reminder.", "days":5}}
    result(client.post(path + "/gmail/send", json=request), 502)
    item = result(client.get(path))
    assert item["gmail_send_pending"] and item["schedule_id"] is None
    assert item["follow_up_draft"] == "Saved reminder."
    result(client.post(path + "/gmail/send", json=request), 409)
    assert attempts == [True]
    store.write("UPDATE outreach SET gmail_send_pending='2000-01-01T00:00:00+00:00' WHERE id=?", (item["id"],))
    recovered = result(client.post(path + "/events", json={"action":"sent"}))
    assert recovered["state"] == "waiting" and not recovered["gmail_send_pending"]
    assert recovered["schedule_id"] is None and recovered["follow_up_draft"] == "Saved reminder."


def test_gmail_unknown_delivery_cannot_be_retried(desk, monkeypatch):
    client, store = desk
    item = create_draft(client, saved_job(client), "gmail")
    path = f"/api/outreach/{item['id']}"
    result(client.patch(path + "/contact-handoff", json={"email": "jane@example.com"}))
    attempts = []

    async def token(self, send=False):
        return "me@example.com", "fake-token"

    async def timeout(*args):
        attempts.append(args)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "send_message", staticmethod(timeout))
    request = {"account": "me@example.com", "recipient": "jane@example.com", "text": "Referral request"}
    result(client.post(path + "/gmail/send", json=request), 502)
    store.initialize(seed=False)
    item = result(client.get(path))
    assert item["state"] == "draft" and item["gmail_send_pending"] and item["events"] == []
    result(client.post(path + "/gmail/send", json=request), 409)
    assert len(attempts) == 1


@pytest.mark.parametrize("outcome", ["sent", "replied", "unconfirmed", "unexpected", "automatic_reply", "other_sender", "missing_date"])
def test_scheduled_gmail_follow_up_checks_thread_and_sends_at_most_once(tmp_path, monkeypatch, outcome):
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "scheduled.sqlite3"
    store = Store(db)
    attempts = []
    approved_message = "The follow-up I approved."
    sender = "me@example.com"
    recipient = "jane@example.com"
    thread_id = "gmail-thread"
    monkeypatch.setattr(app_module, "GMAIL_SCHEDULE_INTERVAL", 0.01)

    async def token(self, send=False):
        return sender, "fake-token"

    async def thread(self, token, identifier):
        assert identifier == thread_id
        if outcome == "unexpected":
            raise KeyError("unexpected response")
        messages = [{"labelIds": ["SENT"], "internalDate": "1", "payload": {"headers": [
            {"name": "Subject", "value": "Referral request"},
            {"name": "Message-ID", "value": "<first@example.com>"},
        ]}}]
        if outcome == "replied":
            messages.append({"internalDate": "2", "payload": {"headers": [{"name": "From", "value": recipient}]}})
        elif outcome == "automatic_reply":
            messages.append({"id":"auto-one", "internalDate":"2", "payload":{"headers":[{"name":"From", "value":recipient},
                             {"name":"Auto-Submitted", "value":"auto-replied"}]}})
        elif outcome == "other_sender":
            messages.append({"id":"other-one", "internalDate":"2", "payload":{"headers":[{"name":"From", "value":"recruiter@example.com"}]}})
        elif outcome == "missing_date":
            messages.append({"payload":{"headers":[{"name":"From", "value":recipient}]}})
        return {"messages": messages}

    async def send(token, message, identifier):
        attempts.append((identifier, message.get_content().strip(), message["In-Reply-To"]))
        if outcome == "unconfirmed":
            raise httpx.ReadTimeout("Gmail did not respond")
        return {"id": "follow-up", "threadId": identifier}

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "thread", thread)
    monkeypatch.setattr(Gmail, "send_message", staticmethod(send))
    with TestClient(app_module.create_app(db, seed=False), base_url="http://127.0.0.1:8765") as client:
        store.set_setting("gmail_account", sender)
        store.set_setting("gmail_send_enabled", "1")
        item = action(client, create_draft(client, saved_job(client), "gmail"), "sent")
        path = f"/api/outreach/{item['id']}"
        route = path + "/gmail/follow-up-schedule"
        store.write("UPDATE outreach SET thread_id=?,mail_account=?,mail_peer=?,mail_anchor=1 WHERE id=?",
                    (thread_id, sender, recipient, item["id"]))
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        payload = {"text": approved_message, "account": sender, "recipient": recipient,
                   "thread_id": thread_id, "send_at": future}
        result(client.post(route, json={**payload, "recipient": "other@example.com"}), 409)
        first = result(client.post(route, json=payload))
        result(client.delete(f"{route}/{first['schedule_id']}"))
        scheduled = result(client.post(route, json=payload))
        result(client.delete(f"{route}/{first['schedule_id']}"), 409)
        result(client.post(path + "/gmail/send", json={"text": "A duplicate follow-up", "account": sender,
                      "recipient": recipient, "expected_due_at": item["due_at"]}), 409)
        result(client.patch(path + "/follow-up", json={"text": "A different unscheduled draft"}))
        store.write("UPDATE gmail_follow_up_schedule SET send_at='2000-01-01T00:00:00Z' WHERE id=?",
                    (scheduled["schedule_id"],))

        for _ in range(100):
            updated = result(client.get(path))
            if not updated["schedule_id"] or updated["scheduled_issue"]:
                break
            time.sleep(.01)
        else:
            pytest.fail("The scheduled send did not finish.")

        if outcome in {"unexpected", "automatic_reply", "other_sender", "missing_date"}:
            assert updated["state"] == "waiting" and updated["scheduled_issue"]
            assert attempts == []
            assert result(client.get("/api/state"))["mail"]["scheduling"]
            if outcome == "automatic_reply":
                assert "automatic" in updated["scheduled_issue"]
            elif outcome == "other_sender":
                assert "Another sender" in updated["scheduled_issue"]
            elif outcome == "missing_date":
                assert "message date" in updated["scheduled_issue"]
            time.sleep(.05)
            assert attempts == []
        elif outcome == "replied":
            assert updated["state"] == "replied"
            assert updated["schedule_id"] is None
            assert attempts == []
        else:
            assert attempts == [(thread_id, approved_message, "<first@example.com>")]
            if outcome == "sent":
                assert updated["schedule_id"] is None
                assert updated["events"][0]["message"] == approved_message
            else:
                assert updated["gmail_send_pending"] and updated["scheduled_issue"]
                store.write("UPDATE outreach SET gmail_send_pending='2000-01-01T00:00:00+00:00' WHERE id=?", (item["id"],))
                recovered = result(client.post(path + "/events", json={"action":"followed_up", "expected_due_at":updated["due_at"]}))
                assert not recovered["gmail_send_pending"] and recovered["schedule_id"] is None


def test_gmail_sending_scope_is_opt_in(desk, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    client, _ = desk
    monkeypatch.setattr("scout.gmail.google_client", lambda: {"client_id": "test"})
    for send, scopes in [(False, [SCOPE]), (True, [SCOPE, SEND_SCOPE])]:
        url = result(client.post("/api/gmail/authorize", json={"send": send}))["url"]
        assert parse_qs(urlsplit(url).query)["scope"][0].split() == scopes


@pytest.mark.parametrize("outcome", ["send", "reply", "new_during_review", "missing_id", "stale_view"])
def test_resuming_schedule_acknowledges_only_reviewed_messages(tmp_path, monkeypatch, outcome):
    from datetime import datetime, timedelta, timezone
    import json

    db = tmp_path / "resume.sqlite3"
    store = Store(db)
    attempts = []
    monkeypatch.setattr(app_module, "GMAIL_SCHEDULE_INTERVAL", 0.01)
    messages = [
        {"id":"sent-one", "labelIds":["SENT"], "internalDate":"1", "payload":{"headers":[
            {"name":"Subject", "value":"Referral request"}, {"name":"Message-ID", "value":"<first@example.com>"}]}},
        {"id":"auto-one", "internalDate":"2", "payload":{"headers":[
            {"name":"From", "value":"jane@example.com"}, {"name":"Auto-Submitted", "value":"auto-replied"}]}},
    ]

    async def token(self, send=False):
        return "me@example.com", "fake-token"

    async def thread(self, token, identifier):
        assert identifier == "thread-one"
        return {"messages":messages}

    async def send(token, message, identifier):
        attempts.append((identifier, message.get_content().strip()))
        return {"id":"follow-up", "threadId":identifier}

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "thread", thread)
    monkeypatch.setattr(Gmail, "send_message", staticmethod(send))
    with TestClient(app_module.create_app(db, seed=False), base_url="http://127.0.0.1:8765") as client:
        store.set_setting("gmail_account", "me@example.com")
        store.set_setting("gmail_send_enabled", "1")
        item = action(client, create_draft(client, saved_job(client), "gmail"), "sent")
        path = f"/api/outreach/{item['id']}"
        store.write("UPDATE outreach SET thread_id='thread-one',mail_account='me@example.com',mail_peer='jane@example.com',mail_anchor=1 WHERE id=?", (item["id"],))
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        scheduled = result(client.post(path + "/gmail/follow-up-schedule", json={"text":"Approved follow-up.",
            "account":"me@example.com", "recipient":"jane@example.com", "thread_id":"thread-one", "send_at":future}))
        assert result(client.post("/api/gmail/check")) == {"replies":0}
        paused = result(client.get(path))
        assert json.loads(paused["scheduled_review_pending_ids"]) == ["auto-one"]
        resume = path + "/gmail/follow-up-schedule/" + scheduled["schedule_id"] + "/resume"
        request = {"expected_issue":paused["scheduled_issue"], "expected_pending_ids":paused["scheduled_review_pending_ids"]}
        if outcome == "reply":
            messages.append({"id":"human-one", "internalDate":"3", "payload":{"headers":[{"name":"From", "value":"jane@example.com"}]}})
            result_item = result(client.post(resume, json=request))
            assert result_item["state"] == "replied" and result_item["schedule_id"] is None and attempts == []
            return
        if outcome == "missing_id":
            messages[1].pop("id")
            assert "message ID" in result(client.post(resume, json=request), 409)["detail"]
            assert result(client.get(path))["scheduled_issue"] and attempts == []
            return
        if outcome == "stale_view":
            result(client.post(resume, json={**request, "expected_pending_ids":"[]"}), 409)
            assert result(client.get(path))["scheduled_issue"] and attempts == []
            return
        if outcome == "new_during_review":
            messages.append({"id":"other-one", "internalDate":"2", "payload":{"headers":[{"name":"From", "value":"recruiter@example.com"}]}})
            assert "new message" in result(client.post(resume, json=request), 409)["detail"]
            paused = result(client.get(path))
            assert set(json.loads(paused["scheduled_review_pending_ids"])) == {"auto-one", "other-one"}
            request = {"expected_issue":paused["scheduled_issue"], "expected_pending_ids":paused["scheduled_review_pending_ids"]}
        resumed = result(client.post(resume, json=request))
        assert resumed["schedule_id"] and resumed["scheduled_issue"] == ""
        assert "auto-one" in json.loads(resumed["scheduled_reviewed_message_ids"])
        assert result(client.post("/api/gmail/check")) == {"replies":0}
        assert result(client.get(path))["scheduled_issue"] == ""
        if outcome == "send":
            messages.append({"id":"other-one", "internalDate":"2", "payload":{"headers":[{"name":"From", "value":"recruiter@example.com"}]}})
            result(client.post("/api/gmail/check"))
            paused = result(client.get(path))
            assert paused["scheduled_issue"] and json.loads(paused["scheduled_review_pending_ids"]) == ["other-one"]
            resumed = result(client.post(resume, json={"expected_issue":paused["scheduled_issue"],
                             "expected_pending_ids":paused["scheduled_review_pending_ids"]}))
        store.write("UPDATE gmail_follow_up_schedule SET send_at='2000-01-01T00:00:00Z' WHERE id=?", (resumed["schedule_id"],))
        for _ in range(100):
            updated = result(client.get(path))
            if not updated["schedule_id"] or updated["scheduled_issue"]:
                break
            time.sleep(.01)
        assert attempts == [("thread-one", "Approved follow-up.")]
        assert updated["schedule_id"] is None


def test_stripe_author_research_requires_recent_engineering_bio(monkeypatch):
    from datetime import date

    urls = ["https://stripe.dev/authors/alex-one", "https://stripe.dev/authors/blair-two",
            "https://stripe.dev/authors/casey-three"]
    sitemap = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
        f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>"
    bios = {
        "/authors/alex-one": ("Alex One", "Alex is a software engineer at Stripe.", date.today().strftime("%Y.%m.%d")),
        "/authors/blair-two": ("Blair Two", "Blair is a writer at Stripe covering engineering.", date.today().strftime("%Y.%m.%d")),
        "/authors/casey-three": ("Casey Three", "Casey is an engineer at Stripe.", "2020.01.01"),
    }

    def respond(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap)
        if request.url.path == "/blog/topic/engineering":
            return httpx.Response(200, text="<p>Author:</p><p>Casey Three</p><p>Authors:</p><p>Alex One, Blair Two</p>")
        name, bio, published = bios[request.url.path]
        return httpx.Response(200, text=f"<h2>Blog: {name}(1)</h2><p>{bio}</p><p>{published}</p>")

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.research.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    candidates = asyncio.run(stripe_authors())
    assert [person["name"] for person in candidates] == ["Alex One"]
    assert candidates[0]["source"] == urls[0]
    assert "Verify location" in candidates[0]["reason"]


def test_stripe_recent_authors_are_checked_before_sitemap_limit(monkeypatch):
    from datetime import date

    urls = [f"https://stripe.dev/authors/author-{i:02d}" for i in range(20)] + ["https://stripe.dev/authors/zara-example"]
    sitemap = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
        f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>"
    fetched = []

    def respond(request):
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap)
        if request.url.path == "/blog/topic/engineering":
            return httpx.Response(200, text="<p>Author:</p><p>Zara Example</p>")
        fetched.append(request.url.path)
        if request.url.path == "/authors/zara-example":
            return httpx.Response(200, text=f"<h2>Blog: Zara Example(1)</h2><p>Zara is an engineer at Stripe.</p><p>{date.today():%Y.%m.%d}</p>")
        return httpx.Response(404)

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.research.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    candidates = asyncio.run(stripe_authors())
    assert [person["name"] for person in candidates] == ["Zara Example"]
    assert len(fetched) == 16 and "/authors/zara-example" in fetched


def test_cloudflare_authors_need_public_engineering_role(monkeypatch):
    pages = {
        "/": '<a href="/author/engineer-one/">Engineer</a><a href="/author/writer-two/">Writer</a>',
        "/author/engineer-one/": '<title>Alex One - Cloudflare Blog</title><div><h1>Alex One</h1><p>Systems Engineer • London</p></div>',
        "/author/writer-two/": '<title>Blair Two - Cloudflare Blog</title><div><h1>Blair Two</h1><p>Technical writer covering engineering</p></div>',
    }
    def respond(request):
        return httpx.Response(200, text=pages[request.url.path])

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.research.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    candidates = asyncio.run(cloudflare_authors())
    assert [person["name"] for person in candidates] == ["Alex One"]
    assert candidates[0]["source"] == "https://blog.cloudflare.com/author/engineer-one/"
    assert "Verify current employment" in candidates[0]["reason"]


def test_datadog_speakers_require_recent_employer_and_engineering_role(monkeypatch):
    from datetime import date

    year = date.today().year
    html = f'''<title>{year} Speakers | DASH by Datadog</title>
        <p>Alex One</p><p>Software Engineer | Datadog</p><p>Alex works on backend systems at Datadog.</p>
        <p>Blair Two</p><p>Software Engineer | Other Company</p>
        <p>Casey Three</p><p>Product Manager | Datadog</p>
        <p>Alex One</p><p>Software Engineer | Datadog</p>'''

    def respond(request):
        assert str(request.url) == "https://dash.datadoghq.com/sessions/speakers/"
        return httpx.Response(200, text=html)

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.research.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    candidates = asyncio.run(datadog_speakers())
    assert [person["name"] for person in candidates] == ["Alex One"]
    assert candidates[0]["title"] == "Software Engineer"
    assert candidates[0]["notes"] == "Alex works on backend systems at Datadog."
    assert str(year) in candidates[0]["reason"] and "Verify" in candidates[0]["reason"]
    html = html.replace(str(year), str(year - 3))
    assert asyncio.run(datadog_speakers()) == []


def test_affirm_feed_requires_recent_named_technical_byline_and_official_source(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    now = datetime.now(timezone.utc)
    def entry(byline, *, age=0, link="https://tech.affirm.com/post?source=rss"):
        published = format_datetime(now - timedelta(days=age))
        return (f"<item><title>Building Data Systems</title><link>{link.replace('&', '&amp;')}</link>"
                f"<pubDate>{published}</pubDate><content:encoded><![CDATA[<p>{byline}</p>]]></content:encoded></item>")
    rss = '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>' + "".join([
        entry("Authors: Alex One, Blair Two, Data Platform, Affirm"),
        entry("Author: Helen Eight, Checkout Engineering, Affirm"),
        entry("Author: Ines Nine, Machine Learning &amp; Decision Platform, Affirm"),
        entry("Author: Jules Ten, Machine Learning and Decision Platform, Affirm"),
        entry("Author: Alex One, Data Platform, Affirm"),
        entry("Author: Casey Three, Data Platform, Affirm", age=800),
        entry("Author: Dana Four, Data Platform, Affirm", age=-1),
        entry("Author: Erin Five, Data Platform, Affirm", link="https://other.example/post"),
        entry("Author: Fran Six, Product Marketing, Affirm"),
        entry("Author: Team Blog, Data Platform, Affirm"),
        entry("Author: Grace Seven, Data Platform"),
    ]) + "</channel></rss>"

    def respond(request):
        assert str(request.url) == "https://tech.affirm.com/feed"
        return httpx.Response(200, content=rss.encode())

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.research.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    people = asyncio.run(affirm_authors())
    assert [person["name"] for person in people] == ["Alex One", "Helen Eight", "Ines Nine", "Blair Two", "Jules Ten"]
    assert all(person["source"] == "https://tech.affirm.com/post" for person in people)
    assert people[0]["title"] == "Author · Data Platform" and people[1]["title"] == "Author · Checkout Engineering"
    assert "Verify their current role" in people[0]["reason"]


def test_abnormal_stories_require_recent_named_engineering_interview(monkeypatch):
    from datetime import date

    month = date.today().strftime("%B %Y")
    def story(name, section, published, body):
        return (f"<title>Meet {name} | Abnormal AI</title><p>{section} · {published}</p>"
                f"<h1>Meet {name}</h1><p>{body}</p>")
    pages = {
        "/careers/stories": '<a href="/careers/stories/alex-example">Alex</a>'
                             '<a href="https://evil.example/careers/stories/fake">External</a>',
        "/sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
            f'<url><loc>https://abnormal.ai/careers/stories/{slug}</loc></url>'
            for slug in ['alex-example', 'old-person', 'marketing-person', 'unrelated-person']) + '</urlset>',
        "/careers/stories/alex-example": story("Alex Example", "Engineering", month,
                                                "I am a software engineer working on backend systems in Bangalore."),
        "/careers/stories/old-person": story("Blair Example", "Engineering", "January 2020", "I am a software engineer."),
        "/careers/stories/marketing-person": story("Casey Example", "Marketing", month, "I am a software engineer."),
        "/careers/stories/unrelated-person": story("Dana Example", "Engineering", month, "Our software engineer works on backend systems."),
    }
    fetched = []
    def respond(request):
        assert request.url.host == "abnormal.ai"
        fetched.append(request.url.path)
        return httpx.Response(200, text=pages[request.url.path])

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.research.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    people = asyncio.run(abnormal_engineers())
    assert [person["name"] for person in people] == ["Alex Example"]
    assert people[0]["source"] == "https://abnormal.ai/careers/stories/alex-example"
    assert month in people[0]["reason"] and "Verify" in people[0]["reason"]
    assert "backend" in people[0]["notes"]
    assert fetched.count("/careers/stories/alex-example") == 1


def test_contact_ranking_explains_related_area_without_claiming_same_team():
    job = {"title": "Software Engineer - Kubernetes platform", "department": ""}
    candidates = [
        {"name": "Dan Jones", "title": "Principal Red Team Engineer", "notes": "", "reason": "Public profile."},
        {"name": "Rupert Carr", "title": "Systems Engineer", "notes": "", "reason": "Public profile."},
    ]
    ranked = rank_candidates(job, candidates)
    assert [person["name"] for person in ranked] == ["Rupert Carr", "Dan Jones"]
    assert "infrastructure" in ranked[0]["reason"] and "team is unconfirmed" in ranked[0]["reason"]
    assert "does not establish a matching specialty" in ranked[1]["reason"]


def test_public_author_cache_deduplicates_and_retries_empty_results():
    async def exercise():
        cache = ResearchCache()
        calls = []

        async def search():
            calls.append("search")
            await asyncio.sleep(.01)
            return [{"name": "Alex Example"}]

        first, second = await asyncio.gather(cache.authors("stripe", search), cache.authors("stripe", search))
        assert len(calls) == 1
        assert first == ([{"name": "Alex Example"}], False)
        assert second == ([{"name": "Alex Example"}], True)
        cache.entries["stripe"] = (0, first[0])
        assert (await cache.authors("stripe", search))[1] is False
        assert len(calls) == 2

        async def empty():
            calls.append("empty")
            return []

        await cache.authors("cloudflare", empty)
        await cache.authors("cloudflare", empty)
        assert calls.count("empty") == 2

    asyncio.run(exercise())


def test_people_endpoint_reuses_official_authors_per_company(desk, monkeypatch):
    client, _ = desk
    calls = []

    async def authors():
        calls.append(True)
        return [{"name": "Alex Example", "title": "Software engineer at Stripe", "source": "https://stripe.dev/authors/alex-example",
                 "notes": "", "reason": "Public author page."}]

    monkeypatch.setattr("scout.research.stripe_authors", authors)
    monkeypatch.setattr("scout.research.brave_key", lambda: "")
    job = result(client.post("/api/jobs", json={"company": "Stripe", "title": "Software Engineer",
                         "url": "https://example.com/stripe-role"}), 201)
    path = f"/api/jobs/{job['id']}/people"
    assert result(client.get(path))["official_cached"] is False
    second = result(client.get(path))
    assert second["official_cached"] is True and second["people"][0]["name"] == "Alex Example"
    assert len(calls) == 1


def test_abnormal_people_works_without_paid_search_key(desk, monkeypatch):
    client, _ = desk
    calls = []

    async def stories():
        calls.append(True)
        return [{"name": "Alex Example", "title": "Engineering career story",
                 "source": "https://abnormal.ai/careers/stories/alex-example",
                 "notes": "Backend engineer", "reason": "Dated employer interview."}]

    monkeypatch.setattr("scout.research.abnormal_engineers", stories)
    monkeypatch.setattr("scout.research.brave_key", lambda: "")
    job = result(client.post("/api/jobs", json={"company": "Abnormal AI", "title": "Software Engineer - Backend",
                         "url": "https://example.com/abnormal-role"}), 201)
    path = f"/api/jobs/{job['id']}/people"
    first, second = result(client.get(path)), result(client.get(path))
    assert first["status"] == "official" and first["people"][0]["name"] == "Alex Example"
    assert "backend" in first["people"][0]["reason"] and "team is unconfirmed" in first["people"][0]["reason"]
    assert second["official_cached"] is True and len(calls) == 1


def test_expired_gmail_authorization_surfaces_reconnect_in_settings(desk, monkeypatch):
    from types import SimpleNamespace

    client, store = desk
    store.set_setting("gmail_account", "me@example.com")
    store.set_setting("gmail_send_enabled", "1")
    vault = SimpleNamespace(get_password=lambda *_: '{"expires_at": 0, "refresh_token": "test"}')
    monkeypatch.setattr(Gmail, "_vault", staticmethod(lambda: vault))
    monkeypatch.setattr("scout.gmail.google_client", lambda: {"client_id": "test"})

    def token_expired(request):
        assert request.url.host == "oauth2.googleapis.com"
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(400, json={"error": "invalid_grant"})

    http_client = httpx.AsyncClient
    monkeypatch.setattr("scout.gmail.httpx.AsyncClient",
                        lambda **kwargs: http_client(transport=httpx.MockTransport(token_expired), **kwargs))
    message = result(client.post("/api/gmail/check"), 502)["detail"]
    assert "Reconnect Gmail" in message
    mail = result(client.get("/api/state"))["mail"]
    assert mail["issue"] == message
    assert mail["send_enabled"]


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


def test_newly_discovered_jobs_rank_ahead_within_location_group(desk, monkeypatch):
    client, _ = desk
    register(client)
    old = sample("23", "Junior Software Engineer")
    new = sample("24", "Software Engineer")
    new["posted_at"] = "2020-01-01"
    us_job = build_job("25", "Junior Software Engineer", "Engineering", "Remote - US", "", "Build APIs.",
                       "https://job-boards.greenhouse.io/sample/jobs/25")
    rows = [old]

    async def fetch(_client, _company, _limit):
        return rows, None

    monkeypatch.setattr(app_module, "fetch_board", fetch)
    scan(client)
    rows = [old, new, us_job]
    assert scan(client)["fresh"] == 2
    jobs = result(client.get("/api/jobs"))["items"]
    assert [job["region"] for job in jobs] == ["india_remote", "india_remote", "us_remote"]
    assert [job["title"] for job in jobs] == ["Software Engineer", "Junior Software Engineer", "Junior Software Engineer"]
    assert jobs[0]["score"] < jobs[1]["score"]
    assert [job["id"] for job in result(client.get("/api/jobs?latest=true"))["items"]] == [jobs[0]["id"], jobs[2]["id"]]
    scan(client)
    assert result(client.get("/api/jobs"))["items"][0]["title"] == "Junior Software Engineer"


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
        "application_deadline": "2026-11-30T20:00:00Z",
    })
    assert posting["posted_at"] is None
    assert posting["deadline_at"] == "2026-11-30T20:00:00+00:00"
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


def test_linked_gmail_thread_must_match_saved_single_recipient(desk, monkeypatch):
    client, store = desk
    item = action(client, create_draft(client, saved_job(client), "gmail"), "sent")
    path = f"/api/outreach/{item['id']}"
    store.set_setting("gmail_account", "me@example.com")
    outgoing = {"To": "Jane <jane@example.com>"}

    async def token(_self):
        return "me@example.com", "fake-token"

    async def label(_self, _token, _name):
        return "job-scout-label"

    async def thread(_self, _token, _id):
        return {"messages": [message("sent", 101, "me@example.com", "Referral",
                                      labels=["SENT", "job-scout-label"], extra=outgoing)]}

    monkeypatch.setattr(Gmail, "token", token)
    monkeypatch.setattr(Gmail, "_label", label)
    monkeypatch.setattr(Gmail, "thread", thread)
    link = {"thread_id": "thread-one"}
    assert "Save the contact" in result(client.post(path + "/gmail", json=link), 409)["detail"]
    result(client.patch(path + "/contact-handoff", json={"email": "other@example.com"}))
    assert "does not match" in result(client.post(path + "/gmail", json=link), 409)["detail"]
    result(client.patch(path + "/contact-handoff", json={"email": "Jane@example.com"}))
    outgoing["Cc"] = "other@example.com"
    assert "Cc or Bcc" in result(client.post(path + "/gmail", json=link), 409)["detail"]
    outgoing.pop("Cc")
    outgoing["Bcc"] = "other@example.com"
    assert "Cc or Bcc" in result(client.post(path + "/gmail", json=link), 409)["detail"]
    outgoing.pop("Bcc")
    outgoing["Cc"] = outgoing.pop("To")
    assert "Cc or Bcc" in result(client.post(path + "/gmail", json=link), 409)["detail"]
    outgoing["To"] = outgoing.pop("Cc")
    linked = result(client.post(path + "/gmail", json=link))
    assert linked["thread_id"] == "thread-one" and linked["mail_peer"] == "jane@example.com"
    result(client.post(path + "/gmail", json={"thread_id": "thread-two"}), 409)
    assert result(client.get(path))["thread_id"] == "thread-one"


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


@pytest.mark.parametrize("case", ["complete", "zero_total", "capped", "missing", "shifting", "duplicate", "unsafe_path", "first_changed"])
def test_workday_requires_complete_stable_snapshot(case):
    import json
    calls = []
    paths = ["/job/India-Bangalore/Junior-Software-Engineer_R1", "/job/US/Software-Engineer_R2"]

    def respond(request):
        assert request.url.host == "sample.wd5.myworkdayjobs.com"
        if request.method == "GET":
            return httpx.Response(404)
        assert request.method == "POST"
        offset = json.loads(request.content)["offset"]
        calls.append(offset)
        total = 2000 if case == "capped" else 3 if case == "shifting" and offset else 0 if case == "zero_total" and offset else 2
        path = paths[0] if not offset or case == "duplicate" else paths[1]
        if case == "first_changed" and not offset and len(calls) > 1:
            path = paths[1]
        if case == "unsafe_path":
            path = "/job/%2e%2e/evil"
        rows = [] if case == "missing" and offset else [{"title":"Junior Software Engineer", "externalPath":path,
                                                         "locationsText":"Bengaluru, India", "postedOn":"Posted Today"}]
        return httpx.Response(200, json={"total":total, "jobPostings":rows})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_board(client, {"provider":"workday", "board":"sample.wd5.External"}, asyncio.Semaphore(1))

    jobs, error = asyncio.run(read())
    if case in {"complete", "zero_total"}:
        assert not error and calls == [0, 1, 0] and len(jobs) == 2
        assert jobs[0]["region"] == "india" and jobs[0]["posted_at"] is None
        assert jobs[0]["url"] == "https://sample.wd5.myworkdayjobs.com/External" + paths[0]
    else:
        assert not jobs and error
        if case == "capped":
            assert "capped" in error


def test_workday_details_are_checked_and_loaded_on_demand(desk, monkeypatch):
    client, store = desk
    store.set_setting("graduation_year", "2027")
    job = saved_job(client)
    path = "/job/India-Bangalore/Junior-Data-Engineer_R1"
    url = "https://sample.wd5.myworkdayjobs.com/External" + path
    store.write("UPDATE companies SET provider='workday',board='sample.wd5.External' WHERE id=?", (job["company_id"],))
    store.write("UPDATE jobs SET external_id=?,url=?,manual=0,description='',location='2 Locations',region='other',evidence='2 Locations' WHERE id=?",
                (path, url, job["id"]))
    detail = {"jobPostingInfo":{"externalUrl":url, "canApply":True, "location":"Bengaluru, India", "additionalLocations":["Pune, India"],
              "jobDescription":"<h2>Minimum requirements</h2><p>3+ years of experience.</p><p>Graduating in 2028.</p><p>Must be authorized to work in India.</p>"}}
    status = [200]

    def respond(request):
        assert request.url.host == "sample.wd5.myworkdayjobs.com"
        return httpx.Response(status[0], json=detail)

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.app.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    loaded = result(client.get(f"/api/jobs/{job['id']}"))
    assert loaded["details_status"] == "loaded" and "3+ years" in loaded["detail_assessment"]
    assert loaded["graduation_warning"] == {"saved_year":2027, "excerpt":"Graduating in 2028."}
    assert loaded["detail_location"]["region"] == "india"
    assert loaded["work_authorization"] == ["Must be authorized to work in India."]
    assert store.job(job["id"])["description"] == ""
    assert result(client.post(f"/api/jobs/{job['id']}/verify"))["status"] == "listed"
    detail["jobPostingInfo"]["canApply"] = False
    assert result(client.post(f"/api/jobs/{job['id']}/verify"))["status"] == "unavailable"
    detail["jobPostingInfo"]["externalUrl"] = "https://other.example/job/different"
    assert result(client.get(f"/api/jobs/{job['id']}"))["details_status"] == "unavailable"
    status[0] = 404
    assert result(client.post(f"/api/jobs/{job['id']}/verify"))["status"] == "not_listed"


@pytest.mark.parametrize("case", ["success", "unavailable", "mismatch"])
def test_workday_scan_resolves_early_career_multilocation_posts(case):
    path = "/job/India-Bangalore/Summer-Intern_R1"
    public = "https://sample.wd5.myworkdayjobs.com/External" + path

    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total":1, "jobPostings":[
                {"title":"Software Engineer Intern", "externalPath":path, "locationsText":"2 Locations"}]})
        if case == "unavailable":
            return httpx.Response(503)
        return httpx.Response(200, json={"jobPostingInfo":{
            "externalUrl": public if case == "success" else "https://other.example/different",
            "location":"Bangalore, India", "additionalLocations":["Hyderabad, India"],
            "jobDescription":"<p>Graduating in 2028.</p>"}})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_board(client, {"provider":"workday", "board":"sample.wd5.External"}, asyncio.Semaphore(1))
    jobs, error = asyncio.run(read())
    assert not error and len(jobs) == 1
    if case == "success":
        assert jobs[0]["region"] == "india" and "Hyderabad" in jobs[0]["location"]
        assert "Graduating in 2028" in jobs[0]["description"]
    else:
        assert jobs[0]["region"] == "unclear" and jobs[0]["location"] == "2 Locations"
        assert jobs[0]["description"] == ""


def test_workday_location_enrichment_is_bounded():
    import json
    details = []
    rows = [{"title":"Software Engineer Intern", "externalPath":f"/job/India/Intern_R{i}",
             "locationsText":"2 Locations"} for i in range(21)]

    def respond(request):
        if request.method == "POST":
            offset = json.loads(request.content)["offset"]
            return httpx.Response(200, json={"total":21 if not offset else 0,
                                             "jobPostings":rows[offset:offset + 20]})
        path = request.url.path.removeprefix("/wday/cxs/sample/External")
        details.append(path)
        return httpx.Response(200, json={"jobPostingInfo":{
            "externalUrl":"https://sample.wd5.myworkdayjobs.com/External" + path,
            "location":"Bangalore, India", "jobDescription":"<p>Build software.</p>"}})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_board(client, {"provider":"workday", "board":"sample.wd5.External"}, asyncio.Semaphore(1))
    jobs, error = asyncio.run(read())
    assert not error and len(jobs) == 21 and len(details) == 20
    assert sum(job["region"] == "india" for job in jobs) == 20
    assert sum(job["region"] == "unclear" for job in jobs) == 1


@pytest.mark.parametrize(("description", "expected_score"), [
    ("<p>5+ years of experience as a software engineer.</p>", 0),
    ("<h2>Preferred qualifications</h2><p>5+ years of experience.</p>", 55),
    ("<p>1+ years of experience as a software engineer.</p>", 55),
])
def test_workday_scan_checks_requirements_for_india_roles(description, expected_score):
    path = "/job/India-Bangalore/Software-Engineer_R1"
    public = "https://sample.wd5.myworkdayjobs.com/External" + path
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total":1, "jobPostings":[
                {"title":"Software Engineer", "externalPath":path, "locationsText":"Bangalore, India"}]})
        return httpx.Response(200, json={"jobPostingInfo":{"externalUrl":public,
                              "location":"Bangalore, India", "jobDescription":description}})
    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_board(client, {"provider":"workday", "board":"sample.wd5.External"}, asyncio.Semaphore(1))
    jobs, error = asyncio.run(read())
    assert not error and jobs[0]["score"] == expected_score and jobs[0]["description"]


def test_workday_source_migration_preserves_customized_company(tmp_path):
    store = Store(tmp_path / "migration.sqlite3")
    store.initialize(seed=True)
    store.write("UPDATE companies SET provider='',board='',status='unsupported' WHERE name='Adobe'")
    store.write("UPDATE companies SET provider='',board='',url='https://custom.example/jobs' WHERE name='Intel'")
    store.write("DELETE FROM settings WHERE key='sources_workday_1'")
    store.initialize(seed=True)
    assert store.one("SELECT provider,board FROM companies WHERE name='Adobe'") == {
        "provider":"workday", "board":"adobe.wd5.external_experienced"}
    assert store.one("SELECT provider,url FROM companies WHERE name='Intel'") == {
        "provider":"", "url":"https://custom.example/jobs"}


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
    assert result(client.get("/api/jobs?early_only=true&recent_days=7&date_basis=posted"))["items"] == []
    assert {row["id"] for row in result(client.get("/api/jobs?early_only=true&recent_days=7&date_basis=seen"))["items"]} == {junior["id"], old["id"]}
    store.write("UPDATE jobs SET posted_at=datetime('now') WHERE id=?", (old["id"],))
    assert [row["id"] for row in result(client.get("/api/jobs?early_only=true&recent_days=7&date_basis=posted"))["items"]] == [old["id"]]
    result(client.get("/api/jobs?date_basis=invalid"), 422)
    assert experienced["score"] == 0
    ranged = build_job("2", "Software Engineer", "", "India", "", "2–4 years of experience.", "https://sample.example/2")
    assert ranged["score"] > 0


def test_early_title_with_experience_requirement_is_not_ranked_as_new_grad():
    job = build_job("1", "Software Engineer 1", "", "India", "", "Must Have Skills\n2+ years of experience as a software engineer.", "https://example.com/1")
    assert job["score"] == 55 and "2+ years" in job["reason"]
    preferred = build_job("2", "Software Engineer 1", "", "India", "", "Preferred qualifications\n2+ years of experience.", "https://example.com/2")
    assert preferred["score"] == 90
    capped = build_job("3", "Software Engineer 1", "", "India", "", "Up to 2 years of experience.", "https://example.com/3")
    assert capped["score"] == 90


def test_senior_title_abbreviations_are_not_new_grad_matches(desk):
    for title in ("Mgr, Software Development", "Data Engineering SMTS/LMTS", "Software Engineering PMTS"):
        assert build_job("1", title, "", "India", "", "", "https://example.com/1")["score"] == 0
    assert build_job("2", "Software Engineer AMTS", "", "India", "", "", "https://example.com/2")["score"] == 55

    client, store = desk
    job = result(client.post("/api/jobs", json={"company":"Example", "title":"Software Engineering PMTS",
                 "url":"https://example.com/saved-senior"}), 201)
    result(client.patch(f"/api/jobs/{job['id']}", json={"saved":True}))
    store.write("UPDATE jobs SET score=55,reason='Old score' WHERE id=?", (job["id"],))
    store.write("DELETE FROM settings WHERE key='senior_title_filter_1'")
    store.initialize(seed=False)
    refreshed = result(client.get(f"/api/jobs/{job['id']}"))
    assert refreshed["score"] == 0 and refreshed["saved"]


def test_experience_ranking_respects_preferred_and_all_required_evidence(desk):
    preferred = "Minimum requirements\nA computer science degree.\nPreferred qualifications\n3+ years of experience."
    assert build_job("1", "Software Engineer", "", "India", "", preferred, "https://example.com/1")["score"] == 55
    assert build_job("2", "Software Engineer", "", "India", "", "Our company has 20 years of experience.", "https://example.com/2")["score"] == 55
    many = "Minimum requirements\n" + "\n".join(f"{i} year of experience in project {i}." for i in range(1, 5)) + "\n2+ years of software engineering experience."
    assert build_job("3", "Software Engineer 1", "", "India", "", many, "https://example.com/3")["score"] == 55

    client, store = desk
    job = result(client.post("/api/jobs", json={"company":"Example", "title":"Software Engineer",
                 "url":"https://example.com/preferred", "description":preferred}), 201)
    store.write("UPDATE jobs SET score=0,reason='' WHERE id=?", (job["id"],))
    store.write("DELETE FROM settings WHERE key='experience_ranking_2'")
    store.initialize(seed=False)
    assert result(client.get(f"/api/jobs/{job['id']}"))["score"] == 55


def test_experience_requirements_include_role_wording_and_degree_alternatives(desk):
    role = "Experience: 3+ years in a Technical Support, Software Engineering, or Technical Analyst role."
    alternatives = "Bachelors with 8+, Masters with 6+ years, or PhD with 3+ years, of relevant industry experience."
    mixed = "Bachelors with 3+ years, or Masters with 0+ years, of relevant industry experience."
    for description in (role, "4+ years in marketing operations, marketing technology engineering.", alternatives):
        job = build_job("1", "Software Engineer", "", "India", "", description, "https://example.com/1")
        assert job["score"] == 0
        assert eligibility_evidence(description)[0]["text"] == description
    assert build_job("2", "Software Engineer", "", "India", "", mixed, "https://example.com/2")["score"] == 55
    assert build_job("3", "Software Engineer", "", "India", "", "Preferred qualifications\n" + role, "https://example.com/3")["score"] == 55
    assert build_job("4", "Software Engineer", "", "India", "", "Our company has 10 years in business.", "https://example.com/4")["score"] == 55
    assert build_job("5", "Software Engineer 1", "", "India", "", "Up to 3 years in software development.", "https://example.com/5")["score"] == 90

    client, store = desk
    job = result(client.post("/api/jobs", json={"company":"Example", "title":"Software Engineer",
                 "url":"https://example.com/saved-experience", "description":role}), 201)
    result(client.patch(f"/api/jobs/{job['id']}", json={"saved":True}))
    store.write("UPDATE jobs SET score=55,reason='Old score' WHERE id=?", (job["id"],))
    store.write("DELETE FROM settings WHERE key='experience_ranking_3'")
    store.initialize(seed=False)
    refreshed = result(client.get(f"/api/jobs/{job['id']}"))
    assert refreshed["score"] == 0 and refreshed["saved"]


def test_posting_requirements_take_precedence_over_general_remote_policy():
    boilerplate = "For US-based roles open to remote work, we hire only in certain states."
    office = "Are able to work in our San Francisco office 5 days/week."
    assert geography("San Francisco Office", "On-site", boilerplate + "\n" + office) == ("other", office)
    assert geography("San Francisco Office", "", boilerplate)[0] == "other"
    assert geography("Remote - US", "", boilerplate)[0] == "us_remote"
    assert build_job("3", "Software Engineer (L3)", "", "India", "Remote",
                     "5-7 years experience writing production-grade code.", "https://sample.example/3")["score"] == 0


def test_smartrecruiters_detail_is_loaded_on_demand_and_failure_is_visible(desk, monkeypatch):
    client, store = desk
    job = saved_job(client)
    store.write("UPDATE companies SET provider='smartrecruiters',board='Sample' WHERE id=?", (job["company_id"],))
    store.write("UPDATE jobs SET external_id='123',description='',location='Remote',region='unclear',evidence='Remote · Remote' WHERE id=?", (job["id"],))
    calls = []
    response = {"id":"123", "active":True, "company":{"identifier":"Sample"}, "jobAd":{"sections":{
        "jobDescription":{"title":"Job Description", "text":"<p>Build APIs.</p><p>Applicants must be based in the United States.</p><p>Must be authorized to work in the United States.</p>"},
        "qualifications":{"title":"Minimum requirements", "text":"<p>3+ years of software engineering experience.</p>"},
    }}}

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=response)

    client_class = httpx.AsyncClient
    monkeypatch.setattr("scout.app.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    detail = result(client.get(f"/api/jobs/{job['id']}"))
    assert detail["details_status"] == "loaded" and "Build APIs." in detail["description"]
    assert detail["eligibility"] == [{"label":"Required", "text":"3+ years of software engineering experience."}]
    assert "3+ years" in detail["detail_assessment"]
    assert detail["detail_location"] == {"region":"us_remote", "evidence":"Applicants must be based in the United States."}
    assert detail["work_authorization"] == ["Must be authorized to work in the United States."]
    assert calls == ["https://api.smartrecruiters.com/v1/companies/Sample/postings/123"]
    assert store.job(job["id"])["description"] == ""
    assert store.job(job["id"])["region"] == "unclear"
    response["id"] = "other"
    assert result(client.get(f"/api/jobs/{job['id']}"))["details_status"] == "unavailable"


def test_location_evidence_includes_required_time_zone():
    requirement = "Must be located in the PST/MST timezone"
    assert geography("Massachusetts - Boston", "remote", requirement) == (
        "unclear", "Massachusetts - Boston · remote · " + requirement)
    assert geography("Remote - US", "", requirement) == ("us_remote", "Remote - US · " + requirement)
    assert geography("Bangalore, India", "", "Meetings are held in the PST timezone.") == ("india", "Bangalore, India")
    assert geography("3 Locations", "", "") == ("unclear", "3 Locations")


def test_existing_jobs_gain_time_zone_evidence(desk):
    client, store = desk
    job = result(client.post("/api/jobs", json={"company": "Example", "title": "Software Engineer",
        "url": "https://example.com/remote", "location": "Remote",
        "description": "Must be located in the PST/MST timezone"}), 201)
    store.write("UPDATE jobs SET evidence='Remote' WHERE id=?", (job["id"],))
    store.write("DELETE FROM settings WHERE key='timezone_evidence_1'")
    store.initialize(seed=False)
    assert "Must be located in the PST/MST timezone" in result(client.get(f"/api/jobs/{job['id']}"))["evidence"]


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
        conn.execute("ALTER TABLE gmail_follow_up_schedule DROP COLUMN reviewed_message_ids")
        conn.execute("ALTER TABLE gmail_follow_up_schedule DROP COLUMN review_pending_ids")
        conn.execute("ALTER TABLE contacts DROP COLUMN profile_url")
        conn.execute("ALTER TABLE contacts DROP COLUMN email")
        conn.execute("ALTER TABLE jobs DROP COLUMN hidden")
        conn.execute("ALTER TABLE jobs DROP COLUMN deadline_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN application_target")
        conn.execute("ALTER TABLE scans DROP COLUMN scope")
        conn.execute("INSERT INTO scans(started_at,state,total) VALUES('2026-01-01','completed',0)")
    store.initialize(seed=False)
    store.initialize(seed=False)
    existing = store.outreach(item["id"])
    assert existing["state"] == "waiting" and existing["draft"] == item["draft"]
    assert existing["scheduled_reviewed_message_ids"] is None and existing["scheduled_review_pending_ids"] is None
    assert existing["follow_up_draft"] == ""
    assert existing["conversation_url"] == ""
    assert existing["profile_url"] == "" and existing["email"] == ""
    assert existing["events"][0]["kind"] == "sent"
    assert store.job(item["job_id"])["hidden"] == 0
    assert store.job(item["job_id"])["application_target"] is None
    assert store.job(item["job_id"])["deadline_at"] is None
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


def test_contact_handoff_keeps_source_and_send_state(desk):
    client, _ = desk
    item = create_draft(client, saved_job(client))
    path = f"/api/outreach/{item['id']}/contact-handoff"
    assert client.patch(path, json={"profile_url": "https://linkedin.com.example/in/jane"}).status_code == 400
    assert client.patch(path, json={"email": "jane@example.com,bob@example.com"}).status_code == 400
    link = "https://www.linkedin.com/in/jane-example/"
    assert result(client.patch(path, json={"profile_url": link}))["profile_url"] == link
    saved = result(client.patch(path, json={"email": "jane+job@example.com"}))
    assert saved["email"] == "jane+job@example.com" and saved["profile_url"] == link
    assert saved["source"] == item["source"] and saved["draft"] == item["draft"]
    assert saved["state"] == "draft" and saved["events"] == []


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
    assert eligibility_evidence("Applications close on November 30, 2026.") == [
        {"label": "Application deadline", "text": "Applications close on November 30, 2026."},
    ]


def test_graduation_year_warning_is_conservative():
    assert graduation_warning("Working towards a B.Tech graduating in 2028.", 2027) == {
        "saved_year":2027, "excerpt":"Working towards a B.Tech graduating in 2028."}
    assert graduation_warning("Class of 2028", 2027)["excerpt"] == "Class of 2028"
    assert graduation_warning("Graduating in 2028.", None) is None
    assert graduation_warning("Graduating in 2027 or 2028.", 2027) is None
    assert graduation_warning("Graduating between December 2026 and June 2028.", 2027) is None
    assert graduation_warning("Graduating 2026 to 2028.", 2027) is None
    assert graduation_warning("Preferred qualifications\nGraduating in 2028.", 2027) is None
    assert graduation_warning("Not graduating in 2028.", 2027) is None
    assert graduation_warning("Must graduate before December 2028.", 2027) is None
    assert graduation_warning("Graduating in 2028. Graduating in 2027.", 2027) is None


def test_profile_year_flags_jobs_and_can_be_cleared(desk):
    client, _ = desk
    job = result(client.post("/api/jobs", json={"company":"Example", "title":"Software Engineer Intern",
                 "url":"https://example.com/intern-year", "description":"Class of 2028"}), 201)
    assert result(client.get(f"/api/jobs/{job['id']}"))["graduation_warning"] is None
    result(client.put("/api/profile", json={"graduation_year":1900}), 422)
    assert result(client.put("/api/profile", json={"graduation_year":2027}))["graduation_year"] == 2027
    assert result(client.get("/api/state"))["graduation_year"] == 2027
    rows = result(client.get("/api/jobs"))["items"]
    assert rows[0]["graduation_warning"] == {"saved_year":2027, "excerpt":"Class of 2028"}
    assert "_description" not in rows[0]
    assert result(client.get(f"/api/jobs/{job['id']}"))["graduation_warning"] == rows[0]["graduation_warning"]
    result(client.put("/api/profile", json={"graduation_year":2028}))
    assert result(client.get(f"/api/jobs/{job['id']}"))["graduation_warning"] is None
    result(client.put("/api/profile", json={"graduation_year":None}))
    assert result(client.get("/api/state"))["graduation_year"] is None


def test_work_authorization_shows_employer_wording_with_caveats(desk):
    paragraph = "Visa sponsorship: We do sponsor visas! However, we cannot sponsor every role or candidate."
    restriction = "Applicants must be authorized to work in the U.S. F-1 students are eligible with CPT."
    irrelevant = "Benefits include an employer-sponsored retirement plan and state-sponsored research."
    export = "Authorization to receive controlled technology without sponsorship for an export license."
    description = "\n".join((irrelevant, paragraph, restriction, export, paragraph))
    assert work_authorization_evidence(description) == [paragraph, restriction]
    client, _ = desk
    job = result(client.post("/api/jobs", json={"company":"Example", "title":"Software Engineer Intern",
                 "url":"https://example.com/intern", "description":description}), 201)
    assert result(client.get(f"/api/jobs/{job['id']}"))["work_authorization"] == [paragraph, restriction]


def test_application_targets_prioritize_outstanding_saved_jobs(desk):
    client, store = desk
    first = saved_job(client)
    second = result(client.post("/api/jobs", json={
        "company": first["company"], "title": "Software Engineer, Intern", "url": "https://sample.example/intern",
    }), 201)
    store.write("UPDATE jobs SET saved=0,deadline_at='2026-11-30' WHERE id=?", (first["id"],))
    first_path = f"/api/jobs/{first['id']}"
    second_path = f"/api/jobs/{second['id']}"
    target = result(client.patch(first_path, json={"application_target": "2026-11-12"}))
    assert target["saved"] and target["deadline_at"] == "2026-11-30"
    assert target["application_target"] == "2026-11-12"
    result(client.patch(second_path, json={"application_target": "2026-11-10"}))
    saved = "/api/jobs?view=saved"
    assert [job["id"] for job in result(client.get(saved))["items"]] == [second["id"], first["id"]]
    result(client.patch(second_path, json={"applied": True}))
    assert [job["id"] for job in result(client.get(saved))["items"]] == [first["id"], second["id"]]
    cleared = result(client.patch(first_path, json={"application_target": None}))
    assert cleared["application_target"] is None and cleared["saved"] and cleared["deadline_at"]


def test_next_actions_include_due_targets_and_actionable_referrals(desk):
    client, store = desk
    due = saved_job(client)
    future = result(client.post("/api/jobs", json={
        "company": due["company"], "title": "Software Engineer, Intern", "url": "https://sample.example/intern",
    }), 201)
    result(client.patch(f"/api/jobs/{due['id']}", json={"application_target": "2026-11-06"}))
    result(client.patch(f"/api/jobs/{future['id']}", json={"application_target": "2026-11-10"}))
    waiting = action(client, create_draft(client, due), "sent")
    store.write("UPDATE outreach SET due_at='2000-01-01T00:00:00+00:00' WHERE id=?", (waiting["id"],))
    draft = create_draft(client, future)
    paused = action(client, create_draft(client, due, "gmail"), "sent")
    endpoint = "/api/next-actions?today=2026-11-07"
    actions = result(client.get(endpoint))
    assert [row["id"] for row in actions["applications"]] == [due["id"]]
    assert [row["id"] for row in actions["referrals"]] == [waiting["id"], draft["id"]]
    assert all("draft" not in row for row in actions["referrals"])
    assert result(client.get(f"/api/outreach/{paused['id']}"))["state"] == "waiting"
    result(client.patch(f"/api/jobs/{due['id']}", json={"applied": True}))
    action(client, waiting, "closed")
    after = result(client.get(endpoint))
    assert after["applications"] == []
    assert [row["id"] for row in after["referrals"]] == [draft["id"]]


def test_verify_posting_distinguishes_presence_absence_and_feed_failure(desk, monkeypatch):
    client, _ = desk
    register(client)
    rows, error, calls = [sample()], None, []

    async def fetch(_client, company, _limit):
        calls.append(company["board"])
        return rows, error

    monkeypatch.setattr(app_module, "fetch_board", fetch)
    full = scan(client)
    listing = result(client.get("/api/jobs"))["items"][0]
    path = f"/api/jobs/{listing['id']}"
    original = result(client.patch(path, json={"saved": True, "applied": True}))
    assert result(client.post(path + "/verify"))["status"] == "listed"
    rows = []
    assert result(client.post(path + "/verify"))["status"] == "not_listed"
    error = "HTTP 503"
    check = result(client.post(path + "/verify"))
    assert check["status"] == "unavailable" and check["detail"] == error and check["checked_at"]
    assert result(client.get(path)) == original
    assert result(client.get("/api/state"))["full_scan"]["id"] == full["id"]
    manual = saved_job(client)
    assert result(client.post(f"/api/jobs/{manual['id']}/verify"))["status"] == "manual"
    assert calls == ["sample"] * 4
