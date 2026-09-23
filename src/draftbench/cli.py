"""Offline-default CLI, with separately approved explicit pilot dispatch."""

import argparse
import json
import os
import sys

from . import __version__
from .loader import SuiteError, load_suite
from .report import inventory


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message can echo arbitrary private input tokens.
        print(
            json.dumps({"valid": False, "error": "invalid_arguments"}), file=sys.stderr
        )
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="draftbench",
        description="Offline validation, scoring and fixtures; explicit approved pilot dispatch is separate.",
    )
    parser.add_argument(
        "--version", action="version", version=f"draftbench {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in [
        ("validate", "Validate schemas, identities, rights and local file hashes"),
        (
            "inventory",
            "Report aggregate availability and historical versus reconstructed inputs",
        ),
    ]:
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument(
            "manifest",
            help="Path to a local suite.json; references stay inside its directory",
        )
        sub.add_argument(
            "--output",
            help="Create a NEW report file (never overwrite); otherwise print JSON",
        )
    run = commands.add_parser(
        "run", help="Run the deterministic synthetic adapter only"
    )
    run.add_argument("manifest", help="Synthetic infrastructure suite manifest")
    run.add_argument("--adapter", required=True, choices=["fake", "inspect-fixture"])
    run.add_argument(
        "--output", required=True, help="NEW private run directory outside Git"
    )
    run.add_argument(
        "--max-steps", type=int, help="Stop after this many new synthetic invocations"
    )
    run.add_argument(
        "--inspect-policy",
        help="Local Inspect fixture admission policy JSON; frozen on creation",
    )
    resume = commands.add_parser(
        "resume", help="Resume a saved synthetic run without duplicate work"
    )
    resume.add_argument("run_dir", help="Existing private run directory")
    resume.add_argument(
        "--max-steps", type=int, help="Stop after this many new synthetic invocations"
    )
    score = commands.add_parser(
        "score", help="Score an artifact-bound reference bundle without model calls"
    )
    score.add_argument("bundle", help="Self-contained scoring bundle JSON")
    score.add_argument(
        "--output", help="Create a NEW aggregate report file, never overwrite"
    )
    review = commands.add_parser(
        "review", help="Private blinded annotation export/import"
    )
    review_commands = review.add_subparsers(dest="review_command", required=True)
    export = review_commands.add_parser(
        "export", help="Create a new private annotation session"
    )
    export.add_argument(
        "bundle", help="Source scoring bundle; its labels remain private"
    )
    export.add_argument(
        "--rubric", required=True, help="Neutral annotation rubric JSON"
    )
    export.add_argument("--raters", required=True, help="Private rater roster JSON")
    export.add_argument(
        "--output", required=True, help="NEW session directory outside Git"
    )
    for command in ("import", "adjudicate"):
        sub = review_commands.add_parser(
            command, help="Append bound responses without replacing originals"
        )
        sub.add_argument("session", help="Private annotation session directory")
        sub.add_argument("responses", help="Returned response JSON")
    status = review_commands.add_parser(
        "status", help="Show aggregate annotation coverage"
    )
    status.add_argument("session")
    records = review_commands.add_parser(
        "records", help="Export private custodian records, never blind packets"
    )
    records.add_argument("session")
    records.add_argument(
        "--output", required=True, help="NEW private record export file outside Git"
    )
    records.add_argument("--kind", choices=["original", "adjudication"])
    report = commands.add_parser(
        "report", help="Private static offline reports; never runs adapters"
    )
    reports = report.add_subparsers(dest="report_command", required=True)
    prepare = reports.add_parser(
        "prepare", help="Bind completed saved outputs to mechanical scoring"
    )
    prepare.add_argument("run_dir")
    prepare.add_argument("--output", required=True)
    render = reports.add_parser(
        "render", help="Render a verified run and/or scoring bundle"
    )
    render.add_argument("--run", dest="run_dir")
    render.add_argument(
        "--provider-run",
        dest="provider_run_dir",
        help="Saved provider run; requires --bundle and --bindings",
    )
    render.add_argument("--bundle", dest="bundle_path")
    render.add_argument("--bindings", dest="bindings_path")
    render.add_argument("--session")
    render.add_argument("--selection", dest="selection_path")
    render.add_argument("--output", required=True)
    replay = reports.add_parser(
        "replay", help="Verify and reproduce saved normalized report"
    )
    replay.add_argument("report_dir")
    replay.add_argument("--output", required=True)
    release = commands.add_parser(
        "release", help="Explicit allowlisted LOCAL export, no hosting"
    )
    releases = release.add_subparsers(dest="release_command", required=True)
    check = releases.add_parser(
        "check", help="Preview exact projection and approval digest"
    )
    check.add_argument("report_dir")
    check.add_argument("--selection", required=True)
    export_public = releases.add_parser(
        "export", help="Create NEW local folder after bound approval"
    )
    export_public.add_argument("report_dir")
    export_public.add_argument("--approval", required=True)
    export_public.add_argument("--output", required=True)
    for provider_name in ("openai", "anthropic"):
        provider = commands.add_parser(
            provider_name,
            help="Offline SDK wire fixtures and saved reports; no live CLI",
        )
        providers = provider.add_subparsers(dest="provider_command", required=True)
        fixture = providers.add_parser("fixture-run")
        fixture.add_argument("manifest")
        fixture.add_argument("--policy", required=True)
        fixture.add_argument("--output", required=True)
        fixture.add_argument("--max-steps", type=int)
        fixture.add_argument(
            "--campaign", help="Existing shared USD50 campaign database"
        )
        prepare_provider = providers.add_parser("prepare")
        prepare_provider.add_argument("run_dir")
        prepare_provider.add_argument("--rights", required=True)
        prepare_provider.add_argument("--output", required=True)
        for name in ("fixture-resume", "report"):
            sub = providers.add_parser(name)
            sub.add_argument("run_dir")
            if name == "fixture-resume":
                sub.add_argument("--campaign")
    pilot = commands.add_parser(
        "pilot", help="Explicit bounded pilot; preparation is offline"
    )
    pilots = pilot.add_subparsers(dest="pilot_command", required=True)
    prep = pilots.add_parser(
        "prepare", help="Freeze private plan; no credentials or network"
    )
    for name in (
        "suite",
        "config",
        "tariff",
        "writer-schema",
        "reviewer-schema",
        "output",
        "plan",
    ):
        prep.add_argument("--" + name, required=True)
    check = pilots.add_parser(
        "preflight", help="Revalidate exact plan offline; never approve"
    )
    check.add_argument("--plan", required=True)
    for command in ("run", "resume"):
        sub = pilots.add_parser(
            command,
            help="LIVE: requires exact operator approval and explicit credentials",
        )
        sub.add_argument("--plan", required=True)
        sub.add_argument("--env-file", required=True)
        sub.add_argument(
            "--approve",
            required=True,
            help="Exact reviewed preflight digest; not a credential",
        )
    return parser


def _write_report(output: str, text: str) -> None:
    try:
        # Exclusive creation rejects existing files, hard links and symlinks.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError as exc:
        raise SuiteError("output_exists") from exc
    except (OSError, ValueError) as exc:
        raise SuiteError("output_unwritable") from exc


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "pilot":
        from . import pilot

        try:
            if args.pilot_command == "prepare":
                result = pilot.prepare(
                    **{
                        key: getattr(args, key)
                        for key in (
                            "suite",
                            "config",
                            "tariff",
                            "writer_schema",
                            "reviewer_schema",
                            "output",
                            "plan",
                        )
                    }
                )
            elif args.pilot_command == "preflight":
                result = pilot.preflight(args.plan)
            else:
                result = pilot.run(
                    args.plan,
                    env_file=args.env_file,
                    approve=args.approve,
                    resume=args.pilot_command == "resume",
                )
            print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
            return 0 if result.get("complete", True) else 3
        except Exception:
            # Never echo parser, SDK, filesystem or credential exception text.
            print(
                json.dumps({"valid": False, "error": "pilot_refused"}), file=sys.stderr
            )
            return 2
    if args.command in ("openai", "anthropic"):
        return _provider(args)
    if args.command in ("report", "release"):
        return _report_release(args)
    if args.command in ("run", "resume"):
        return _execution(args)
    if args.command == "score":
        return _score(args)
    if args.command == "review":
        return _annotation(args)
    try:
        loaded = load_suite(args.manifest)
        report = (
            inventory(loaded)
            if args.command == "inventory"
            else {"schema_version": "1", "valid": True, "case_count": len(loaded.cases)}
        )
        text = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            _write_report(args.output, text)
        else:
            sys.stdout.write(text)
        return 0
    except SuiteError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}), file=sys.stderr)
        return 2


def _report_release(args) -> int:
    from .release import check_release, export_release
    from .reporting import build_report, prepare_report, read_report, write_report

    try:
        if args.command == "release":
            result = (
                check_release(args.report_dir, args.selection)
                if args.release_command == "check"
                else export_release(args.report_dir, args.approval, args.output)
            )
        elif args.report_command == "prepare":
            result = prepare_report(args.run_dir, args.output)
        elif args.report_command == "replay":
            result = write_report(read_report(args.report_dir), args.output)
        else:
            result = write_report(
                build_report(
                    run_dir=args.run_dir,
                    provider_run_dir=args.provider_run_dir,
                    bundle_path=args.bundle_path,
                    bindings_path=args.bindings_path,
                    session=args.session,
                    selection_path=args.selection_path,
                ),
                args.output,
            )
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        # Validation diagnostics may contain private labels; never echo them.
        print(
            json.dumps({"valid": False, "error": "report_or_release_invalid"}),
            file=sys.stderr,
        )
        return 2


def _provider(args):
    from .adapters.provider_contract import parse_policy, provider_name
    from .provider_reporting import prepare_provider, report_provider
    from .reporting import read_json

    campaign = None
    try:
        if getattr(args, "campaign", None):
            from .campaign import CampaignBudget

            campaign = CampaignBudget.open(args.campaign)
        if args.provider_command == "prepare":
            result = prepare_provider(args.run_dir, args.output, read_json(args.rights))
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0
        if args.provider_command in ("fixture-run", "fixture-resume"):
            if args.command == "anthropic":
                from .adapters.anthropic_fixture import fixture_transport
            else:
                from .adapters.openai_fixture import fixture_transport
            from .provider_workflow import resume_provider, run_provider

        if args.provider_command == "fixture-run":
            policy = parse_policy(read_json(args.policy))
            if provider_name(policy) != args.command:
                raise ValueError("provider_policy_mismatch")
            result = run_provider(
                args.manifest,
                args.output,
                policy,
                transport=fixture_transport(policy.model),
                max_steps=args.max_steps,
                campaign=campaign,
            )
        elif args.provider_command == "fixture-resume":
            from .ledger import Ledger
            from .store import ArtifactStore
            from .workflow import _directory, _run_lock

            root = _directory(args.run_dir)
            with _run_lock(root):
                with Ledger.open(root / "ledger.sqlite3", readonly=True) as ledger:
                    manifest = ArtifactStore(root / "objects").get_json(
                        ledger.manifest_digest
                    )
                    policy = parse_policy(manifest["policy"])
            if provider_name(policy) != args.command:
                raise ValueError("provider_policy_mismatch")
            result = resume_provider(
                args.run_dir,
                transport=fixture_transport(policy.model),
                campaign=campaign,
            )
        else:
            result = report_provider(args.run_dir)
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0 if result["complete"] or args.provider_command == "report" else 3
    except Exception:
        print(
            json.dumps({"valid": False, "error": "invalid_provider_run"}),
            file=sys.stderr,
        )
        return 2

    finally:
        if campaign is not None:
            campaign.close()


def _execution(args) -> int:
    from .ledger import LedgerError
    from .store import StoreError
    from .workflow import RunError, resume_synthetic, run_synthetic

    try:
        if args.command == "run":
            policy = None
            if args.inspect_policy is not None:
                from .reporting import read_json

                policy = read_json(args.inspect_policy)
            report = run_synthetic(
                args.manifest,
                args.output,
                max_steps=args.max_steps,
                adapter=args.adapter,
                inspect_policy=policy,
            )
        else:
            report = resume_synthetic(args.run_dir, max_steps=args.max_steps)
        print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
        return 0 if report["complete"] else 3
    except (RunError, LedgerError, StoreError, SuiteError) as exc:
        code = str(exc)
    except (OSError, ValueError, TypeError, RecursionError):
        code = "invalid_run_state"
    print(json.dumps({"valid": False, "error": code}), file=sys.stderr)
    return 2


def _score(args) -> int:
    from .scoring.reporting import ScoringError, load_scoring_bundle, score_bundle

    try:
        report = score_bundle(load_scoring_bundle(args.bundle))
        text = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            _write_report(args.output, text)
        else:
            sys.stdout.write(text)
        return 0
    except (ScoringError, SuiteError) as exc:
        code = str(exc)
    except Exception:
        code = "scoring_failed"
    print(json.dumps({"valid": False, "error": code}), file=sys.stderr)
    return 2


def _annotation(args) -> int:
    from .annotation import (
        annotation_status,
        create_annotation_session,
        export_annotation_records,
        import_annotation_responses,
    )
    from .annotation_models import AnnotationError
    from .annotation_store import AnnotationStoreError
    from .scoring.reporting import ScoringError
    from .store import StoreError
    from .workflow import RunError

    try:
        if args.review_command == "export":
            result = create_annotation_session(
                args.bundle, args.rubric, args.raters, args.output
            )
        elif args.review_command in ("import", "adjudicate"):
            result = import_annotation_responses(
                args.session,
                args.responses,
                adjudication=args.review_command == "adjudicate",
            )
        elif args.review_command == "records":
            result = export_annotation_records(
                args.session, args.output, kind=args.kind
            )
        else:
            result = annotation_status(args.session)
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (
        AnnotationError,
        AnnotationStoreError,
        ScoringError,
        StoreError,
        RunError,
        SuiteError,
    ) as exc:
        code = str(exc)
    except FileExistsError:
        code = "annotation_exists"
    except Exception:
        code = "annotation_invalid"
    print(json.dumps({"valid": False, "error": code}), file=sys.stderr)
    return 2
