"""Registered single-generation role tasks; adapter supplies frozen parent input."""

import math

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import Model
from inspect_ai.solver import generate

from .inspect import InspectPolicy, generation_config
from .inspect_transport import FixtureAPI


def _role(name, prompt, policy, completion):
    policy = policy or InspectPolicy()
    return Task(
        name=f"draftbench_{name}",
        dataset=[Sample(input=prompt)],
        solver=generate(tool_calls="none"),
        model=Model(
            FixtureAPI(completion=completion), config=generation_config(policy)
        ),
        config=generation_config(policy),
        epochs=1,
        token_limit=policy.reserved_tokens,
        time_limit=math.ceil(policy.timeout_seconds),
        fail_on_error=True,
        continue_on_fail=False,
        metadata={"synthetic": True, "role": name, "live_execution": False},
    )


@task
def writer(
    prompt="Synthetic writer fixture", policy=None, completion="Synthetic fixture"
):
    return _role("writer", prompt, policy, completion)


@task
def reviewer(
    prompt="Synthetic reviewer fixture", policy=None, completion="Synthetic fixture"
):
    return _role("reviewer", prompt, policy, completion)


@task
def revision(
    prompt="Synthetic revision fixture", policy=None, completion="Synthetic fixture"
):
    return _role("revision", prompt, policy, completion)


TASKS = {"writer": writer, "reviewer": reviewer, "revision": revision}
