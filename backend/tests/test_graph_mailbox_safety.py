"""Graph polling, verified identities and follow-up gates using only mock HTTP responses."""
import base64
from datetime import timedelta
from email import message_from_bytes, policy

import httpx
import pytest

from app.core.encryption import encrypt_json
from app.db.base import as_utc, utcnow
from app.db.models import MessageEvent
from app.services import followup, inbox_processor
from app.services.mailbox import ms_graph
from app.services.mailbox.base import HandoffOutcome, OutboundEmailMessage
from app.services.mailbox.ms_graph import MsGraphMailboxTransport
from tests.test_mailbox_history_and_followup_review import sent  # noqa: F401

DELTA = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages/delta"


@pytest.fixture
def graph(db_session, sent):  # noqa: F811
    user, conn, app = sent
    conn.provider = "ms_graph_oauth"
    conn.granted_scopes_json = ["Mail.Read", "Mail.Send"]
    conn.encrypted_credentials = encrypt_json({"access_token": "mock", "expires_at": (utcnow() + timedelta(days=1)).isoformat()})
    conn.inbox_cursor = (utcnow() - timedelta(days=1)).isoformat()
    db_session.commit()
    return conn, app


def message(n, ref=""):
    return {"id": f"graph-{n}", "conversationId": f"conversation-{n}", "internetMessageId": f"<{n}@org.test>",
            "internetMessageHeaders": [{"name": "In-Reply-To", "value": ref}] if ref else [],
            "from": {"emailAddress": {"address": "hr@org.test"}}, "toRecipients": [],
            "receivedDateTime": "2026-11-20T09:00:00Z", "subject": "Application", "bodyPreview": "Could we schedule an interview?"}


def test_delta_paging_after_failure_and_rfc_correlation(db_session, graph, monkeypatch):
    conn, app = graph
    old = conn.inbox_cursor
    failure = True
    next_url = DELTA + "?$skiptoken=two"
    complete = DELTA + "?$deltatoken=finished"
    def handler(request):
        assert request.headers["Authorization"] == "Bearer mock"
        if str(request.url) == next_url:
            return httpx.Response(503) if failure else httpx.Response(200, json={
                "value": [message(26, app.rfc_message_id)], "@odata.deltaLink": complete})
        return httpx.Response(200, json={"value": [message(i) for i in range(26)], "@odata.nextLink": next_url})
    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler))
    monkeypatch.setattr(inbox_processor, "get_mailbox_transport", lambda *_: transport)
    assert inbox_processor.poll_mailbox_connection(db_session, conn) == 0
    assert conn.inbox_cursor == old and conn.inbox_health_status == "error"
    failure = False
    assert inbox_processor.poll_mailbox_connection(db_session, conn) == 1
    assert conn.inbox_cursor == "graph-delta:" + complete and conn.inbox_health_status == "healthy"
    assert app.response_status == "human_reply" and app.outcome_status == "interview"


@pytest.mark.parametrize("data", [
    {"value": []},
    {"value": [], "@odata.nextLink": "https://other.test/v1.0/me/mailFolders/Inbox/messages/delta"},
    {"value": [None], "@odata.deltaLink": DELTA + "?$deltatoken=end"},
])
def test_incomplete_graph_response_never_marks_inbox_fresh(db_session, graph, monkeypatch, data):
    conn, app = graph
    old = conn.inbox_cursor
    def handler(request):
        assert request.url.host == "graph.microsoft.com"
        return httpx.Response(200, json=data)
    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler))
    monkeypatch.setattr(inbox_processor, "get_mailbox_transport", lambda *_: transport)
    inbox_processor.poll_mailbox_connection(db_session, conn)
    assert conn.inbox_health_status == "error" and conn.inbox_cursor == old


@pytest.mark.parametrize("found", [True, False])
def test_send_uses_mime_and_only_verified_sent_identifiers(db_session, graph, found):
    conn, app = graph
    outbound = OutboundEmailMessage(from_email=conn.sender_email, from_name="Sender", to_email=app.recipient_email,
        cc_emails=[], subject="Re: Analyst", body_text="Following up", rfc_message_id="<second@sender.test>",
        in_reply_to=app.rfc_message_id, references=app.rfc_message_id, custom_action_id="action-one")
    def handler(request):
        if request.method == "POST":
            mime = message_from_bytes(base64.b64decode(request.content), policy=policy.default)
            assert mime["Message-ID"] == outbound.rfc_message_id and mime["In-Reply-To"] == app.rfc_message_id
            assert mime["X-ApplyFlow-Action-ID"] == "action-one"
            return httpx.Response(202)
        assert "sentitems" in request.url.path.lower()
        item = {"id": "real-graph-id", "conversationId": "real-thread", "internetMessageId": outbound.rfc_message_id,
                "from": {"emailAddress": {"address": conn.sender_email}}, "isDraft": False,
                "internetMessageHeaders": [{"name": "X-ApplyFlow-Action-ID", "value": "action-one"}]}
        return httpx.Response(200, json={"value": [item]}) if found else httpx.Response(503)
    result = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler)).send_message(outbound)
    assert result.outcome == HandoffOutcome.PROVIDER_ACCEPTED
    assert result.provider_message_id == ("real-graph-id" if found else "")
    assert result.provider_thread_id == ("real-thread" if found else "")


@pytest.mark.parametrize("identity", ["no_thread", "legacy_fake", "no_provider", "no_ids", "legacy_poll", "no_scope"])
def test_manual_inbox_confirmation_for_incomplete_graph_evidence(db_session, graph, identity):
    conn, app = graph
    conn.inbox_cursor = "graph-delta:" + DELTA + "?$deltatoken=ok"
    if identity == "no_thread":
        app.provider_thread_id = ""
    if identity == "legacy_fake":
        app.provider_message_id = app.rfc_message_id
    if identity == "no_ids":
        app.rfc_message_id = app.provider_message_id = app.provider_thread_id = ""
    if identity == "no_provider":
        app.provider_message_id = ""
    if identity == "legacy_poll":
        conn.inbox_cursor = utcnow().isoformat()
    if identity == "no_scope":
        conn.granted_scopes_json = ["Mail.Send"]
    db_session.commit()
    assert followup.evaluate_and_queue_followups_for_user(db_session, user_id=app.user_id)[0] == 0
    followup.prepare_followup(db_session, app=app, manual=True)
    view = followup.followup_view(db_session, app)
    assert view["confirm_inbox_required"]
    with pytest.raises(ValueError, match="Check the original mailbox"):
        followup.approve_followup(db_session, app=app, draft_id=view["id"], confirm_inbox=False)
    assert followup.approve_followup(db_session, app=app, draft_id=view["id"], confirm_inbox=True).status == "queued"



def test_partial_resume_and_approval_handoff_recheck(db_session, graph, monkeypatch):
    conn, app = graph
    prior_time = as_utc(conn.inbox_last_polled_at)
    next_link = DELTA.replace("/Inbox", "('opaque-folder')") + "?$skiptoken=resume"
    final = DELTA + "?$deltatoken=complete"
    expired = DELTA + "?$deltatoken=expired"
    urls = []

    def handler(request):
        urls.append(str(request.url))
        assert 'IdType="ImmutableId"' in request.headers["Prefer"]
        if str(request.url) == expired:
            return httpx.Response(410)
        if str(request.url) == next_link:
            return httpx.Response(200, json={"value": [{"id": "removed", "@removed": {"reason": "deleted"}}],
                                             "@odata.deltaLink": final})
        return httpx.Response(200, json={"value": [], "@odata.nextLink": next_link})

    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler))
    monkeypatch.setattr(inbox_processor, "get_mailbox_transport", lambda *_: transport)
    conn.inbox_cursor = "graph-delta:" + expired
    db_session.commit()
    monkeypatch.setattr(ms_graph, "MAX_DELTA_PAGES", 2)
    inbox_processor.poll_mailbox_connection(db_session, conn)
    assert urls[0] == expired and "$select" in httpx.URL(urls[1]).params
    assert conn.inbox_cursor == "pending:graph:" + next_link and conn.inbox_health_status == "partial"
    assert as_utc(conn.inbox_last_polled_at) == prior_time
    assert followup.evaluate_and_queue_followups_for_user(db_session, user_id=app.user_id)[0] == 0

    inbox_processor.poll_mailbox_connection(db_session, conn)
    assert urls[-1] == next_link and conn.inbox_cursor == "graph-delta:" + final
    assert followup.evaluate_and_queue_followups_for_user(db_session, user_id=app.user_id)[0] == 1
    draft = followup.followup_view(db_session, app)
    assert not draft["confirm_inbox_required"]
    entry = followup.approve_followup(db_session, app=app, draft_id=draft["id"], confirm_inbox=False)
    assert followup.approved_followup_payload(db_session, app, entry, conn)["parent"] == app.rfc_message_id
    conn.inbox_health_status = "error"
    inbox_processor.ingest_inbound_messages(db_session, conn=conn, records=[])
    with pytest.raises(ValueError, match="Inbox check expired"):
        followup.approved_followup_payload(db_session, app, entry, conn)


@pytest.mark.parametrize("problem", ["wrong_sender", "wrong_action", "wrong_rfc", "duplicate", "paged", "draft"])
def test_unverified_sent_lookup_cannot_reconcile(db_session, graph, problem):
    conn, app = graph
    item = {"id": "graph-id", "from": {"emailAddress": {"address": conn.sender_email}},
            "internetMessageId": app.rfc_message_id, "isDraft": False,
            "internetMessageHeaders": [{"name": "x-applyflow-action-id", "value": "action"}]}
    data = {"value": [item]}
    if problem == "wrong_sender":
        item["from"]["emailAddress"]["address"] = "another@sender.test"
    elif problem == "wrong_action":
        item["internetMessageHeaders"][0]["value"] = "another-action"
    elif problem == "wrong_rfc":
        item["internetMessageId"] = "<different@sender.test>"
    elif problem == "duplicate":
        data["value"].append(dict(item, id="other"))
    elif problem == "paged":
        data["@odata.nextLink"] = DELTA
    else:
        item["isDraft"] = True
    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=data)))
    assert transport.reconcile_sent_message(app.rfc_message_id, "action") is None


def test_hydrated_reply_in_followup_conversation(db_session, graph, monkeypatch):
    conn, app = graph
    db_session.add(MessageEvent(user_id=app.user_id, application_id=app.id, event_type="followup_sent",
                                direction="outbound", rfc_message_id="<followup@sender.test>", provider_message_id="sent-id",
                                details_json={"provider_thread_id": "conversation-42"}))
    db_session.commit()

    def handler(request):
        if request.url.path.endswith("/messages/graph-42"):
            return httpx.Response(200, json=message(42))
        return httpx.Response(200, json={"value": [{"id": "graph-42"}], "@odata.deltaLink": DELTA + "?$deltatoken=end"})

    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler))
    monkeypatch.setattr(inbox_processor, "get_mailbox_transport", lambda *_: transport)
    assert inbox_processor.poll_mailbox_connection(db_session, conn) == 1
    assert app.response_status == "human_reply"

def test_partial_http_response_cannot_finish_round(db_session, graph, monkeypatch):
    conn, app = graph
    cursor = conn.inbox_cursor
    partial = {"value": [], "@odata.deltaLink": DELTA + "?$deltatoken=end"}
    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(
        lambda request: httpx.Response(206, json=partial)))
    monkeypatch.setattr(inbox_processor, "get_mailbox_transport", lambda *_: transport)
    inbox_processor.poll_mailbox_connection(db_session, conn)
    assert conn.inbox_cursor == cursor and conn.inbox_health_status == "error"


def test_send_only_acceptance_requires_no_read_request(db_session, graph):
    conn, app = graph
    conn.granted_scopes_json = ["Mail.Send"]
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(202)

    outbound = OutboundEmailMessage(from_email=conn.sender_email, from_name="", to_email=app.recipient_email,
        cc_emails=[], subject="Application", body_text="Hello", rfc_message_id=app.rfc_message_id)
    transport = MsGraphMailboxTransport(db_session, conn, http_transport=httpx.MockTransport(handler))
    result = transport.send_message(outbound)
    assert result.outcome == HandoffOutcome.PROVIDER_ACCEPTED and not result.provider_message_id
    outbound.from_email = "other@sender.test"
    assert transport.send_message(outbound).outcome == HandoffOutcome.PRE_HANDOFF_FAILURE
    assert requests == ["POST"]
