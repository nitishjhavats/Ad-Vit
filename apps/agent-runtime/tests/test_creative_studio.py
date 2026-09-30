"""The creative studio: upload a video, get told what is wrong with it.

Four sources feed a rating and the tests keep them apart, because the owner
needs to know which is which:

  * measured   - from the file, via ffmpeg. No model. Tested against a video
                 ffmpeg synthesises, so no fixture file and no real ad.
  * judged     - from the organisation's own `creative_analysis` tier. Tested
                 with a fake router that returns a fixed structured answer, so
                 the arithmetic around the judgement is what is under test.
  * compliance - the same gate that governs a live ad, on the on-screen text,
                 under the WORKSPACE's pack. A Schedule J term must block an
                 ayurveda creative and pass a general one.
  * history    - this account's own linked creatives. "Not enough history"
                 when there is none, never a comparison against nothing.

And the property that runs through all of it: a limitation is stated, not
hidden. No audio was analysed, no ad-library comparison was made, no model was
available - each of those is a sentence in the rating rather than a silent gap.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agents.compliance import ComplianceGate
from app.creative import analyse, frames as fr, storage
from app.creative.rubric import CRITERIA, JUDGED, MEASURED, RUBRIC_VERSION, measure
from app.models.router import Completion
from app.policy.rules import PolicyRuleLoader
from conftest import (
    BROADMATE_WORKSPACE,
    OUTSIDER,
    OWNER,
    RIVAL_WORKSPACE,
    SERVICE_DSN,
    auth,
)

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


# ---------------------------------------------------------------------------
# The rubric
# ---------------------------------------------------------------------------


def test_judged_weights_sum_to_one_hundred():
    assert sum(c.weight for c in JUDGED) == 100


def test_every_judged_criterion_says_what_to_look_for_and_why_it_matters():
    for c in JUDGED:
        assert c.ask, c.key
        assert c.why, c.key
    for c in MEASURED:
        assert not c.ask, f"{c.key} is measured; a model is not asked about it"


def test_the_rubric_is_versioned():
    """A rating is comparable to the rubric it was scored against, not silently
    to a newer one."""
    assert RUBRIC_VERSION


def test_a_landscape_video_fails_the_vertical_gate():
    rows = {m["key"]: m for m in measure(width=1920, height=1080, duration_s=20.0)}
    assert rows["vertical"]["passed"] is False
    assert "letterboxed" in rows["vertical"]["note"]


def test_a_nine_sixteen_video_passes_it():
    rows = {m["key"]: m for m in measure(width=1080, height=1920, duration_s=20.0)}
    assert rows["vertical"]["passed"] is True


def test_length_is_judged_against_the_objective():
    conversion = {m["key"]: m for m in measure(width=1080, height=1920, duration_s=50.0)}
    awareness = {m["key"]: m for m in measure(width=1080, height=1920, duration_s=8.0, objective="awareness")}
    assert conversion["length"]["passed"] is False
    assert awareness["length"]["passed"] is True


def test_unreadable_metadata_is_a_gap_not_a_pass():
    rows = {m["key"]: m for m in measure(width=None, height=None, duration_s=None)}
    assert rows["vertical"]["passed"] is None
    assert rows["length"]["passed"] is None


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def videos():
    tmp = Path(tempfile.mkdtemp(prefix="advit-creative-tests-"))
    return {
        "vertical": fr.synthesize(tmp / "v.mp4", seconds=9.0, with_audio=True, text="ORDER NOW"),
        "landscape": fr.synthesize(tmp / "l.mp4", seconds=3.0, width=960, height=540),
        "short": fr.synthesize(tmp / "s.mp4", seconds=2.0),
    }


def test_a_video_yields_hook_frames_dense_and_body_frames_sparse(videos):
    out = fr.extract(videos["vertical"])
    phases = [f.phase for f in out.frames]
    assert phases[:4] == ["hook"] * 4
    assert "body" in phases
    assert all(f.jpeg[:3] == b"\xff\xd8\xff" for f in out.frames), "not JPEG"


def test_metadata_is_read_from_the_file(videos):
    out = fr.extract(videos["vertical"])
    assert (out.metadata.width, out.metadata.height) == (540, 960)
    assert out.metadata.duration_s == pytest.approx(9.0, abs=0.2)
    assert out.metadata.has_audio is True


def test_the_audio_limitation_is_stated_when_there_is_audio(videos):
    out = fr.extract(videos["vertical"])
    assert any("not analysed" in l for l in out.limitations)


def test_a_short_video_does_not_ask_for_frames_past_its_end(videos):
    out = fr.extract(videos["short"])
    assert all(f.at_s < 2.0 for f in out.frames)
    assert out.frames, "a two-second video still has a hook"


# ---------------------------------------------------------------------------
# The rating, with a fake judge
# ---------------------------------------------------------------------------


class FakeJudge:
    """Returns a fixed structured answer. What is under test is everything
    around the judgement: the weighting, the gaps, the compliance pass."""

    def __init__(self, answer: dict, *, model: str = "stub/judge"):
        self.answer = answer
        self.model = model
        self.calls: list[dict] = []

    def complete_json(self, role, *, system, user, schema, **kw):
        self.calls.append({"role": role, "user": user, "schema": schema})
        return self.answer, Completion(
            text=json.dumps(self.answer), model=self.model, role=role,
            model_class="judgement", tokens_in=1000, tokens_out=300,
            cost_usd=0.02, cost_inr=1.76, latency_ms=900, finish_reason="stop",
        )


def full_answer(on_screen: str = "Order now on WhatsApp", **scores) -> dict:
    default = {c.key: 6 for c in JUDGED}
    default.update(scores)
    return {
        "on_screen_text": on_screen,
        "what_is_sold": "an ayurvedic piles ointment",
        "call_to_action": "message on WhatsApp",
        "criteria": [{"key": k, "score": v, "reason": f"because {k}"} for k, v in default.items()],
        "strongest": "the hook",
        "weakest": "the offer",
        "rewrite": "put the price on screen by second four",
    }


@pytest.fixture
def cur():
    with psycopg.connect(SERVICE_DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as c:
            yield c
        conn.rollback()


def ayurveda_gate() -> ComplianceGate:
    return ComplianceGate(list(PolicyRuleLoader().load("ayurveda")))


def general_gate() -> ComplianceGate:
    return ComplianceGate(list(PolicyRuleLoader().load("general_d2c")))


def test_the_overall_is_weighted_over_the_judged_criteria(videos, cur):
    judge = FakeJudge(full_answer(hook=10, problem_first=0))
    rating = analyse.rate(
        path=videos["vertical"], router=judge, gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    # hook 25*10 + problem_first 15*0 + the rest at 6: (250 + 0 + 60*6) / 100 * 10
    expected = round((25 * 10 + 15 * 0 + 60 * 6) / 100 * 10)
    assert rating.overall == expected
    assert rating.rubric_version == RUBRIC_VERSION
    assert rating.model == "stub/judge"


def test_every_judged_score_carries_its_reason(videos, cur):
    judge = FakeJudge(full_answer())
    rating = analyse.rate(
        path=videos["vertical"], router=judge, gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert all(j.reason for j in rating.judged)


def test_the_judge_is_shown_the_frames_and_the_criteria(videos, cur):
    judge = FakeJudge(full_answer())
    analyse.rate(
        path=videos["vertical"], router=judge, gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    call = judge.calls[0]
    assert call["role"] == "creative_analysis"
    images = [p for p in call["user"] if p.get("type") == "image_url"]
    assert len(images) == 12
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    text = " ".join(p["text"] for p in call["user"] if p.get("type") == "text")
    for c in JUDGED:
        assert c.key in text


def test_a_criterion_the_model_skipped_is_a_gap_not_a_pass(videos, cur):
    """The overall is over what WAS judged, and the gap is named."""
    answer = full_answer()
    answer["criteria"] = [c for c in answer["criteria"] if c["key"] != "trust"]
    judge = FakeJudge(answer)
    rating = analyse.rate(
        path=videos["vertical"], router=judge, gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert "trust" not in {j.key for j in rating.judged}
    assert any("did not score: trust" in l for l in rating.limitations)
    assert rating.overall == 60   # every judged criterion at 6


def test_no_model_means_no_overall_and_says_so(videos, cur):
    """Not a zero, not a fifty. The measured and compliance halves still run
    because they cost nothing; the judged half is reported absent."""
    rating = analyse.rate(
        path=videos["vertical"], router=None, gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert rating.overall is None
    assert rating.judged == []
    assert rating.measured, "the measured half should still run"
    assert any("no model was available" in l for l in rating.limitations)


def test_the_compliance_precheck_runs_under_the_workspaces_own_pack(videos, cur):
    """The defect this repository has already had once, in the chat path: every
    workspace judged under the ayurveda pack. Here the same on-screen text must
    block under ayurveda and pass under general_d2c."""
    text = "Piles ka permanent ilaj guaranteed - order now"

    ayur = analyse.rate(
        path=videos["vertical"], router=FakeJudge(full_answer(on_screen=text)),
        gate=ayurveda_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="ayurveda",
    )
    general = analyse.rate(
        path=videos["vertical"], router=FakeJudge(full_answer(on_screen=text)),
        gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )

    assert ayur.compliance["verdict"] == "block"
    assert any(f["rule"].startswith("IN_") for f in ayur.compliance["findings"]), (
        "the India layer did not fire under the ayurveda pack"
    )
    assert general.compliance["verdict"] != "block" or not any(
        f["rule"].startswith("IN_SCHEDULE_J") for f in general.compliance["findings"]
    )


def test_the_compliance_precheck_says_it_only_saw_the_screen(videos, cur):
    rating = analyse.rate(
        path=videos["vertical"], router=FakeJudge(full_answer()),
        gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert "spoken claims were not analysed" in rating.compliance["checked_against"]


def test_no_on_screen_text_means_nothing_was_checked_and_says_so(videos, cur):
    rating = analyse.rate(
        path=videos["vertical"], router=FakeJudge(full_answer(on_screen="")),
        gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert rating.compliance is None
    assert any("nothing to evaluate" in l for l in rating.limitations)


def test_the_ad_library_comparison_is_declared_unavailable_not_faked(videos, cur):
    """"This hook is what is performing in your category" would be a claim with
    nothing behind it until an approved Marketing API app exists."""
    rating = analyse.rate(
        path=videos["vertical"], router=FakeJudge(full_answer()),
        gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert any("Ad Library" in c for c in rating.comparisons_unavailable)


def test_history_is_honest_when_there_is_none(videos, cur):
    rating = analyse.rate(
        path=videos["vertical"], router=FakeJudge(full_answer()),
        gate=general_gate(), licence_posture=None,
        cur=cur, workspace_id=BROADMATE_WORKSPACE, business_type="general_d2c",
    )
    assert rating.history["available"] is False
    assert "nothing of this account's own" in rating.history["reason"]


# ---------------------------------------------------------------------------
# The API, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """NOT `with TestClient(app)`.

    The context-manager form runs the app's lifespan, and the lifespan's exit
    calls close_pools() - so the first test file alphabetically after this one
    would find the session-scoped pools closed and every database test in it
    would fail with PoolsNotOpen. That happened. The rest of this suite
    constructs the client bare, with the pools owned by conftest, and so does
    this.
    """
    from app.main import app

    return TestClient(app)


def _storage_client() -> httpx.Client:
    from app.config import get_settings

    s = get_settings()
    return httpx.Client(
        base_url=f"{s.supabase_url.rstrip('/')}/storage/v1",
        headers={"Authorization": f"Bearer {s.supabase_service_role_key}",
                 "apikey": s.supabase_service_role_key},
        timeout=30,
    )


@pytest.fixture
def scrub_creatives():
    """Rows by SQL; objects through the Storage API. Supabase's
    storage.protect_delete() refuses a direct DELETE on storage.objects so an
    object cannot be orphaned by accident, and that applies to test cleanup
    too - the runtime has no delete path of its own by design."""

    def _scrub():
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("delete from t_advit.creatives where original_name like 'studio-test-%'")
            conn.commit()
        with _storage_client() as http:
            listing = http.post("/object/list/creatives", json={"prefix": "", "limit": 1000})
            names = []
            for entry in listing.json() if listing.status_code == 200 else []:
                # top-level entries are workspace folders; list inside each
                sub = http.post("/object/list/creatives", json={"prefix": entry["name"], "limit": 1000})
                names += [f"{entry['name']}/{o['name']}" for o in (sub.json() if sub.status_code == 200 else [])]
            if names:
                http.request("DELETE", "/object/creatives", json={"prefixes": names})

    _scrub()
    yield
    _scrub()


def test_declaring_an_upload_writes_the_row_and_signs_a_url(client, scrub_creatives):
    r = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/uploads",
        json={"original_name": "studio-test-a.mp4", "content_type": "video/mp4", "size_bytes": 1234},
        headers=auth(OWNER),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload"]["method"] == "PUT"
    assert "/storage/v1/object/upload/sign/creatives/" in body["upload"]["url"]
    assert body["upload"]["url"].split("/creatives/")[1].startswith(BROADMATE_WORKSPACE), (
        "the object path does not start with the workspace id, so the storage "
        "policies would not scope it"
    )

    listed = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives", headers=auth(OWNER)).json()
    mine = [c for c in listed if c["id"] == body["creative_id"]]
    assert mine and mine[0]["status"] == "uploaded"


def test_a_stranger_cannot_declare_an_upload_into_another_workspace(client, scrub_creatives):
    r = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/uploads",
        json={"original_name": "studio-test-x.mp4", "content_type": "video/mp4", "size_bytes": 1},
        headers=auth(OUTSIDER),
    )
    assert r.status_code == 404


def test_analysing_before_uploading_is_refused(client, scrub_creatives):
    declared = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/uploads",
        json={"original_name": "studio-test-b.mp4", "content_type": "video/mp4", "size_bytes": 1},
        headers=auth(OWNER),
    ).json()
    r = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/{declared['creative_id']}/analyse",
        json={}, headers=auth(OWNER),
    )
    assert r.status_code == 409
    assert "not been uploaded" in r.json()["detail"]


def test_the_whole_flow_with_no_key_on_file(client, scrub_creatives, videos):
    """Declare, upload the real bytes to the signed URL, analyse. The test
    organisation holds no OpenRouter key, so the judged half is reported absent
    and the measured and compliance halves still land on the row."""
    declared = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/uploads",
        json={"original_name": "studio-test-c.mp4", "content_type": "video/mp4",
              "size_bytes": videos["vertical"].stat().st_size},
        headers=auth(OWNER),
    ).json()

    put = httpx.put(
        declared["upload"]["url"],
        content=videos["vertical"].read_bytes(),
        headers=declared["upload"]["headers"],
        timeout=60,
    )
    assert put.status_code == 200, put.text

    r = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/{declared['creative_id']}/analyse",
        json={"objective": "conversion"}, headers=auth(OWNER),
    )
    assert r.status_code == 200, r.text
    rating = r.json()["rating"]

    assert rating["overall"] is None
    assert {m["key"] for m in rating["measured"]} == {"vertical", "length"}
    assert any("OpenRouter" in l or "no model" in l for l in rating["limitations"])

    row = client.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/{declared['creative_id']}", headers=auth(OWNER)
    ).json()
    assert row["status"] == "analysed"
    assert row["rating_json"]["rubric_version"] == RUBRIC_VERSION


def test_a_stranger_cannot_read_a_creative_or_its_rating(client, scrub_creatives):
    declared = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/uploads",
        json={"original_name": "studio-test-d.mp4", "content_type": "video/mp4", "size_bytes": 1},
        headers=auth(OWNER),
    ).json()
    r = client.get(
        f"/api/workspaces/{RIVAL_WORKSPACE}/creatives/{declared['creative_id']}", headers=auth(OUTSIDER)
    )
    assert r.status_code == 404
    listed = client.get(f"/api/workspaces/{RIVAL_WORKSPACE}/creatives", headers=auth(OUTSIDER)).json()
    assert declared["creative_id"] not in {c["id"] for c in listed}


def test_the_rubric_is_served_beside_the_rating(client):
    r = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/creatives/rubric", headers=auth(OWNER))
    assert r.status_code == 200
    assert r.json()["version"] == RUBRIC_VERSION
    assert {c["key"] for c in r.json()["criteria"]} == {c.key for c in CRITERIA}


# ---------------------------------------------------------------------------
# Storage: the path is the tenancy
# ---------------------------------------------------------------------------


def test_a_tenant_cannot_read_another_workspaces_object():
    """The storage policy reads the first path segment through
    is_workspace_member. Checked at the SQL layer, which is what the Storage
    service evaluates."""
    from conftest import claims_for

    with psycopg.connect(SUPERUSER_DSN) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """insert into storage.objects (bucket_id, name, owner)
                   values ('creatives', %s, %s::uuid)""",
                (f"{BROADMATE_WORKSPACE}/{uuid.uuid4()}.mp4", OWNER),
            )
            cur.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims_for(OUTSIDER)),))
            cur.execute("set local role authenticated")
            cur.execute(
                "select count(*) from storage.objects where bucket_id = 'creatives' and name like %s",
                (f"{BROADMATE_WORKSPACE}/%",),
            )
            assert cur.fetchone()[0] == 0
            cur.execute("reset role")
            cur.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims_for(OWNER)),))
            cur.execute("set local role authenticated")
            cur.execute(
                "select count(*) from storage.objects where bucket_id = 'creatives' and name like %s",
                (f"{BROADMATE_WORKSPACE}/%",),
            )
            assert cur.fetchone()[0] == 1
        conn.rollback()


def test_an_object_path_never_comes_from_the_caller():
    """The workspace comes from an AuthorizedWorkspace and the creative id from
    a row the runtime wrote. There is no argument through which a caller can
    name a path."""
    import inspect

    from app import routes_creative

    src = inspect.getsource(routes_creative.declare_upload)
    assert "storage.object_path(ws.id, creative_id," in src
    assert "payload.path" not in src and "payload.asset_ref" not in src
