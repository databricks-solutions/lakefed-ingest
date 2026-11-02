"""Create keeps what it was asked for, and who asked, on the job's preparation record, in the job's own transaction,
on the small path and the big one (by database); a record sent in the request's options is never taken. Today's
Create writes every table before it answers, so the job is Ready at once. The Create fixtures of new_job_test and
by_database_test; no app run."""
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import new_job as nj  # noqa: E402
import preparing as p  # noqa: E402
from by_database_test import FAN_IN as BIG_FAN_IN, _create as _create_big, big  # noqa: E402,F401
from new_job_test import FAN_IN, _create, client  # noqa: E402,F401


def _stored(fake) -> dict:
    [schedule] = fake.of("insert into lakefed_ingest.schedule (")
    return json.loads(schedule[8])


def test_a_create_may_not_send_a_preparation_record(client):  # noqa: F811
    forged = p.new_record("mallory@example.com", {}, {}, {}, {}, turn_on=True, state=p.PREPARING, stage=p.READING)
    refused = _create(client, dry_run=False, run={"cadence_minutes": 60, "paused": True, "options": {p.OPTION: forged}})
    assert refused.status_code == 422 and "options: unknown key prepare" in refused.text
    assert client.db.of("insert into lakefed_ingest.schedule (") == []


def test_a_create_records_its_maker_its_settings_and_its_source_and_is_ready(client):  # noqa: F811
    body = _create(client, dry_run=False, tables_per_group=2000).json()
    assert body["created"] is True
    record = _stored(client.db)[p.OPTION]
    assert record["state"] == p.READY and record["maker"] == "op@example.com"
    assert record["settings"] == nj.settings_of(FAN_IN, new_job=True)
    assert record["sizing"] == {"tables_per_group": 2000, "groups_per_server": None}
    assert record["source"] == {"kind": "csv", "source_id": None}
    assert record["turn_on"] is False and record["set_aside"] == {} and record["left_out"] == {}


def test_a_big_create_records_its_upload_and_its_scope(big):  # noqa: F811
    response = _create_big(big)
    assert response.status_code == 202
    big.tasks.run()
    record = _stored(big.db)[p.OPTION]
    assert record["maker"] == "op@example.com" and record["state"] == p.READY
    assert record["source"] == {"kind": "upload", "source_id": "u1"}
    assert record["settings"] == nj.settings_of(BIG_FAN_IN, new_job=True)
    assert record["scope"] == {}
