"""Offline regression tests for durable discovery and the explicit review boundary."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select

from app.api.routes_automation import _serialize_batch, api_cancel_batch, api_pause_batch, api_resume_batch
from app.db.base import utcnow
from app.db.models import Application, Batch, JobPosting, MailboxConnection, OutboxEntry, RecipientHistory, Schedule
from app.services import batch_runner
from app.services.approval import approve_application_for_send
from app.services.dedupe import record_duplicate_override
from app.services.discovery.base import DiscoveredCandidate
from app.services.outbox import claim_next_outbox_entry, enqueue_application_for_send, process_outbox_entry
from app.services.scheduler import dispatch_due_schedules
from app.tasks.maintenance import task_recover_discovery_batches
from app.tasks.pipeline import task_run_batch
from tests.test_vnext_acceptance_suite import _seed_vnext_user


def approve_and_dispatch(db, user, batch, conn, transport):
    """Exercise the normal human approval, queue, claim and fake handoff in send tests."""
    app = db.scalars(select(Application).where(Application.batch_id == batch.id,
                                               Application.transport_status == "draft")).one()
    assert not app.approved_content_hash
    ok, reason = approve_application_for_send(db, user=user, app_rec=app, batch=batch)
    assert ok, reason
    db.commit()
    entry, reason = enqueue_application_for_send(db, user=user, batch=batch, app_record=app, conn=conn,
                                                expected_hash=app.approved_content_hash)
    assert entry, reason
    leased = claim_next_outbox_entry(db, worker_id="offline-test", outbox_id=entry.id)
    assert leased
    return process_outbox_entry(db, entry=leased, transport_override=transport, ignore_window_for_test=True)


def vacancy(idx=1):
    company = f"Oasis{idx}"
    address = f"careers@oasis{idx}.test"
    quote = f"Send your CV to {address}."
    return DiscoveredCandidate(
        source_provider="career_page", source_type="job_post", source_url=f"https://oasis{idx}.test/jobs/{idx}",
        company_name=company, validated_employer=company, company_domain=f"oasis{idx}.test",
        role_extracted="CAFM Administrator", title="CAFM Administrator", location_raw="Dubai, UAE",
        location_confidence="explicit", retrieved_at=utcnow(), posted_at=utcnow() - timedelta(days=1),
        jd_text=f"{company} is hiring a CAFM Administrator in Dubai, UAE. IBM Maximo, SQL and Power BI. {quote}",
        application_email=address, evidence_quote=quote,
    )


def test_fresh_search_revisits_unsent_sources_and_deduplicates_within_run(db_session, monkeypatch):
    from app.db.models import DiscoverySourceSighting
    from app.services.discovery.base import SearchProviderOutcome

    user, *_ = _seed_vnext_user(db_session)
    prior = Batch(user_id=user.id, mode="dry_run", status="completed")
    batch = Batch(user_id=user.id, mode="live", budget_config_json={
        "max_ai_calls": 0, "max_employer_followup_queries": 0, "max_email_corroboration_queries": 0,
    })
    db_session.add_all([prior, batch])
    db_session.flush()
    first_seen = utcnow() - timedelta(days=2)
    pages = {}
    hits = []
    for idx, category in enumerate(("incomplete_evidence", "rejected", "informational_post", "verified_email_vacancy"), 1):
        page = vacancy(idx)
        hit = replace(page, jd_text="CAFM opportunities in Dubai", evidence_quote="", application_email="")
        hits.append(hit)
        pages[hit.source_url] = replace(hit, is_informational_post=True) if idx == 3 else page
        db_session.add(JobPosting(
            user_id=user.id, batch_id=prior.id, canonical_url_hash=batch_runner._hash_str(hit.source_url),
            content_hash="previous-content", discovery_category=category, source_provider="offline",
            source_url=hit.source_url, title=hit.title, jd_text=hit.jd_text, first_seen_at=first_seen,
        ))
    # Tracking variant repeats a search URL. A mirror is distinct until we read the page.
    mirror_url = "https://mirror.test/jobs/cafm"
    hits.extend([replace(hits[0], source_url=hits[0].source_url + "?utm_source=repeat"),
                 replace(hits[0], source_url=mirror_url)])
    pages[mirror_url] = replace(pages[hits[0].source_url], source_url=mirror_url)
    db_session.commit()

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            return SearchProviderOutcome(self.name, query, candidates=hits)

    fetched = []

    async def fetch(url, **kwargs):
        fetched.append(url)
        return pages[url], "fetched"

    async def research(**kwargs):
        # A differently worded informational result from follow-up research only
        # matches the primary posting after employer and role normalization.
        related = replace(pages[hits[2].source_url], source_url="https://research.test/announcement",
                          jd_text="CAFM recruitment announcement in Dubai")
        return [related], [], [], []

    monkeypatch.setattr(batch_runner, "fetch_and_parse_career_page", fetch)
    monkeypatch.setattr(batch_runner, "run_employer_followup_and_corroboration", research)
    batch_runner.execute_batch_pipeline(db_session, batch=batch, search_providers_override=[Provider()],
                                        max_search_queries_override=1, skip_network_dns=True)
    assert sorted(fetched) == sorted(pages)
    primary = batch.sources_searched_json[0]
    assert (primary["results"], primary["duplicates_removed"], primary["unique_added"]) == (6, 1, 5)
    assert batch.checkpoint_json["candidates_processed"] == 6
    assert batch.discovered_count == 4
    assert batch.drafted_count == 3
    assert batch.category_counts_json["informational_post"] == batch.skipped_count == 1
    assert batch.duplicates_removed_count == 3
    assert batch.metrics_json["duplicate_vacancies_skipped"] == 2
    assert "1 informational" in batch.goal_shortfall_reason
    assert "3 duplicate sightings" in batch.goal_shortfall_reason
    assert batch.metrics_json["duplicate_search_hits"] == 1
    duplicates = db_session.scalars(select(DiscoverySourceSighting).filter_by(batch_id=batch.id, is_duplicate=True)).all()
    assert sorted(s.dedupe_reason for s in duplicates) == ["duplicate_in_batch"] * 2 + ["duplicate_normalized_vacancy"]
    current = db_session.scalars(select(JobPosting).filter_by(batch_id=batch.id)).all()
    assert all(p.first_seen_at == first_seen.replace(tzinfo=None) for p in current if p.source_url != mirror_url)
    assert not db_session.scalar(select(OutboxEntry))


def test_republished_content_checks_current_recipient_history_before_drafting(db_session):
    user, *_ = _seed_vnext_user(db_session)
    prior = Batch(user_id=user.id, mode="live", budget_config_json={"max_ai_calls": 0})
    current = Batch(user_id=user.id, mode="live", budget_config_json={"max_ai_calls": 0})
    db_session.add_all([prior, current])
    db_session.commit()
    candidates = [vacancy(i) for i in range(1, 5)]
    known_address = candidates[3].application_email
    db_session.add(RecipientHistory(user_id=user.id, normalized_email=known_address,
                                    display_email=known_address, source="imported"))
    batch_runner.execute_batch_pipeline(db_session, batch=prior, candidate_overrides=candidates, skip_network_dns=True)
    previous = {a.recipient_email: a for a in db_session.scalars(select(Application).filter_by(batch_id=prior.id))}
    assert previous[known_address].primary_skip_code == "PREVIOUSLY_CONTACTED"
    previous[candidates[1].application_email].transport_status = "provider_accepted"
    reserved = candidates[2].application_email
    db_session.add(RecipientHistory(user_id=user.id, normalized_email=reserved, display_email=reserved,
                                    source="reserved", first_application_id=previous[reserved].id))
    db_session.commit()
    # The unsent draft is mirrored at a new URL. The accepted, reserved and rejected
    # postings recur at the original URLs, where current contact checks still apply.
    reposts = [replace(candidates[0], source_url=candidates[0].source_url + "/reposted", canonical_url=""),
               *candidates[1:]]
    batch_runner.execute_batch_pipeline(db_session, batch=current, candidate_overrides=reposts,
                                        dedupe_against_history=True, skip_network_dns=True)
    assert current.discovered_count == 4
    assert current.duplicates_removed_count == 0
    assert current.drafted_count == 1
    assert current.skip_reason_counts_json["PREVIOUSLY_CONTACTED"] == 3
    draft = db_session.scalars(select(Application).filter_by(batch_id=current.id, transport_status="draft")).one()
    assert draft.recipient_email == candidates[0].application_email
    assert not db_session.scalar(select(OutboxEntry))


@pytest.mark.parametrize("legacy_checkpoint", [False, True])
def test_vacancy_identity_survives_worker_restart(db_session, monkeypatch, legacy_checkpoint):
    """Synthetic openings exercise identity and cursor recovery independently of production history."""
    from app.db.models import DiscoverySourceSighting

    user, *_ = _seed_vnext_user(db_session)
    batch = Batch(user_id=user.id, mode="dry_run", budget_config_json={"max_ai_calls": 0})
    db_session.add(batch)
    db_session.commit()
    base = replace(vacancy(), validated_employer="", jd_text="Looking for a CAFM Administrator.",
                   application_email="", evidence_quote="")
    places = ["Dubai, UAE", "Abu Dhabi, UAE", "London, Canada", "London, United Kingdom"]
    # Each source has identical boilerplate. Neither a URL nor text implies a common opening.
    def page(path, location, code="", **changes):
        return replace(base, source_url=f"https://oasis1.test/{path}", location_raw=location, job_code=code, **changes)

    first = page("one", places[0], " alpha-1 ", raw_metadata={"split_role_index": 1})
    candidates = [
        first,
        page("one-mirror", places[1], "ALPHA-1", jd_text="Reworded description"),
        page("one", places[0], "BETA-2", raw_metadata={"split_role_index": 2}),
        *[page(f"site-{i}", place) for i, place in enumerate(places)],
        page("req-375/roundup", places[0], jd_text="Updated location listing", raw_metadata={"split_role_index": 3}),
        page("ref-REQ-765", places[0], "BETA-2", jd_text="Reposted", raw_metadata={"split_role_index": 1}),
        page("text-id", places[0], jd_text="Requisition ID: TXT-300"),
        page("text-id-mirror", places[0], "txt-300", jd_text="Different wording"),
        page("overseas-mirror", places[2], "ALPHA-1", jd_text="Another office advertising the same requisition"),
    ]
    verify = batch_runner.verify_discovered_candidate

    def interrupt(candidate, **kwargs):
        if candidate.job_code != first.job_code:
            raise SystemExit("worker interrupted")
        return verify(candidate, **kwargs)

    monkeypatch.setattr(batch_runner, "verify_discovered_candidate", interrupt)
    with pytest.raises(SystemExit):
        batch_runner.execute_batch_pipeline(db_session, batch=batch, candidate_overrides=candidates, skip_network_dns=True)
    db_session.rollback()
    saved = dict(batch.checkpoint_json)
    before = db_session.scalars(select(JobPosting).filter_by(batch_id=batch.id)).one()
    assert saved["candidates_processed"] == 2 and batch.duplicates_removed_count == 1
    assert saved["seen_content_hashes"] == [before.content_hash]
    assert saved["seen_vacancy_keys"] == [before.normalized_vacancy_key]
    if legacy_checkpoint:
        # Old workers used a location-free comparison and persisted a second, location-aware
        # vacancy key on the row. Neither key incorporated the structured job code.
        legacy_inputs = dict(employer="oasis1", role="cafm administrator", requisition_id="")
        old_key = batch_runner.compute_normalized_vacancy_key(**legacy_inputs, country_code="", city="")
        old_hash = batch_runner._hash_str("cafm administrator|oasis1|looking for a cafm administrator.")
        assert not before.company_name and not before.purported_employer
        before.content_hash, before.requisition_id = old_hash, ""
        before.normalized_vacancy_key = batch_runner.compute_normalized_vacancy_key(
            **legacy_inputs, country_code="AE", city="Dubai")
        saved.pop("vacancy_identity_version", None)
        # The old worker also discarded the mirror. Its committed cursor and counters are authoritative.
        batch.checkpoint_json = {**saved, "seen_content_hashes": [old_hash], "seen_vacancy_keys": [old_key]}
        db_session.commit()

    monkeypatch.setattr(batch_runner, "verify_discovered_candidate", verify)
    batch_runner.execute_batch_pipeline(db_session, batch=batch, skip_network_dns=True)
    rows = db_session.scalars(select(JobPosting).filter_by(batch_id=batch.id)).all()
    assert {(p.city, p.country_code, p.requisition_id) for p in rows} == {
        ("Dubai", "AE", "ALPHA-1"), ("Dubai", "AE", "BETA-2"), ("Dubai", "AE", "TXT-300"),
        ("Dubai", "AE", ""), ("Abu Dhabi", "AE", ""), ("London", "CA", ""), ("London", "GB", ""),
    }
    assert len(rows) == len({p.content_hash for p in rows}) == len({p.normalized_vacancy_key for p in rows}) == 7
    assert batch.discovered_count == batch.metrics_json["normalized_vacancies"] == 7
    assert batch.checkpoint_json["candidates_processed"] == len(candidates)
    duplicates = db_session.scalars(select(DiscoverySourceSighting).filter_by(batch_id=batch.id, is_duplicate=True)).all()
    assert len(duplicates) == batch.duplicates_removed_count == 5
    assert batch.drafted_count == 0 and not db_session.scalar(select(OutboxEntry))


def test_candidate_checkpoint_survives_worker_loss_and_redelivery(db_session, monkeypatch):
    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, mode="live", run_mode="autopilot", discovery_stage="queued")
    db_session.add(b)
    db_session.commit()
    initial = [vacancy(1), vacancy(2)]
    # Simulate worker death after one durable item, before the next evaluation.
    original = batch_runner.verify_discovered_candidate
    calls = []

    def interrupt(cand, **kwargs):
        calls.append(cand.source_url)
        if len(calls) == 2:
            raise SystemExit("simulated lost worker")
        return original(cand, **kwargs)

    monkeypatch.setattr(batch_runner, "verify_discovered_candidate", interrupt)
    from app.tasks import pipeline

    execute = pipeline.execute_batch_pipeline
    monkeypatch.setattr(pipeline, "execute_batch_pipeline", lambda db, **kw: execute(db, candidate_overrides=initial, **kw))
    with pytest.raises(SystemExit):
        task_run_batch.run(b.id)
    db_session.refresh(b)
    assert b.checkpoint_json["candidates_processed"] == 1
    assert b.drafted_count == 1
    assert task_run_batch.run(b.id) == "running"
    b.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert task_recover_discovery_batches.run() == 1
    assert task_run_batch.run(b.id) == "completed"
    db_session.refresh(b)
    assert _serialize_batch(b)["progress_counts"] == {
        "queries_executed": 0, "openings_discovered": 2, "candidates_found": 2, "candidates_processed": 2,
    }
    assert b.drafted_count == 2
    assert calls.count(calls[0]) == 1
    assert calls[1] == calls[2]
    assert db_session.query(JobPosting).count() == db_session.query(Application).count() == 2
    assert {p.date_source for p in db_session.scalars(select(JobPosting))} == {"unknown"}
    assert not db_session.scalar(select(OutboxEntry))
    assert not db_session.scalar(select(RecipientHistory))
    assert all(not a.approved_content_hash for a in db_session.scalars(select(Application)))


def test_pause_resume_and_terminal_replay(db_session):
    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, mode="live", discovery_stage="deferred",
              checkpoint_json={"pasted_candidate": batch_runner.serialize_candidate(vacancy())})
    db_session.add(b)
    db_session.commit()
    assert task_recover_discovery_batches.run() == 0
    assert api_pause_batch(b.id, user, db_session)["status"] == "paused"
    assert task_run_batch.run(b.id) == "paused"
    assert api_resume_batch(b.id, BackgroundTasks(), user, db_session)["status"] == "completed"
    assert b.drafted_count == 1
    assert api_pause_batch(b.id, user, db_session)["status"] == "completed"
    assert api_cancel_batch(b.id, user, db_session)["status"] == "cancelled"
    assert task_run_batch.run(b.id) == "cancelled"
    assert api_resume_batch(b.id, BackgroundTasks(), user, db_session)["status"] == "cancelled"


@pytest.mark.parametrize("format_block", [False, True])
def test_duplicate_override_rechecks_original_vacancy(db_session, format_block):
    user, *_ = _seed_vnext_user(db_session)
    cand = vacancy()
    cand.attachment_requirements = ["docx"] if format_block else []
    b = Batch(user_id=user.id, mode="live")
    db_session.add_all([b, RecipientHistory(user_id=user.id, normalized_email=cand.application_email,
                                            display_email=cand.application_email, source="imported")])
    db_session.commit()
    batch_runner.execute_batch_pipeline(db_session, batch=b, candidate_overrides=[cand], skip_network_dns=True)
    app = db_session.scalars(select(Application)).one()
    identity = app.id, app.job_posting_id
    assert app.primary_skip_code == "PREVIOUSLY_CONTACTED"
    record_duplicate_override(db_session, app=app, reason="Different advertised vacancy")
    batch_runner.reprepare_duplicate_application(db_session, app_rec=app)
    db_session.commit()
    assert (app.id, app.job_posting_id) == identity
    assert db_session.query(Application).count() == 1
    assert not app.approved_content_hash
    assert not db_session.scalar(select(OutboxEntry))
    if format_block:
        assert app.primary_skip_code == "ATTACHMENT_NOT_READY"
        assert b.skipped_count == 1 and b.drafted_count == 0
    else:
        assert app.transport_status == "draft" and app.selected_cv_version_id and app.body_text
        assert b.skipped_count == 0 and b.drafted_count == 1


def test_cancel_releases_only_unclaimed_reservations(db_session):
    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, mode="live")
    conn = MailboxConnection(user_id=user.id, sender_email=user.email, provider="smtp_imap")
    db_session.add_all([b, conn])
    db_session.commit()
    batch_runner.execute_batch_pipeline(db_session, batch=b, candidate_overrides=[vacancy(1), vacancy(2)], skip_network_dns=True)
    apps = db_session.scalars(select(Application).order_by(Application.recipient_email)).all()
    for app, state in zip(apps, ["queued", "leased"], strict=True):
        app.transport_status = "queued"
        db_session.add(OutboxEntry(application_id=app.id, user_id=user.id, mailbox_connection_id=conn.id,
                                   status=state, lease_expires_at=utcnow() + timedelta(minutes=5)))
        db_session.add(RecipientHistory(user_id=user.id, source="reserved", first_application_id=app.id,
                                        normalized_email=app.recipient_email, display_email=app.recipient_email))
    db_session.commit()
    result = api_cancel_batch(b.id, user, db_session)
    assert result["cancelled_outbox_count"] == result["protected_outbox_count"] == 1
    db_session.expire_all()
    assert apps[0].transport_status == "draft"
    assert db_session.scalars(select(RecipientHistory.first_application_id)).all() == [apps[1].id]
    assert set(db_session.scalars(select(OutboxEntry.status))) == {"cancelled", "leased"}


def test_ai_budget_and_same_batch_cc_overlap(db_session, monkeypatch):
    user, *_ = _seed_vnext_user(db_session)
    first, second = vacancy(1), vacancy(2)
    second.cc_emails = [first.application_email.upper()]
    second.jd_text += f" CC {first.application_email}."
    second.evidence_quote += f" CC {first.application_email}."
    calls = []
    generate = batch_runner.generate_grounded_application_draft

    def check_allow_provider(**kwargs):
        calls.append(kwargs["allow_provider"])
        return generate(**kwargs)

    monkeypatch.setattr(batch_runner, "generate_grounded_application_draft", check_allow_provider)
    for _ in range(2):
        b = Batch(user_id=user.id, mode="dry_run", budget_config_json={"max_ai_calls": 0})
        db_session.add(b)
        db_session.commit()
        batch_runner.execute_batch_pipeline(db_session, batch=b, candidate_overrides=[first, second], skip_network_dns=True)
        assert b.drafted_count == 1
        assert b.skip_reason_counts_json["DUPLICATE_RECIPIENT_IN_BATCH"] == 1
    assert calls == [False, False]


def test_demo_preparation_is_offline(db_session, monkeypatch):
    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, mode="demo_fixture")
    db_session.add(b)
    db_session.commit()
    generate = batch_runner.generate_grounded_application_draft
    calls = []

    def check(**kwargs):
        calls.append(kwargs["allow_provider"])
        return generate(**kwargs)

    monkeypatch.setattr(batch_runner, "generate_grounded_application_draft", check)
    batch_runner.execute_batch_pipeline(db_session, batch=b, candidate_overrides=[vacancy()])
    assert calls == [False]


def test_initial_query_without_saved_response_is_not_reissued(db_session, monkeypatch):
    from app.db.models import UsageLedger
    from app.services.discovery.base import SearchProviderOutcome
    from app.services.discovery.query_builder import PlannedSearchQuery

    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, discovery_stage="queued", checkpoint_json={"max_search_queries": 3})
    db_session.add(b)
    db_session.commit()
    queries = [PlannedSearchQuery(q, "direct_email_posts", "AE", "United Arab Emirates", "Dubai", "CAFM", "en")
               for q in ("first", "second", "third")]
    calls = []

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            calls.append(query)
            if len(calls) == 2:
                raise SystemExit("simulated lost worker")
            return SearchProviderOutcome(self.name, query, estimated_cost_usd=0.04)

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", lambda *_: [Provider()])
    monkeypatch.setattr(batch_runner, "build_email_application_discovery_plan", lambda **_: queries)
    with pytest.raises(SystemExit):
        task_run_batch.run(b.id)
    db_session.refresh(b)
    assert _serialize_batch(b)["progress_counts"]["queries_executed"] == 1
    b.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert task_recover_discovery_batches.run() == 1
    db_session.refresh(b)
    assert b.status == "completed"
    assert len(calls) == len(set(calls)) == 3
    assert b.budget_usage_json["search_queries_executed"] == 3
    assert len(b.sources_searched_json) == 2
    assert b.estimated_cost_usd == pytest.approx(0.08)
    assert db_session.query(UsageLedger).filter_by(batch_id=b.id, operation="search_query").count() == 2
    assert len(b.source_errors_json) == 1
    error = b.source_errors_json[0]
    assert (error["query"], error["code"], error["cost_status"]) == (calls[1], "RESEARCH_RESPONSE_UNCERTAIN", "unknown")
    assert "Query regions returning hits: none" in b.coverage_note


def test_filtered_empty_search_plan_never_invokes_provider(db_session, monkeypatch):
    user, _, pref, *_ = _seed_vnext_user(db_session)
    pref.excluded_keywords_json = ["CAFM"]
    b = Batch(user_id=user.id, discovery_stage="queued")
    db_session.add(b)
    db_session.commit()
    plans = []

    def no_plan(**kwargs):
        plans.append(kwargs["excluded_keywords"])
        return []

    class NeverSearch:
        name = "offline"

        async def search(self, *args, **kwargs):
            pytest.fail("Empty plan reached a provider")

    monkeypatch.setattr(batch_runner, "build_email_application_discovery_plan", no_plan)
    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", lambda *_: [NeverSearch()])
    assert task_run_batch.run(b.id) == "completed"
    assert plans == [["CAFM"]]
    db_session.refresh(b)
    assert b.discovered_count == 0 and b.budget_usage_json["search_queries_executed"] == 0

def test_cancellation_during_provider_request_fences_late_results(db_session, monkeypatch):
    from app.db.models import UsageLedger
    from app.db.session import session_scope
    from app.services.discovery.base import SearchProviderOutcome

    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, discovery_stage="queued")
    db_session.add(b)
    db_session.commit()
    calls = []

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            calls.append(query)
            with session_scope() as control:
                api_cancel_batch(b.id, user, control)
            return SearchProviderOutcome(self.name, query, candidates=[vacancy()], estimated_cost_usd=0.07)

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", lambda *_: [Provider()])
    assert task_run_batch.run(b.id) == "cancelled"
    assert len(calls) == 1 and not db_session.scalar(select(JobPosting))
    assert db_session.query(UsageLedger).filter_by(batch_id=b.id).count() == 0
    db_session.refresh(b)
    assert b.estimated_cost_usd == 0 and not b.budget_usage_json.get("search_queries_executed")
    assert task_recover_discovery_batches.run() == 0

@pytest.mark.parametrize("budget", ["monthly", "batch", "calls"])
def test_draft_provider_stops_at_budget(db_session, budget):
    from app.db.models import UsageLedger

    user, _, pref, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, budget_config_json={"max_ai_calls": 1})
    db_session.add(b)
    db_session.flush()
    if budget == "monthly":
        db_session.add(UsageLedger(user_id=user.id, batch_id=b.id, provider="fake", operation="search_query",
                                   units=1, estimated_cost_usd=pref.max_monthly_cost_usd))
    elif budget == "batch":
        b.estimated_cost_usd = pref.max_batch_cost_usd
    db_session.commit()
    calls = []

    class Provider:
        def generate_structured_draft(self, prompt, context):
            calls.append(True)
            raise RuntimeError("mock provider unavailable")

    batch_runner.execute_batch_pipeline(db_session, batch=b, candidate_overrides=[vacancy(1), vacancy(2)],
                                        llm_provider_override=Provider(), skip_network_dns=True)
    assert len(calls) == (1 if budget == "calls" else 0)
    assert b.drafted_count == 2
    assert not db_session.scalar(select(OutboxEntry))


def test_permanent_failure_has_bounded_recovery(db_session, monkeypatch):
    from app.tasks import pipeline

    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, discovery_stage="queued")
    db_session.add(b)
    db_session.commit()

    def fail(*args, **kwargs):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(pipeline, "execute_batch_pipeline", fail)
    assert [task_recover_discovery_batches.run() for _ in range(4)] == [1, 1, 1, 0]
    db_session.refresh(b)
    assert b.status == "failed" and b.finished_at and b.lease_token is None

def test_repeated_lost_worker_stops_at_retry_limit(db_session):
    user, *_ = _seed_vnext_user(db_session)
    b = Batch(user_id=user.id, discovery_stage="discovery_search", status="running", consecutive_failures=2,
              lease_token="dead-worker", lease_expires_at=utcnow() - timedelta(seconds=1))
    db_session.add(b)
    db_session.commit()
    assert task_run_batch.run(b.id) == "failed"
    assert task_recover_discovery_batches.run() == 0
    db_session.refresh(b)
    assert b.consecutive_failures == 3 and b.lease_token is None


@pytest.mark.parametrize("interrupted_stage", ["employer_followup", "email_corroboration"])
def test_paid_research_survives_worker_loss_during_page_fetch(db_session, monkeypatch, interrupted_stage):
    from app.db.models import DiscoverySourceSighting, UsageLedger
    from app.services import research_planner
    from app.services.discovery.base import SearchProviderOutcome

    user, _, pref, *_ = _seed_vnext_user(db_session)
    pref.max_monthly_cost_usd = 0.14
    portal, listing = vacancy(1), vacancy(2)
    portal.application_email = portal.evidence_quote = ""
    portal.portal_apply_detected = True
    official_url = listing.source_url
    listing.source_url = "https://board.test/post/alternate"
    followup = vacancy(3)
    batch = Batch(user_id=user.id, discovery_stage="discovery_search",
                  checkpoint_json={"plan": [], "candidates": [batch_runner.serialize_candidate(c)
                                                               for c in (portal, listing)]},
                  budget_config_json={"max_batch_cost_usd": 0.14})
    db_session.add(batch)
    db_session.commit()
    searches = []
    fetches = []

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            stage = "email_corroboration" if query.startswith("site:") else "employer_followup"
            searches.append(stage)
            hit = vacancy(2) if stage == "email_corroboration" else followup
            return SearchProviderOutcome(self.name, query, candidates=[hit], estimated_cost_usd=0.07)

    async def fetch(url, **kwargs):
        stage = "email_corroboration" if url == official_url else "employer_followup"
        first_attempt = stage not in fetches
        fetches.append(stage)
        if first_attempt and stage == interrupted_stage:
            raise SystemExit("worker lost after the paid provider result")
        return (vacancy(2) if stage == "email_corroboration" else followup), "fetched"

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", lambda *_: [Provider()])
    monkeypatch.setattr(research_planner, "fetch_and_parse_career_page", fetch)
    with pytest.raises(SystemExit):
        task_run_batch.run(batch.id)
    db_session.refresh(batch)
    progress = batch.checkpoint_json["research_progress"]
    assert progress["pending"]["hits"]
    assert progress["plan"][progress["cursor"]]["stage"] == interrupted_stage
    assert batch.budget_usage_json["estimated_cost_usd"] == pytest.approx(len(searches) * 0.07)
    assert db_session.query(UsageLedger).filter_by(batch_id=batch.id, operation="employer_research").count() == len(searches)
    batch.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert task_run_batch.run(batch.id) == "completed"
    db_session.refresh(batch)
    assert searches == ["employer_followup", "email_corroboration"]
    assert fetches.count(interrupted_stage) == 2
    assert batch.budget_usage_json["followup_queries_executed"] == batch.budget_usage_json["corroboration_queries_executed"] == 1
    assert batch.budget_usage_json["pages_fetched"] == 2
    assert batch.estimated_cost_usd == pytest.approx(0.14)
    ledger = db_session.query(UsageLedger).filter_by(batch_id=batch.id, operation="employer_research").all()
    assert [(row.units, row.estimated_cost_usd) for row in ledger] == [(1, 0.07), (1, 0.07)]
    assert db_session.query(JobPosting).filter_by(batch_id=batch.id, source_url=followup.source_url).count() == 1
    verified = db_session.query(JobPosting).filter_by(batch_id=batch.id, source_url=listing.source_url).one()
    assert verified.corroboration_url == official_url
    sightings = db_session.query(DiscoverySourceSighting).filter_by(batch_id=batch.id)
    assert sightings.filter_by(research_stage="employer_followup").count() == 1
    assert sightings.filter_by(research_stage="email_corroboration", corroborated=True).count() == 1


@pytest.mark.parametrize(("paused_status", "reason"), [
    ("quota_paused", "Rolling recipient budget reached"),
    ("paused", "Mailbox authentication failed; please reconnect your mailbox"),
])
@pytest.mark.parametrize("completion", [
    {"discovery_stage": "completed", "checkpoint_json": {"candidates_processed": 2}},
    {"stage_status_json": [{"stage": "eligibility_and_drafting", "status": "completed"}]},
    {"drafted_count": 1},
    {"import_origin": "gemini_zip"},
])
def test_resume_outbox_pause_preserves_completed_discovery(db_session, monkeypatch, completion, paused_status, reason):
    from app.api import routes_automation

    user, *_ = _seed_vnext_user(db_session)
    batch = Batch(user_id=user.id, status=paused_status, finished_at=utcnow(), pause_reason=reason, **completion)
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    finished_at = batch.finished_at
    monkeypatch.setattr(routes_automation, "_dispatch_requested_batch",
                        lambda *_: pytest.fail("Completed discovery dispatched again"))
    for _ in range(2):
        assert api_resume_batch(batch.id, BackgroundTasks(), user, db_session)["status"] == "completed"
    assert batch.discovery_stage == "completed" and batch.finished_at == finished_at
    assert task_recover_discovery_batches.run() == 0


def test_quota_pause_before_search_remains_resumable(db_session, monkeypatch):
    from app.api import routes_automation

    user, *_ = _seed_vnext_user(db_session)
    batch = Batch(user_id=user.id, status="quota_paused", discovery_stage="quota_paused",
                  pause_reason="Monthly estimated provider cost limit reached", finished_at=utcnow())
    db_session.add(batch)
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(routes_automation, "_dispatch_requested_batch",
                        lambda tasks, db, batch: dispatched.append(batch.id))
    assert api_resume_batch(batch.id, BackgroundTasks(), user, db_session)["status"] == "queued"
    assert dispatched == [batch.id]

@pytest.mark.parametrize("interruption", ["lost_response", "replaced_lease"])
def test_in_flight_research_with_unknown_charge_is_not_reissued(db_session, monkeypatch, interruption):
    from app.db.models import UsageLedger
    from app.db.session import session_scope
    from app.services.discovery.base import SearchProviderOutcome

    user, *_ = _seed_vnext_user(db_session)
    target = vacancy()
    target.application_email = target.evidence_quote = ""
    batch = Batch(user_id=user.id, discovery_stage="discovery_search",
                  checkpoint_json={"plan": [], "candidates": [batch_runner.serialize_candidate(target)]})
    db_session.add(batch)
    db_session.commit()
    calls = []

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            calls.append(query)
            if interruption == "lost_response":
                raise SystemExit("worker lost inside the provider request")
            with session_scope() as replacement:
                replacement.get(Batch, batch.id).lease_token = "replacement-worker"
            return SearchProviderOutcome(self.name, query, candidates=[vacancy(2)], estimated_cost_usd=0.07)

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", lambda *_: [Provider()])
    if interruption == "lost_response":
        with pytest.raises(SystemExit):
            task_run_batch.run(batch.id)
    else:
        assert task_run_batch.run(batch.id) == "running"
    db_session.refresh(batch)
    assert batch.checkpoint_json["research_progress"]["pending"]["awaiting_response"]
    batch.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert task_run_batch.run(batch.id) == "completed"
    db_session.refresh(batch)
    assert len(calls) == 1 and batch.budget_usage_json["followup_queries_executed"] == 1
    assert db_session.query(UsageLedger).filter_by(batch_id=batch.id).count() == 0
    assert len(batch.source_errors_json) == 1
    error = batch.source_errors_json[0]
    assert error["code"] == "RESEARCH_RESPONSE_UNCERTAIN" and error["cost_status"] == "unknown"
    assert db_session.query(JobPosting).filter_by(batch_id=batch.id, source_url=vacancy(2).source_url).count() == 0

@pytest.mark.parametrize("provider_available_on_resume", [True, False])
def test_initial_paid_hits_survive_worker_loss_during_fetch(db_session, monkeypatch, provider_available_on_resume):
    from app.db.models import UsageLedger
    from app.services.discovery.base import SearchProviderOutcome
    from app.services.discovery.query_builder import PlannedSearchQuery

    class WorkerLost(BaseException):
        pass

    user, _, pref, *_ = _seed_vnext_user(db_session)
    pref.max_monthly_cost_usd = 0.07
    batch = Batch(user_id=user.id, discovery_stage="queued", checkpoint_json={"max_search_queries": 2},
                  budget_config_json={"max_batch_cost_usd": 0.07, "max_ai_calls": 0})
    db_session.add(batch)
    db_session.commit()
    plans = [PlannedSearchQuery(q, "direct_email_posts", "AE", "United Arab Emirates", "Dubai", "CAFM", "en")
             for q in ("first", "second")]
    searches, fetches = [], []

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            searches.append(query)
            return SearchProviderOutcome(self.name, query, candidates=[vacancy()], estimated_cost_usd=0.07)

    async def fetch(url, **kwargs):
        fetches.append(url)
        if len(fetches) == 1:
            raise WorkerLost()
        return vacancy(), "fetched"

    providers = [Provider()]
    monkeypatch.setattr(batch_runner, "build_email_application_discovery_plan", lambda **_: plans)
    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", lambda *_: providers)
    monkeypatch.setattr(batch_runner, "fetch_and_parse_career_page", fetch)
    with pytest.raises(WorkerLost):
        task_run_batch.run(batch.id)
    db_session.refresh(batch)
    saved = batch.checkpoint_json["initial_query"]
    assert saved["index"] == 0 and saved["query"] == searches[0]
    assert saved["hits"][0]["source_url"] == fetches[0]
    assert batch.budget_usage_json["search_queries_executed"] == 1
    assert batch.budget_usage_json["pages_fetched"] == 0
    assert batch.estimated_cost_usd == pytest.approx(0.07)
    ledger = db_session.query(UsageLedger).filter_by(batch_id=batch.id, operation="search_query")
    assert ledger.one().estimated_cost_usd == pytest.approx(0.07)
    if not provider_available_on_resume:
        providers.clear()
    batch.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()

    assert task_run_batch.run(batch.id) == "completed"
    assert task_run_batch.run(batch.id) == "completed"
    db_session.refresh(batch)
    assert len(searches) == 1 and fetches == [saved["hits"][0]["source_url"]] * 2
    assert batch.budget_usage_json["pages_fetched"] == batch.budget_usage_json["search_queries_executed"] == 1
    assert batch.discovered_count == batch.drafted_count == 1
    assert len(batch.sources_searched_json) == 1 and batch.sources_searched_json[0]["provider"] == "offline"
    assert batch.estimated_cost_usd == pytest.approx(0.07) and ledger.count() == 1


@pytest.mark.parametrize(("mode", "run_mode"), [
    ("demo_fixture", "approval_required"), ("live", "demo_fixture"), ("dry_run", "demo_fixture"),
    ("approval_required", "autopilot"), ("live", "dry_run"), ("demo_fixture", "autopilot"),
])
def test_api_rejects_conflicting_batch_modes(client, mode, run_mode):
    from tests.test_security_and_isolation import _register_and_auth

    _, headers = _register_and_auth(client, "tester@example.com")
    response = client.post("/api/v1/batches", headers=headers,
                           json={"mode": mode, "run_mode": run_mode, "run_immediately": False})
    assert response.status_code == 422
    assert client.get("/api/v1/batches").json()["batches"] == []


def test_fixture_batch_uses_only_offline_sources(db_session, monkeypatch):
    import socket

    from app.services.ai import grounding

    user, *_ = _seed_vnext_user(db_session)
    batch = Batch(user_id=user.id, mode="demo_fixture", run_mode="demo_fixture")
    db_session.add(batch)
    db_session.commit()

    def unexpected(*_args, **_kwargs):
        pytest.fail("An isolated fixture batch tried to use an external service")

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", unexpected)
    monkeypatch.setattr(batch_runner, "sync_gmail_sent_history", unexpected)
    monkeypatch.setattr(grounding, "get_configured_llm_provider", unexpected)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    batch_runner.execute_batch_pipeline(db_session, batch=batch)
    assert batch.status == "completed"
    assert batch.discovered_count > 0
    assert {row["provider"] for row in batch.sources_searched_json} == {"demo_fixture"}
    assert not db_session.scalars(select(OutboxEntry)).first()


def _dispatch_synthetic_schedule(db, user):
    due = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    db.add(Schedule(user_id=user.id, name="Synthetic dry run", days_of_week_json=[due.weekday()]))
    db.commit()
    batch, = dispatch_due_schedules(db, now_utc=due)
    return batch


@pytest.mark.parametrize(("mode", "status"), [
    ("demo_fixture", "completed"), ("dry_run", "completed"), ("live", "cancelled"),
])
def test_worker_global_stop_allows_only_manual_non_sending_modes(db_session, monkeypatch, mode, status):
    user, *_ = _seed_vnext_user(db_session)
    _dispatch_synthetic_schedule(db_session, user)
    user.automation_stopped_at = utcnow()
    batch = Batch(user_id=user.id, mode=mode, discovery_stage="queued", idempotency_key="sched-client-key",
                  checkpoint_json={"pasted_candidate": batch_runner.serialize_candidate(vacancy())}
                  if mode == "dry_run" else {})
    db_session.add(batch)
    db_session.commit()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Stopped manual evaluation called an external service")

    monkeypatch.setattr(batch_runner, "sync_gmail_sent_history", forbidden)
    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", forbidden)
    assert task_run_batch.run(batch.id) == status
    db_session.refresh(batch)
    assert (batch.discovered_count > 0) == (mode != "live")
    assert task_run_batch.run(batch.id) == status
    assert not db_session.scalars(select(OutboxEntry)).first()


@pytest.mark.parametrize("expired_lease", [False, True])
def test_worker_global_stop_rejects_delayed_schedule_at_entry(db_session, monkeypatch, expired_lease):
    user, *_ = _seed_vnext_user(db_session)
    batch = _dispatch_synthetic_schedule(db_session, user)
    user.automation_stopped_at = utcnow()
    if expired_lease:
        batch.status = "running"
        batch.lease_token = "previous-worker"
        batch.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.commit()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Stopped delayed job reached provider setup")

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", forbidden)
    monkeypatch.setattr(batch_runner, "sync_gmail_sent_history", forbidden)
    assert task_run_batch.run(batch.id) == "cancelled"
    db_session.refresh(batch)
    assert batch.pause_reason and batch.lease_token is None
    assert task_run_batch.run(batch.id) == "cancelled"
    assert not db_session.scalar(select(OutboxEntry))


def test_worker_global_stop_fences_scheduled_lease_before_search(db_session, monkeypatch):
    from app.services.discovery.base import SearchProviderOutcome

    user, *_ = _seed_vnext_user(db_session)
    batch = _dispatch_synthetic_schedule(db_session, user)
    # An uncorrelated lease guard could authorize the scheduled row through this one.
    manual = Batch(user_id=user.id, mode="dry_run")
    db_session.add(manual)
    db_session.commit()
    searches = []

    class Provider:
        name = "offline"

        async def search(self, query, **kwargs):
            searches.append(query)
            return SearchProviderOutcome(self.name, query)

    def stop_after_claim(*_args):
        user.automation_stopped_at = utcnow()
        db_session.commit()
        return [Provider()]

    monkeypatch.setattr(batch_runner, "_get_configured_search_providers", stop_after_claim)
    assert task_run_batch.run(batch.id) == "cancelled"
    db_session.refresh(batch)
    assert batch.lease_token is None and not batch.checkpoint_json
    assert searches == []
    assert task_run_batch.run(manual.id, max_search_queries=1) == "completed"
    assert len(searches) == 1
    assert not db_session.scalar(select(OutboxEntry))

