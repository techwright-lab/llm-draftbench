"""Deterministic infrastructure fixtures, never measured model output."""

import copy
import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..identity import canonical_bytes
from ..models import Contract, Digest, PublicationUnit, Role, Text


class AdapterLimited(Exception):
    """A known synthetic adapter limit, not a provider billing claim."""


class FakeResult(Contract):
    schema_version: Literal["1"]
    adapter: Literal["fake"]
    adapter_version: Literal["1"]
    synthetic: Literal[True]
    role: Role
    request_digest: Digest
    parent_digests: dict[str, Digest]
    units: Annotated[list[PublicationUnit], Field(max_length=256)]
    feedback: Text | None

    @model_validator(mode="after")
    def shape(self):
        keys = {
            "writer": set(),
            "reviewer": {"draft"},
            "revision": {"draft", "review"},
        }
        if set(self.parent_digests) != keys[self.role]:
            raise ValueError("invalid_parent_bindings")
        if self.role == "reviewer":
            if self.units or self.feedback is None:
                raise ValueError("invalid_review_output")
        elif not self.units or self.feedback is not None:
            raise ValueError("invalid_draft_output")
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("duplicate_unit")
        return self


class FakeAdapter:
    def invoke(self, request: dict) -> dict:
        digest = hashlib.sha256(canonical_bytes(request)).hexdigest()
        role = request["role"]
        feedback = None
        if role == "writer":
            units = [
                {
                    "unit_id": f"unit-{index}",
                    "kind": "social_post",
                    "content": f"Synthetic infrastructure draft unit {index}. Request {digest}.",
                }
                for index in (1, 2)
            ]
        elif role == "reviewer":
            units = []
            feedback = (
                "Synthetic critique fixture. Not an editorial or factual assessment."
            )
        elif role == "revision":
            units = copy.deepcopy(request["parents"]["draft"]["units"])
            for unit in units:
                unit["content"] += " Synthetic revision fixture."
        else:
            raise ValueError("invalid_role")
        return {
            "schema_version": "1",
            "adapter": "fake",
            "adapter_version": "1",
            "synthetic": True,
            "role": role,
            "request_digest": digest,
            "parent_digests": copy.deepcopy(request["parent_digests"]),
            "units": units,
            "feedback": feedback,
        }
