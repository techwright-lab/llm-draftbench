"""Tests use synthetic data only and prohibit network access."""

import copy
import hashlib
import json
import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network access forbidden in offline tests")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def seal(value):
    payload = {k: v for k, v in value.items() if k != "identity"}
    value["identity"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def case_data():
    producer = {
        "kind": "synthetic",
        "name": {"state": "known", "value": "fixture"},
        "version": {"state": "known", "value": "1"},
        "requested_model": {"state": "inapplicable", "value": None},
        "served_model": {"state": "inapplicable", "value": None},
    }
    rights = {
        "license": "CC0-1.0",
        "usage": "redistributable",
        "authorization": "Synthetic test material created for this project.",
    }
    artifact = {
        "artifact_id": "source-1",
        "file": {"path": "source.txt", "sha256": digest(b"Synthetic source.\n")},
        "rights": rights,
        "producer": producer,
    }
    packet = {
        "basis": "synthetic",
        "prompt_version": "1",
        "messages": [{"role": "user", "content": "Write two ordered synthetic posts."}],
        "evidence_ids": [],
        "draft_ids": [],
        "review_ids": [],
        "producer": producer,
    }
    return seal(
        {
            "schema_version": "1",
            "case_id": "case-1",
            "source_family": "family-1",
            "metadata": {
                key: {"state": "known", "value": value}
                for key, value in {
                    "content_type": "social_thread",
                    "domain": "fiction",
                    "lane": "offline_fixture",
                    "body_format": "plain_text",
                    "source_system": "fixture_exporter",
                    "source_revision": "1",
                }.items()
            },
            "split": "development",
            "rights": rights,
            "source": {"state": "available", "artifact": artifact},
            "generator": {
                "writer": {"state": "available", "input": packet},
                "reviewer": {"state": "unknown", "input": None},
                "revision": {"state": "inapplicable", "input": None},
            },
            "evidence": {"state": "unavailable", "artifacts": []},
            "history": {
                "state": "available",
                "drafts": [
                    {
                        "draft_id": "draft-1",
                        "version": 1,
                        "units": [
                            {
                                "unit_id": "post-1",
                                "kind": "social_post",
                                "content": "First synthetic post.",
                            },
                            {
                                "unit_id": "post-2",
                                "kind": "social_post",
                                "content": "Second synthetic post.",
                            },
                        ],
                        "producer": producer,
                    }
                ],
                "reviews": [],
            },
            "evaluator": {
                "labels": [
                    {
                        "label_id": "label-1",
                        "basis": "synthetic",
                        "draft_id": "draft-1",
                        "question_id": "supported",
                        "question_version": "1",
                        "state": "unknown",
                        "value": None,
                        "producer": producer,
                    }
                ]
            },
        }
    )


@pytest.fixture
def make_suite(tmp_path, case_data):
    suite_rights = copy.deepcopy(case_data["rights"])

    def create(cases=None):
        cases = copy.deepcopy(cases if cases is not None else [case_data])
        (tmp_path / "source.txt").write_bytes(b"Synthetic source.\n")
        body = b"".join((json.dumps(c) + "\n").encode() for c in cases)
        (tmp_path / "cases.jsonl").write_bytes(body)
        suite = seal(
            {
                "schema_version": "1",
                "suite_id": "synthetic-suite",
                "purpose": "synthetic_infrastructure",
                "rights": suite_rights,
                "cases": {"path": "cases.jsonl", "sha256": digest(body)},
            }
        )
        manifest = tmp_path / "suite.json"
        manifest.write_text(json.dumps(suite))
        return manifest

    return create


@pytest.fixture
def campaign(tmp_path_factory):
    from draftbench.campaign import CampaignBudget

    path = tmp_path_factory.mktemp("campaign") / "campaign.sqlite3"
    with CampaignBudget.create(path) as budget:
        yield budget
