"""Explicit operator launcher. Prepare/preflight are offline; only run dispatches.

Operator approval is a local attestation, not authentication against the same
user. No data file can grant approval: run requires the exact reviewed digest.
"""

import hashlib
from decimal import Context, Decimal, localcontext
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

from .adapters import replay_contract as replay
from .adapters.pilot_policy import RATES
from .adapters.provider_contract import digest, native_request, parse_policy
from .campaign import CampaignBudget
from .identity import canonical_bytes, strict_json_loads
from .loader import load_suite
from .provider_reporting import snapshot_provider
from .reporting import read_json
from .workflow import _directory, _fsync, _run_lock

MODELS = ("gpt-6-luna", "gpt-6-sol", "claude-sonnet-5")
FORMAT = "draftbench-pilot-plan-v2"
CALLS_PER_MODEL = 4
MAX_CALLS = CALLS_PER_MODEL * len(MODELS)
REPLAY_DIRECTORY = "review-replay"


def code_identity():
    root = Path(__file__).parent
    return digest(
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.py"))
        }
    )


def runtime_dependencies():
    """Bind installed draftbench[pilot]'s active Requires-Dist closure.

    Extras belong to the requiring edge, not to the whole environment. Visit
    base and each requested extra separately, including extras discovered after
    an earlier visit; cycles terminate without losing late optional edges.
    Never enumerate unrelated installed distributions or fall back to a list.
    """
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    pending = [Requirement("draftbench[pilot]")]
    installed, versions, visited = {}, {}, set()
    environment = default_environment()
    try:
        while pending:
            requirement = pending.pop()
            name = canonicalize_name(requirement.name)
            if requirement.url:
                # A version cannot attest an arbitrary direct-URL requirement.
                raise ValueError("unverifiable_dependency_url")
            if name not in installed:
                dist = distribution(name)
                if canonicalize_name(dist.metadata["Name"], validate=True) != name:
                    raise ValueError("dependency_name_mismatch")
                installed[name] = dist
                versions[name] = version(name)
                Version(versions[name])
            dist = installed[name]
            if not requirement.specifier.contains(versions[name], prereleases=True):
                raise ValueError("unsatisfied_dependency")
            extras = {canonicalize_name(extra) for extra in requirement.extras}
            provided = {
                canonicalize_name(extra)
                for extra in dist.metadata.get_all("Provides-Extra", [])
            }
            if not extras <= provided:
                raise ValueError("unknown_dependency_extra")
            for extra in sorted({""} | extras):
                if (name, extra) in visited:
                    continue
                visited.add((name, extra))
                for raw in dist.requires or []:
                    child = Requirement(raw)
                    if child.marker is None or child.marker.evaluate(
                        {**environment, "extra": extra}
                    ):
                        pending.append(child)
    except (PackageNotFoundError, ValueError, TypeError, KeyError, OSError) as exc:
        raise ValueError("pilot_dependencies_unresolvable") from exc
    return dict(sorted(versions.items()))


def _total(policies):
    # Fixed three-model rates/caps need far fewer than 80 digits; do not inherit
    # a calling application's low Decimal precision or rounding/trap settings.
    with localcontext(Context(prec=80)):
        return sum((p.reservation_total(CALLS_PER_MODEL) for p in policies), Decimal(0))


def _policies(config, tariff):
    if (
        set(config) != {"format", "account_route", "organization", "project"}
        or config["format"] != "article-pilot-v2"
    ):
        raise ValueError("invalid_pilot_config")
    if set(tariff) != {
        "format",
        "reviewed",
        "reviewed_by",
        "reviewed_at",
        "sources",
        "account_route",
        "currency",
        "rates",
        "scope",
    }:
        raise ValueError("invalid_tariff_attestation")
    if (
        tariff["format"] != "operator-tariff-attestation-v1"
        or type(tariff["reviewed"]) is not bool
        or tariff["account_route"] != config["account_route"]
        or tariff["currency"] != "USD"
        or tariff["rates"] != {m: list(RATES[m]) for m in MODELS}
        or not isinstance(tariff["sources"], list)
        or not tariff["sources"]
        or any(
            type(s) is not str or not s.startswith("https://")
            for s in tariff["sources"]
        )
        or any(
            type(tariff[k]) is not str or not tariff[k].strip()
            for k in ("reviewed_by", "reviewed_at", "scope")
        )
    ):
        raise ValueError("invalid_tariff_attestation")
    if tariff["reviewed"] and any(
        "REPLACE" in tariff[k] for k in ("reviewed_by", "reviewed_at", "scope")
    ):
        raise ValueError("unreviewed_tariff")
    policies = []
    for model in MODELS:
        policy = dict(
            model=model,
            account_route=config["account_route"],
            currency="USD",
            pricing_provenance="public-docs-2026-09-23-v1",
            verified_tariff_digest=digest(tariff) if tariff["reviewed"] else None,
            max_cost="50",
            max_output_tokens=replay.MAX_OUTPUT_TOKENS,
            max_requests=CALLS_PER_MODEL,
            max_total_tokens=4100000,
            max_input_bytes=100000,
            timeout_seconds=300,
        )
        if model.startswith("gpt-"):
            policy.update(
                contract="openai-gpt6-text-v1",
                organization=config["organization"],
                project=config["project"],
                reasoning_effort=replay.REASONING_EFFORT,
            )
        else:
            policy.update(
                contract="anthropic-messages-text-v1",
                effort=replay.REASONING_EFFORT,
                thinking="adaptive",
            )
        p = parse_policy(policy)
        p.admit(CALLS_PER_MODEL)
        policies.append(p)
    if _total(policies) > Decimal("50"):
        raise ValueError("campaign_too_small")
    return policies


def _scope(paths, root, campaign):
    from .provider_workflow import approval_scope

    config, tariff = read_json(paths["config"]), read_json(paths["tariff"])
    policies = _policies(config, tariff)
    loaded = load_suite(paths["suite"])
    if len(loaded.cases) != 1 or any(
        getattr(loaded.cases[0].generator, role).input is None
        for role in ("writer", "reviewer", "revision")
    ):
        raise ValueError("one_complete_case_required")
    # Schemas and request settings are pinned from this TG revision; an export
    # rendered by other TG code needs a reviewed lab update first.
    if any(
        not replay.TG_REVISION.startswith(
            getattr(loaded.cases[0].generator, role).input.prompt_version
        )
        or len(getattr(loaded.cases[0].generator, role).input.prompt_version) < 7
        for role in ("writer", "reviewer", "revision")
    ):
        raise ValueError("tg_revision_mismatch")
    runs = []
    for policy in policies:
        scope = approval_scope(
            paths["suite"], root / policy.model, policy, campaign=campaign
        )
        manifest = scope["scope"]["manifest"]
        if len(manifest["plan"]) != CALLS_PER_MODEL:
            raise ValueError("four_calls_required")
        inputs = manifest["inputs"][manifest["plan"][0]["case_id"]]
        native_request(
            policy,
            replay.replay_prompt(inputs["writer"]["messages"], "SeoContentSchema"),
        )
        runs.append(scope)
    return {
        "format": FORMAT,
        "paths": paths,
        "output": str(root),
        "code_identity": code_identity(),
        "dependencies": runtime_dependencies(),
        "config": config,
        "tariff": tariff,
        "tg_revision": replay.TG_REVISION,
        "schemas": replay.SCHEMAS,
        "review_item_ids": replay.review_item_ids(inputs["reviewer"]["messages"]),
        "target_words": replay.target_words(inputs["writer"]["messages"]),
        "file_digests": {
            k: hashlib.sha256(Path(v).read_bytes()).hexdigest()
            for k, v in paths.items()
        },
        "suite_custody": {
            "manifest": loaded.manifest.model_dump(mode="json"),
            "cases": [c.model_dump(mode="json") for c in loaded.cases],
        },
        "campaign": campaign.identity,
        "runs": runs,
        "max_calls": MAX_CALLS,
        "conservative_reservation_usd": str(_total(policies)),
        "semantic_quality": "unassessed",
    }


def prepare(*, suite, config, tariff, output, plan):
    """Create a private frozen review packet; never load credentials or SDKs."""
    from .annotation import _write_new

    paths = {
        k: str(Path(v).absolute())
        for k, v in dict(suite=suite, config=config, tariff=tariff).items()
    }
    # Validate inputs before creating authority. A failed creation is never reset.
    _policies(read_json(config), read_json(tariff))
    load_suite(suite)
    root = _directory(output, create=True)
    if Path(plan).absolute() != root / "plan.json":
        raise ValueError("plan_must_be_output_plan_json")
    with (
        _run_lock(root, create=True),
        CampaignBudget.create(root / "campaign.sqlite3") as campaign,
    ):
        scope = _scope(paths, root, campaign)
        packet = {"digest": digest(scope), "scope": scope}
        _write_new(root / "plan.json", canonical_bytes(packet))
        _fsync(root)
    return _summary(packet)


def _summary(packet):
    scope = packet["scope"]
    return {
        "valid": True,
        "approval_digest": packet["digest"],
        "max_calls": MAX_CALLS,
        "models": list(MODELS),
        "campaign_ceiling_usd": "50.00",
        "conservative_reservation_usd": scope["conservative_reservation_usd"],
        "tariff_reviewed": scope["tariff"]["reviewed"],
        "tg_revision": scope["tg_revision"],
        "target_words": scope["target_words"],
        "account_access_verified": False,
        "model_execution_performed": False,
        "semantic_quality": "unassessed",
    }


def _checked(plan):
    packet = read_json(plan)
    scope = packet["scope"]
    root = _directory(scope["output"])
    if Path(plan).absolute() != root / "plan.json" or scope["format"] != FORMAT:
        raise ValueError("invalid_plan_location")
    with CampaignBudget.open(root / "campaign.sqlite3", readonly=True) as campaign:
        current = _scope(scope["paths"], root, campaign)
    if packet["digest"] != digest(scope) or current != scope:
        raise ValueError("pilot_plan_changed")
    return packet, root


def preflight(plan):
    """Read-only, zero egress; no secret loading, account probe or approval."""
    packet, _ = _checked(plan)
    return _summary(packet)


def _validate_outputs(snapshot, scope):
    """Stop rules only. Length against the brief target is scored, never a stop."""
    from jsonschema import Draft202012Validator

    if any(
        row["state"] not in ("planned", "reserved", "completed")
        for row in snapshot["work"]
    ):
        return False
    for row in snapshot["work"]:
        if row["state"] != "completed":
            continue
        role = row["role"]
        try:
            data = strict_json_loads(row["result"]["output"])
        except (ValueError, TypeError):
            return False
        schema = scope["schemas"][replay.ROLE_SCHEMAS[role]]
        if not isinstance(data, dict) or not Draft202012Validator(schema).is_valid(
            data
        ):
            return False
        if role == "reviewer":
            ids = [item["item_id"] for item in data["results"]]
            if sorted(ids) != sorted(scope["review_item_ids"]):
                return False
        elif data["body"].lstrip().startswith("---"):
            return False
    return True


def _lengths(snapshot, target):
    return {
        row["role"]: replay.length_score(
            strict_json_loads(row["result"]["output"])["body"], target
        )
        for row in snapshot["work"]
        if row["state"] == "completed" and row["role"] in ("writer", "revision")
    }


def _replay_request(root, model, snapshot, stage):
    """Write the TG review replay input once; a changed rewrite is refused."""
    from .annotation import _write_new

    rows = snapshot["work"]
    draft, review = (rows[0], rows[1]) if stage == "draft" else (rows[2], rows[3])
    request = replay.replay_request(
        case_id=draft["case_id"],
        model=model,
        stage=stage,
        draft_output=draft["result"]["output"],
        review_output=review["result"]["output"],
    )
    directory = root / REPLAY_DIRECTORY
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _fsync(root)
    _directory(directory)
    path = directory / f"{model}.{stage}.request.json"
    data = canonical_bytes(request)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("review_replay_request_changed")
    else:
        _write_new(path, data)
    return path


def _revision_input(root, model, snapshot):
    """Return the validated TG replay response, or None while it is absent."""
    _replay_request(root, model, snapshot, "draft")
    path = root / REPLAY_DIRECTORY / f"{model}.response.json"
    if not (path.exists() or path.is_symlink()):
        return None
    response = read_json(path)
    rows = snapshot["work"]
    inputs = snapshot["manifest"]["inputs"][rows[0]["case_id"]]
    replay.revision_messages(
        response,
        case_id=rows[0]["case_id"],
        draft_output=rows[0]["result"]["output"],
        review_output=rows[1]["result"]["output"],
        system=inputs["revision"]["messages"][0]["content"],
    )
    return response


def run(plan, *, env_file, approve, resume=False):
    """Only explicit live entry point. Stop globally on uncertain/invalid output.

    Each invocation drives at most one role before saved-output schema checks.
    Resume retains the same authority and validates every completed output before
    spending anything else. It never regenerates malformed or uncertain work.
    Before each revision it waits for the TG review replay response file.
    """
    from .adapters.sdk_logging import refuse_sdk_debug_logging
    from .credentials import load_credentials
    from .provider_workflow import resume_provider, run_provider

    refuse_sdk_debug_logging()
    packet, root = _checked(plan)
    scope = packet["scope"]
    if approve != packet["digest"] or not scope["tariff"]["reviewed"]:
        raise ValueError("exact_reviewed_approval_required")
    credentials = load_credentials(env_file)
    if (
        credentials["DRAFTBENCH_OPENAI_ORGANIZATION"] != scope["config"]["organization"]
        or credentials["DRAFTBENCH_OPENAI_PROJECT"] != scope["config"]["project"]
    ):
        raise ValueError("credential_route_mismatch")
    with _run_lock(root), CampaignBudget.open(root / "campaign.sqlite3") as campaign:
        # Recheck under the authority lock. Every child is approved by equality,
        # never by an unconditional callback. No automatic new authority/run IDs.
        again, _ = _checked(plan)
        if again != packet:
            raise ValueError("pilot_plan_changed")
        if not resume and any((root / m).exists() for m in MODELS):
            raise ValueError("use_explicit_resume")
        completed, waiting, lengths = 0, [], {}
        for item in scope["runs"]:
            manifest = item["scope"]["manifest"]
            policy = parse_policy(manifest["policy"])
            run_dir = root / policy.model
            expected = item["binding"]

            def approved(binding):
                return approve == packet["digest"] and binding == expected

            key = credentials[
                "DRAFTBENCH_ANTHROPIC_API_KEY"
                if policy.model.startswith("claude-")
                else "DRAFTBENCH_OPENAI_API_KEY"
            ]
            if run_dir.exists():
                # Recover in-flight -> uncertain without dispatch, then stop.
                resume_provider(
                    run_dir,
                    approve=approved,
                    api_key=key,
                    campaign=campaign,
                    max_steps=0,
                )
            while True:
                if not run_dir.exists():
                    run_provider(
                        scope["paths"]["suite"],
                        run_dir,
                        policy,
                        approve=approved,
                        api_key=key,
                        campaign=campaign,
                        max_steps=1,
                    )
                    continue
                snapshot = snapshot_provider(run_dir)
                if not _validate_outputs(snapshot, scope):
                    return {
                        "complete": False,
                        "error": "pilot_stopped_output_or_state",
                        "semantic_quality": "unassessed",
                    }
                lengths[policy.model] = _lengths(snapshot, scope["target_words"])
                states = [row["state"] for row in snapshot["work"]]
                if all(state == "completed" for state in states):
                    _replay_request(root, policy.model, snapshot, "revision")
                    completed += 1
                    break
                revision_input = None
                if states[2] == "planned" and states[1] == "completed":
                    revision_input = _revision_input(root, policy.model, snapshot)
                    if revision_input is None:
                        waiting.append(policy.model)
                        break
                resume_provider(
                    run_dir,
                    approve=approved,
                    api_key=key,
                    campaign=campaign,
                    max_steps=1,
                    revision_input=revision_input,
                )
                after = snapshot_provider(run_dir)
                if states == [row["state"] for row in after["work"]]:
                    return {"complete": False, "error": "pilot_admission_stopped"}
        result = {
            "complete": completed == len(MODELS),
            "campaign": campaign.summary(),
            "length": lengths,
            "semantic_quality": "unassessed",
            "billing_reconciled": False,
        }
        if waiting:
            result["waiting"] = "tg_review_replay_required"
            result["review_replay_requests"] = [
                f"{REPLAY_DIRECTORY}/{model}.draft.request.json" for model in waiting
            ]
        return result
