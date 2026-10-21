"""Structure checks for the CI release pipeline and the bau-installed workflow."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
MAIN_ONLY = "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def is_publish_step(step: dict) -> bool:
    run = step.get("run", "")
    uses = step.get("uses", "")
    return (
        "--push" in run
        or "gh release create" in run
        or "docker login" in run
        or "record-deployment" in uses
        or "certs.d" in run
    )


def test_release_publishes_only_from_main_and_in_order():
    jobs = load("ci.yml")["jobs"]
    assert set(jobs["images"]["needs"]) == {"version"}
    assert {"version", "images", "web"} <= set(jobs["release"]["needs"])
    for job in ("images", "release"):
        # The gate is a job-level PUBLISH flag that only a push to main turns on,
        # and every publishing step tests that flag.
        assert MAIN_ONLY in str(jobs[job]["env"]["PUBLISH"])
        publish_steps = [s for s in jobs[job]["steps"] if is_publish_step(s)]
        assert publish_steps, job
        for step in publish_steps:
            assert "env.PUBLISH == 'true'" in str(step.get("if", "")), (job, step)


def test_images_are_arm64_only_and_never_latest():
    images = load("ci.yml")["jobs"]["images"]
    assert images["runs-on"] == "ubuntu-24.04-arm"
    text = yaml.safe_dump(images)
    assert "linux/arm64" in text and "linux/amd64" not in text and ":latest" not in text
    assert "--provenance=false" in text and "--sbom=false" in text
    names = {m["name"] for m in images["strategy"]["matrix"]["include"]}
    assert names == {"bau-agent", "bau-egress"}


def test_bundle_smoke_runs_before_publishing():
    steps = load("ci.yml")["jobs"]["release"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert names.index("bundle-smoke") < next(
        i for i, s in enumerate(steps) if "gh release create" in s.get("run", "")
    )


def test_secrets_only_in_the_release_environment():
    jobs = load("ci.yml")["jobs"]
    assert any("secrets.NEXUS" in yaml.safe_dump(j) for j in jobs.values())
    for name, job in jobs.items():
        if "secrets.NEXUS" in yaml.safe_dump(job):
            assert "release" in str(job.get("environment", "")), name
            assert MAIN_ONLY in str(job["environment"]), name


def test_web_uploads_dist_after_building_it():
    steps = load("ci.yml")["jobs"]["web"]["steps"]
    build = next(i for i, s in enumerate(steps) if s.get("run") == "pnpm build")
    upload = next(i for i, s in enumerate(steps) if "upload-artifact" in s.get("uses", ""))
    assert build < upload
    assert steps[upload]["with"]["name"] == "web-dist"


def test_ci_status_waits_for_the_release_jobs():
    assert {"version", "images", "release"} <= set(load("ci.yml")["jobs"]["ci-status"]["needs"])


def test_bau_installed_records_prod_for_a_real_release():
    wf = load("bau-installed.yml")
    text = yaml.safe_dump(wf)
    assert set(wf[True]["workflow_dispatch"]["inputs"]) == {"version", "host"}
    assert "bau-v" in text and "environment: prod" in text and "project: bau" in text
    assert "AWS_SANDBOX_ROLE_ARN" in text
