# Versioning, enforced.
#
# The repo claims three orthogonal version axes:
#
#   image      = git SHA, immutable
#   model      = LLM deployment name + version, embedding version, prompt
#                bundle SHA, plus registered sklearn model versions
#   deployment = the commit that changed the values file
#
# Every one of those was a claim in a README with nothing checking it. These
# tests make them properties. The interesting thing about version discipline
# is that it fails silently by construction - an unpinned model rolls forward
# and nothing errors, it just starts producing different output - so a test is
# the only place the failure can be made loud.

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHARTS = ROOT / "deploy" / "charts"

LIB_CHART = CHARTS / "medw-lib"
# Charts that consume the library chart.
CONSUMERS = sorted(
    d for d in CHARTS.iterdir()
    if d.is_dir() and d.name != "medw-lib" and (d / "Chart.yaml").exists()
    and "dependencies" in (d / "Chart.yaml").read_text()
)
ALL_CHARTS = sorted(d for d in CHARTS.iterdir() if d.is_dir() and (d / "Chart.yaml").exists())


def chart_yaml(d: pathlib.Path) -> dict:
    return yaml.safe_load((d / "Chart.yaml").read_text())


def values(d: pathlib.Path, name: str = "values.yaml") -> dict:
    path = d / name
    return yaml.safe_load(path.read_text()) if path.exists() else {}


# --- axis 1: the image is a git SHA --------------------------------------


@pytest.mark.parametrize("chart", ALL_CHARTS, ids=lambda d: d.name)
def test_no_chart_uses_a_floating_tag(chart):
    """`latest` makes "what is running" unanswerable and rollback impossible.

    Qdrant is exempt from the SHA rule (it is an upstream release, pinned to
    v1.12.1) but not from this one - a self-upgrading vector store changes
    recall with no commit anywhere.
    """
    tag = values(chart).get("image", {}).get("tag")
    if tag is None:
        pytest.skip(f"{chart.name} declares no image tag")
    assert tag not in ("latest", "main", "master", ""), f"{chart.name} uses a floating tag: {tag}"


@pytest.mark.parametrize("chart", ALL_CHARTS, ids=lambda d: d.name)
def test_environment_overlays_never_pin_their_own_tag(chart):
    """Promotion is a PR moving the SAME sha up an environment.

    If values-prod.yaml could set its own image tag, the artifact promoted
    would not be the artifact tested, and "we shipped exactly what passed
    staging" would stop being true. The overlays may override the *repository*
    (prod uses a different ACR) but never the tag.
    """
    for overlay in chart.glob("values-*.yaml"):
        # values-local.yaml is NOT in the promotion chain. It is a manual
        # `helm install -f` for a laptop, using images side-loaded with
        # `minikube image load` rather than pulled from a registry - so it must
        # pin `tag: dev` and set pullPolicy: Never. Flux never reads it.
        #
        # Excluded by name rather than by relaxing the rule: dev, staging and
        # prod are still forbidden from pinning their own tag, because that is
        # what makes "promotion ships exactly what was tested" true.
        if overlay.name == "values-local.yaml":
            continue
        image = values(chart, overlay.name).get("image", {})
        assert "tag" not in image, (
            f"{overlay.relative_to(ROOT)} pins its own image tag; promotion must carry "
            "the tag up from the environment below"
        )


# --- axis 2: the model pins ----------------------------------------------

# A deployment name must carry its version. Name it `gpt-4o` and Azure rolls
# the underlying version forward and your outputs change with no commit
# anywhere in the repo - the exact silent-drift failure the axes exist to stop.
PINNED_DEPLOYMENT = re.compile(r"^[a-z0-9.\-]+-\d{4}-\d{2}-\d{2}$")


def charts_with_chat_deployment():
    return [d for d in ALL_CHARTS if "chat_deployment" in values(d).get("config", {})]


@pytest.mark.parametrize("chart", charts_with_chat_deployment(), ids=lambda d: d.name)
def test_chat_deployment_carries_a_version_suffix(chart):
    name = values(chart)["config"]["chat_deployment"]
    assert PINNED_DEPLOYMENT.match(name), (
        f"{chart.name}: chat_deployment {name!r} has no version suffix. Azure will roll "
        "the model forward under you and the outputs change with no commit."
    )


@pytest.mark.parametrize("chart", ALL_CHARTS, ids=lambda d: d.name)
def test_embedding_version_is_set_wherever_embeddings_are_used(chart):
    """embed_version is baked into the Qdrant collection name, so it is what
    stops query vectors being compared against index vectors from a different
    model. Absent, the two spaces silently mix and return plausible garbage."""
    config = values(chart).get("config", {})
    if "embed_deployment" not in config:
        pytest.skip(f"{chart.name} does not embed")
    assert config.get("embed_version"), f"{chart.name} sets embed_deployment but no embed_version"


def test_indexer_and_retriever_agree_on_the_embedding_version():
    """The one invariant that cannot be expressed in a single chart.

    Ingestion writes vectors and retrieval queries them. If their
    embed_version values differ, the collection name differs, and retrieval
    searches a collection that ingestion never wrote to - returning nothing,
    or worse, stale results from a previous version.
    """
    versions = {
        d.name: values(d)["config"]["embed_version"]
        for d in ALL_CHARTS
        if "embed_version" in values(d).get("config", {})
    }
    assert len(set(versions.values())) == 1, f"embed_version disagrees across charts: {versions}"


# --- chart versioning ----------------------------------------------------


@pytest.mark.parametrize("chart", CONSUMERS, ids=lambda d: d.name)
def test_library_chart_pin_matches_the_library_version(chart):
    """Bumping medw-lib without re-pinning a consumer leaves that service
    rendering from a stale vendored tarball.

    That happened once already: medw-lib gained the autoscaling, ingress and
    PDB templates, and every consumer kept rendering the old 0.3.0 copy until
    the pins were bumped. `helm dependency update` catches it - but only if
    someone runs it, and only after the mistake is committed.
    """
    lib_version = chart_yaml(LIB_CHART)["version"]
    deps = chart_yaml(chart).get("dependencies", [])
    pins = [d["version"] for d in deps if d["name"] == "medw-lib"]
    assert pins == [lib_version], (
        f"{chart.name} pins medw-lib {pins} but the library is at {lib_version}"
    )


@pytest.mark.parametrize("chart", CONSUMERS, ids=lambda d: d.name)
def test_chart_lock_agrees_with_chart_yaml(chart):
    """Chart.lock is committed (the .tgz is not). A lock that disagrees with
    the declared dependency means the last `helm dependency update` predates
    the current pin."""
    lock = chart / "Chart.lock"
    if not lock.exists():
        pytest.skip(f"{chart.name} has no Chart.lock yet")
    locked = {d["name"]: d["version"] for d in yaml.safe_load(lock.read_text())["dependencies"]}
    declared = {d["name"]: d["version"] for d in chart_yaml(chart).get("dependencies", [])}
    assert locked == declared, f"{chart.name}: Chart.lock {locked} != Chart.yaml {declared}"


@pytest.mark.parametrize("chart", ALL_CHARTS, ids=lambda d: d.name)
def test_every_chart_declares_a_version(chart):
    meta = chart_yaml(chart)
    assert meta.get("version"), f"{chart.name} has no chart version"


# --- the bump script -----------------------------------------------------


def test_bump_rejects_latest():
    """The release tool must refuse the one tag that breaks rollback."""
    from scripts.bump_image_tag import BumpError, validate_tag

    with pytest.raises(BumpError, match="latest"):
        validate_tag("latest")


@pytest.mark.parametrize("bad", ["v1.2.3", "release", "", "not-a-sha", "main"])
def test_bump_rejects_non_sha_tags(bad):
    from scripts.bump_image_tag import BumpError, validate_tag

    with pytest.raises(BumpError):
        validate_tag(bad)


@pytest.mark.parametrize("good", ["4f2a91c", "3646734344a96d01288078ccb72a2c04721113e6"])
def test_bump_accepts_git_shas(good):
    from scripts.bump_image_tag import validate_tag

    validate_tag(good)   # must not raise


def test_bump_is_a_no_op_in_dry_run(tmp_path):
    """`--dry-run` has to be trustworthy or nobody uses it, and a release tool
    nobody trusts gets bypassed."""
    from scripts.bump_image_tag import bump

    target = CHARTS / "retrieval" / "values.yaml"
    before = target.read_text()
    old, new = bump("retrieval", "abc1234", dry_run=True)
    assert target.read_text() == before
    assert new == "abc1234" and old != ""


def test_bump_preserves_comments_and_changes_one_line():
    """The values files carry the reasoning for every setting in comments. A
    release step that strips them is worse than editing by hand."""
    from scripts.bump_image_tag import bump

    target = CHARTS / "retrieval" / "values.yaml"
    original = target.read_text()
    try:
        bump("retrieval", "abc1234")
        after = target.read_text()
        assert "the pipeline commits a real git SHA here" in after, "trailing comment lost"
        assert "the MODEL version axis lives here" in after, "block comments lost"
        differing = [
            (a, b) for a, b in zip(original.splitlines(), after.splitlines(), strict=True) if a != b
        ]
        assert len(differing) == 1, f"expected exactly one changed line, got {differing}"
        assert "abc1234" in differing[0][1]
    finally:
        target.write_text(original)


def test_bump_covers_every_service_we_build():
    """A service added without a values.yaml would be silently skipped at
    release time - built, pushed, and never deployed."""
    from scripts.bump_image_tag import service_charts

    found = set(service_charts())
    expected = {"gateway", "retrieval", "generation", "reranker", "ingestion-worker"}
    assert found == expected, f"bump script would miss: {expected - found}"
