"""Identifiers: the Part 2 path encoding and the SEM-1 §1 identifier scheme (normative, permanent).

Part 2 (IDTA-01002, http-rest-api): identifiers in paths and query parameters are UTF-8 bytes,
base64url-encoded WITHOUT padding. Decoding here tolerates missing or present padding but rejects
anything that does not round-trip to valid UTF-8.

SEM-1 §1 (owner ruling 2026-10-02): permanent identifiers are minted under https://id.madfam.io/.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

ID_BASE = "https://id.madfam.io/"

SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
HEX16 = r"[0-9a-f]{16}"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# AAS v3.1.2 idShort pattern (aas.json): letter first, then letters/digits/_/-, not ending in '-'.
ID_SHORT = r"[a-zA-Z][a-zA-Z0-9_-]*[a-zA-Z0-9_]+"
CONCEPT_TERM = r"[a-z0-9][a-z0-9_.-]*"

DESIGN_KINDS = ("solid", "soft")
#: Type-level assemblies (ASM-1 §5): authored in the solid commons, named by their assembly digest.
ASSEMBLY_KIND = "assembly"
TYPE_KINDS = ("solid", "soft", "material", ASSEMBLY_KIND)
_KINDS = "|".join(TYPE_KINDS)

_RE_TYPE_SHELL = re.compile(rf"^{re.escape(ID_BASE)}aas/({_KINDS})/({SLUG})/({HEX16})$")
_RE_TYPE_ASSET = re.compile(rf"^{re.escape(ID_BASE)}asset/({_KINDS})/({SLUG})$")
_RE_TYPE_SUBMODEL = re.compile(rf"^{re.escape(ID_BASE)}sm/({_KINDS})/({SLUG})/({HEX16})/({ID_SHORT})$")
_RE_INSTANCE_SHELL = re.compile(rf"^{re.escape(ID_BASE)}aas/instance/({UUID})$")
_RE_INSTANCE_ASSET = re.compile(rf"^{re.escape(ID_BASE)}asset/instance/({UUID})$")
_RE_INSTANCE_SUBMODEL = re.compile(rf"^{re.escape(ID_BASE)}sm/instance/({UUID})/({ID_SHORT})$")
_RE_CONCEPT = re.compile(rf"^{re.escape(ID_BASE)}concept/({CONCEPT_TERM})$")
_RE_TEMPLATE = re.compile(rf"^{re.escape(ID_BASE)}smt/({SLUG})/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)$")
_RE_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RE_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# Which kinds of type shell each commons may publish. Material cards travel with the commons
# whose platform owns them (yantra4d → solid, fashion-cabinet → soft); assemblies live in the solid
# commons (`assemblies/{slug}/assembly.json`, ASM-1 §7).
COMMONS_KINDS: dict[str, frozenset[str]] = {
    "solid-hyperobjects": frozenset({"solid", "material", ASSEMBLY_KIND}),
    "soft-hyperobjects": frozenset({"soft", "material"}),
}


class InvalidEncodedId(ValueError):
    """A path or query identifier is not valid base64url-encoded UTF-8."""


def b64url_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64url_decode(encoded: str) -> str:
    if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
        raise InvalidEncodedId("not a base64url value")
    padded = encoded.rstrip("=") + "=" * (-len(encoded.rstrip("=")) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
        return raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise InvalidEncodedId("not base64url-encoded UTF-8") from None


@dataclass(frozen=True)
class TypeShellId:
    kind: str
    slug: str
    digest16: str

    @property
    def asset_id(self) -> str:
        return f"{ID_BASE}asset/{self.kind}/{self.slug}"

    @property
    def submodel_prefix(self) -> str:
        return f"{ID_BASE}sm/{self.kind}/{self.slug}/{self.digest16}/"


@dataclass(frozen=True)
class InstanceShellId:
    uuid: str

    @property
    def asset_id(self) -> str:
        return f"{ID_BASE}asset/instance/{self.uuid}"

    @property
    def submodel_prefix(self) -> str:
        return f"{ID_BASE}sm/instance/{self.uuid}/"


def parse_type_shell_id(value: str) -> TypeShellId | None:
    m = _RE_TYPE_SHELL.match(value)
    return TypeShellId(m.group(1), m.group(2), m.group(3)) if m else None


def parse_instance_shell_id(value: str) -> InstanceShellId | None:
    m = _RE_INSTANCE_SHELL.match(value)
    return InstanceShellId(m.group(1)) if m else None


def type_submodel_parts(value: str) -> tuple[str, str, str, str] | None:
    """(kind, slug, digest16, idShort) of a type submodel id, or None."""
    m = _RE_TYPE_SUBMODEL.match(value)
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def instance_submodel_parts(value: str) -> tuple[str, str] | None:
    """(uuid, idShort) of an instance submodel id, or None."""
    m = _RE_INSTANCE_SUBMODEL.match(value)
    return (m.group(1), m.group(2)) if m else None


def is_type_asset_id(value: str) -> bool:
    return bool(_RE_TYPE_ASSET.match(value))


def is_instance_asset_id(value: str) -> bool:
    return bool(_RE_INSTANCE_ASSET.match(value))


def is_concept_id(value: str) -> bool:
    return bool(_RE_CONCEPT.match(value))


def is_template_id(value: str) -> bool:
    """A MADFAM submodel template id: https://id.madfam.io/smt/{template-name}/{major}/{minor}."""
    return bool(_RE_TEMPLATE.match(value))


def looks_like_instance_id(value: str) -> bool:
    """True for any identifier in the instance namespace (shell, asset or submodel)."""
    return value.startswith((f"{ID_BASE}aas/instance/", f"{ID_BASE}sm/instance/", f"{ID_BASE}asset/instance/"))


def is_commit_sha(value: str) -> bool:
    return bool(_RE_SHA40.match(value))


def is_sha256(value: str) -> bool:
    return bool(_RE_SHA256.match(value))


def is_valid_tenant(value: str) -> bool:
    return bool(_RE_TENANT.match(value))
