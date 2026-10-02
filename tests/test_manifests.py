"""The production manifests satisfy the Kyverno-derived rules (scripts/check_manifests.py), and the
rules themselves catch violations. CI also runs the script on a real `kustomize build`."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "infra" / "k8s" / "production"

spec = importlib.util.spec_from_file_location("check_manifests", ROOT / "scripts" / "check_manifests.py")
check_manifests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_manifests)


def render() -> list[dict]:
    """A minimal stand-in for `kustomize build`: the resources plus the images: digest pins."""
    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    pins = {i["name"]: f"{i['newName']}@{i['digest']}" for i in kustomization["images"]}
    docs: list[dict] = []
    for resource in kustomization["resources"]:
        docs += [d for d in yaml.safe_load_all((OVERLAY / resource).read_text()) if d]
    for doc in docs:
        if doc.get("kind") == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            for c in pod.get("containers", []) + pod.get("initContainers", []):
                c["image"] = pins[c["image"]]
    return docs


def test_overlay_passes_the_rules():
    assert check_manifests.check(render()) == []


def test_enclii_manifest_names_the_ruled_domains():
    docs = list(yaml.safe_load_all((ROOT / "enclii.yaml").read_text()))
    domains = {d["name"] for doc in docs for d in doc.get("spec", {}).get("domains", [])}
    assert domains == {"asset-shells.madfam.io", "asset-shells-api.madfam.io"}


def _mutated(path: list, value) -> list[dict]:
    docs = copy.deepcopy(render())
    node = next(d for d in docs if d.get("kind") == "Deployment")
    for key in path[:-1]:
        node = node[key]
    if value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    return docs


CONTAINER = ["spec", "template", "spec", "containers", 0]


@pytest.mark.parametrize(
    ("path", "value", "fragment"),
    [
        (["spec", "template", "spec", "securityContext", "runAsNonRoot"], False, "runAsNonRoot"),
        ([*CONTAINER, "image"], "docker.io/library/python:latest", "pinned by digest"),
        ([*CONTAINER, "securityContext", "readOnlyRootFilesystem"], False, "readOnlyRootFilesystem"),
        ([*CONTAINER, "securityContext", "allowPrivilegeEscalation"], True, "allowPrivilegeEscalation"),
        ([*CONTAINER, "securityContext", "capabilities"], {"drop": []}, "drop ALL"),
        ([*CONTAINER, "resources"], {}, "requests"),
        ([*CONTAINER, "readinessProbe"], None, "probes"),
        ([*CONTAINER, "ports"], [{"containerPort": 8000, "hostPort": 8000}], "host ports"),
    ],
)
def test_rules_catch_violations(path, value, fragment):
    errors = check_manifests.check(_mutated(path, value))
    assert any(fragment in e for e in errors), errors


def test_rules_catch_forbidden_objects_and_tunnel_reach():
    docs = render() + [{"kind": "Secret", "metadata": {"name": "x"}}]
    policy = next(d for d in docs if d.get("kind") == "NetworkPolicy" and "cloudflare" in str(d["spec"]))
    policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] = "something-else"
    errors = check_manifests.check(docs)
    assert any("Secret" in e for e in errors) and any("tunnel" in e for e in errors)
    assert check_manifests.check(render()[:1])  # missing deployments


def test_script_entrypoint(tmp_path, capsys):
    path = tmp_path / "render.yaml"
    path.write_text(yaml.safe_dump_all(render()))
    assert check_manifests.main(str(path)) == 0
    assert "manifest rules ok" in capsys.readouterr().out
