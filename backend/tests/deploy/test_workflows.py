"""Static safety contract for the deployment workflows (DEPLOY-1).

These assert the production-critical invariants of the GitHub Actions workflows
without a live runner: no automatic production deploy, manual + protected
promotion, single-flight concurrency, least privilege, no IPC enablement, and no
secret echoing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (_WORKFLOWS / name).read_text(encoding="utf-8")


def _on(doc: dict) -> dict:
    # PyYAML parses the bare key ``on`` as the boolean True.
    spec = doc[True] if True in doc else doc["on"]
    return spec if isinstance(spec, dict) else {key: None for key in spec}


def test_deploy_is_manual_only_no_automatic_trigger() -> None:
    on = _on(_load("deploy-production.yml"))
    assert set(on) == {"workflow_dispatch"}
    assert "push" not in on and "pull_request" not in on


def test_deploy_uses_protected_production_environment() -> None:
    doc = _load("deploy-production.yml")
    promote = doc["jobs"]["promote"]
    assert promote["environment"] == "production"


def test_deploy_single_flight_concurrency() -> None:
    doc = _load("deploy-production.yml")
    assert doc["concurrency"]["group"] == "apexscan-production"
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_deploy_least_privilege_permissions() -> None:
    doc = _load("deploy-production.yml")
    assert doc["permissions"] == {"contents": "read", "packages": "read"}


def test_deploy_requires_sha_and_dhan_confirmation() -> None:
    on = _on(_load("deploy-production.yml"))
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["target_sha"]["required"] is True
    assert inputs["dhan_restart_safety_confirmed"]["type"] == "boolean"
    text = _text("deploy-production.yml")
    assert "inputs.dhan_restart_safety_confirmed != true" in text
    assert "deploy.eligibility" in text  # SHA eligibility gate is invoked


def test_deploy_fails_closed_without_transport() -> None:
    assert "PRODUCTION_TRANSPORT_NOT_CONFIGURED" in _text("deploy-production.yml")


def test_deploy_never_builds_from_source() -> None:
    # Allow --no-build; forbid any real --build directive.
    assert "--build" not in _text("deploy-production.yml").replace("--no-build", "")


def test_build_publishes_sha_pinned_image_and_does_not_deploy() -> None:
    on = _on(_load("build-image.yml"))
    assert "workflow_dispatch" in on
    assert on["push"]["branches"] == ["main"]
    doc = _load("build-image.yml")
    assert doc["permissions"]["packages"] == "write"
    text = _text("build-image.yml")
    assert "BUILD_SHA=${{ github.sha }}" in text
    assert "${{ github.sha }}" in text  # immutable SHA tag
    # Stage A must not reach production.
    assert "PRODUCTION_SSH" not in text and "remote_update.sh" not in text


def test_stage_a_builds_sha_bound_bundle_without_deploying() -> None:
    text = _text("build-image.yml")
    assert "deploy.bundle create" in text  # bundle built in Stage A
    assert "deployment-bundle-${{ github.sha }}" in text  # artifact bound to exact SHA
    # source SHA passed via env, not interpolated into the run shell.
    assert "SOURCE_SHA: ${{ github.sha }}" in text
    assert "remote_update.sh" not in text and "PRODUCTION_SSH" not in text  # never deploys


def test_no_workflow_enables_ipc() -> None:
    forbidden = ("publisher_enabled", "consumer_enabled", "dual_path", "cutover", "market_stream")
    for name in ("build-image.yml", "deploy-production.yml", "ci.yml"):
        lowered = _text(name).lower()
        for token in forbidden:
            assert token not in lowered, f"{name} references {token}"


def test_no_workflow_echoes_secrets() -> None:
    # Flag only writes to the log (stdout); writing a secret to a controlled file
    # (`printf ... > ~/.ssh/key`) is how SSH material is provisioned, not a leak.
    for name in ("build-image.yml", "deploy-production.yml", "ci.yml"):
        for line in _text(name).splitlines():
            stripped = line.strip()
            if stripped.startswith(("echo", "printf", "print")) and ">" not in stripped:
                assert "secrets." not in stripped
                assert "SSH_KEY" not in stripped


def test_ci_typechecks_deploy_tooling() -> None:
    assert "mypy app deploy" in _text("ci.yml")


def test_no_dispatch_input_interpolated_into_run() -> None:
    # ${{ inputs.* }} / ${{ github.event.* }} must reach run: via env vars, never
    # be interpolated into the shell (script-injection guard).
    for name in ("build-image.yml", "deploy-production.yml"):
        for job in _load(name)["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run")
                if run:
                    assert "${{ inputs." not in run, f"{name}:{step.get('name')} interpolates input"
                    assert "${{ github.event" not in run


def test_promote_uses_ssh_transport_with_pinned_host_and_cleanup() -> None:
    text = _text("deploy-production.yml")
    assert "python -m deploy.transport" in text  # real transport, not a placeholder
    assert "prod_known_hosts" in text  # host-key pinning material is written
    assert "Remove SSH material" in text and "rm -f ~/.ssh/prod_key" in text  # cleanup
    assert "StrictHostKeyChecking=no" not in text  # host authenticity never disabled


def test_env_secrets_are_gitignored() -> None:
    gitignore = (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ignored = {line.strip() for line in gitignore}
    assert ".env" in ignored, "the real .env must never be committed"
