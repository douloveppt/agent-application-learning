#!/usr/bin/env python3
"""In-memory governance control-plane lesson. Python 3.11+, synthetic data only."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from types import MappingProxyType


class Rejected(ValueError):
    """The supplied request cannot cross a teaching gate."""


PURPOSES = frozenset({"service", "evaluation", "training"})
KINDS = frozenset({"source", "memory", "index", "cache", "eval", "train_manifest", "model"})
SourceKey = tuple[str, str]  # tenant ID, source locator; never a bare content hash


def nonempty(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class Asset:
    asset_id: str
    tenant: str
    kind: str
    purpose: str
    payload: str | None
    parents: tuple[str, ...] = ()
    source_locator: str | None = None
    state: str = "active"


@dataclass(frozen=True)
class Feedback:
    feedback_id: str
    tenant: str
    source_asset: str
    input_text: str
    expected_text: str
    evidence_ref: str
    self_reported_reviewed: bool = False

    @property
    def content_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class Review:
    content_digest: str
    decision: str
    method: str
    reviewer: str
    evidence_ref: str


class TrustedRegistry:
    """An already authenticated policy snapshot, NOT user/model supplied claims.

    Construction and release_hold() stand for a trusted administrative channel.
    This demo does not implement authentication, signed policies or storage ACLs.
    """

    def __init__(self, grants: dict[SourceKey, frozenset[str]],
                 reviews: dict[tuple[str, str], Review] | None = None,
                 legal_holds: frozenset[str] = frozenset()):
        if any(not isinstance(key, tuple) or len(key) != 2
               or not all(nonempty(part) for part in key)
               or not isinstance(purposes, frozenset) or not purposes <= PURPOSES
               for key, purposes in grants.items()):
            raise Rejected("invalid trusted grant registry")
        self.grants = MappingProxyType(dict(grants))
        self.reviews = MappingProxyType(dict(reviews or {}))
        self._holds = set(legal_holds)

    def permits(self, key: SourceKey, purpose: str) -> bool:
        return purpose in self.grants.get(key, frozenset())

    def held(self, asset_id: str) -> bool:
        return asset_id in self._holds

    def release_hold(self, asset_id: str) -> None:
        """Trusted operator action; callers cannot release holds through feedback."""
        self._holds.discard(asset_id)

    def install_hold(self, asset_id: str) -> None:
        """Trusted operator action, represented separately from data payloads."""
        if not nonempty(asset_id):
            raise Rejected("hold asset ID is required")
        self._holds.add(asset_id)


class Catalog:
    def __init__(self, registry: TrustedRegistry):
        self.registry = registry
        self._assets: dict[str, Asset] = {}
        self._tombstones: set[SourceKey] = set()
        self._requests: dict[str, SourceKey] = {}

    def asset(self, asset_id: str) -> Asset:
        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise Rejected("unknown asset") from exc

    def _roots(self, asset_id: str, path: frozenset[str] = frozenset()) -> frozenset[SourceKey]:
        if asset_id in path:
            raise Rejected("lineage cycle")
        asset = self.asset(asset_id)
        if asset.kind == "source":
            return frozenset({(asset.tenant, str(asset.source_locator))})
        roots: set[SourceKey] = set()
        for parent in asset.parents:
            roots.update(self._roots(parent, path | {asset_id}))
        if not roots:
            raise Rejected("missing source lineage")
        return frozenset(roots)

    def _assert_usable(self, asset_id: str, purpose: str,
                       path: frozenset[str] = frozenset()) -> None:
        if asset_id in path:
            raise Rejected("lineage cycle")
        asset = self.asset(asset_id)
        if asset.state != "active" or self.registry.held(asset_id):
            raise Rejected("asset is not available for ordinary use")
        # A source is reusable only under its current trusted grants. Every
        # derived version is purpose-bound; a permissive root cannot authorize
        # promoting a service cache, training manifest or eval into a new use.
        if asset.kind != "source" and asset.purpose != purpose:
            raise Rejected("derived asset purpose does not match requested purpose")
        for key in self._roots(asset_id):
            if key in self._tombstones or not self.registry.permits(key, purpose):
                raise Rejected("source withdrawn or purpose not granted")
        for parent in asset.parents:
            self._assert_usable(parent, purpose, path | {asset_id})

    def admit(self, asset: Asset) -> None:
        if isinstance(asset, Asset) and asset.kind == "eval":
            raise Rejected("new eval assets require publish_eval and a trusted review")
        self._admit(asset)

    def _admit(self, asset: Asset) -> None:
        """Validate the complete new record before changing the catalog.

        Parents must already exist. Append-only identities and parent-first
        creation prevent a cycle in this API; traversal also detects corruption.
        """
        if (not isinstance(asset, Asset)
                or not all(nonempty(value) for value in (asset.asset_id, asset.tenant))
                or asset.kind not in KINDS or asset.purpose not in PURPOSES
                or asset.state != "active" or not nonempty(asset.payload)
                or not isinstance(asset.parents, tuple)
                or any(not nonempty(parent) for parent in asset.parents)
                or len(set(asset.parents)) != len(asset.parents)):
            raise Rejected("invalid asset contract")
        if asset.asset_id in self._assets or asset.asset_id in asset.parents:
            raise Rejected("asset identity reuse or self cycle")
        if asset.kind == "source":
            if asset.parents or not nonempty(asset.source_locator):
                raise Rejected("source requires locator and no parents")
            key = (asset.tenant, str(asset.source_locator))
            if key in self._tombstones or not self.registry.permits(key, asset.purpose):
                raise Rejected("source withdrawn or purpose not granted")
        else:
            if not asset.parents or asset.source_locator is not None:
                raise Rejected("derived asset requires parents, not a new source claim")
            required = {"eval": "evaluation", "train_manifest": "training",
                        "model": "training"}.get(asset.kind)
            if required and asset.purpose != required:
                raise Rejected("asset kind requires its dedicated purpose")
            for parent_id in asset.parents:
                parent = self.asset(parent_id)
                if parent.tenant != asset.tenant:
                    raise Rejected("cross-tenant lineage is forbidden")
                self._assert_usable(parent_id, asset.purpose)
        self._assets[asset.asset_id] = asset

    def read(self, tenant: str, asset_id: str, purpose: str) -> str:
        if purpose not in PURPOSES or self.asset(asset_id).tenant != tenant:
            raise Rejected("tenant or purpose mismatch")
        self._assert_usable(asset_id, purpose)
        return str(self.asset(asset_id).payload)

    def publish_eval(self, feedback: Feedback) -> str:
        if (not isinstance(feedback, Feedback)
                or not all(nonempty(value) for value in (
                    feedback.feedback_id, feedback.tenant, feedback.source_asset,
                    feedback.input_text, feedback.expected_text, feedback.evidence_ref))):
            raise Rejected("feedback evidence is incomplete")
        review = self.registry.reviews.get((feedback.tenant, feedback.feedback_id))
        if (not isinstance(review, Review) or review.decision != "verified"
                or review.method not in {"human_with_evidence", "program_with_evidence"}
                or not nonempty(review.reviewer)
                or review.content_digest != feedback.content_digest
                or review.evidence_ref != feedback.evidence_ref):
            raise Rejected("trusted review is missing, stale or not evidence based")
        asset_id = f"eval:{feedback.tenant}:{feedback.feedback_id}"
        payload = json.dumps({"input": feedback.input_text, "expected": feedback.expected_text,
                              "evidence_ref": feedback.evidence_ref,
                              "feedback_digest": feedback.content_digest}, ensure_ascii=False)
        self._admit(Asset(asset_id, feedback.tenant, "eval", "evaluation",
                          payload, (feedback.source_asset,)))
        return asset_id

    def delete(self, request_id: str, tenant: str, source_locator: str) -> dict:
        """Simulate source-scoped revocation, graph fan-out and progress receipt.

        Content deletion is represented by payload=None, not physical erasure.
        Reusing the request with the same scope resumes it after a hold release.
        """
        if not all(nonempty(value) for value in (request_id, tenant, source_locator)):
            raise Rejected("deletion request scope is required")
        key = (tenant, source_locator)
        if request_id in self._requests and self._requests[request_id] != key:
            raise Rejected("request ID reused for a different scope")
        self._requests[request_id] = key
        # First prevent future use/reimport, then process already known copies.
        self._tombstones.add(key)
        targets = sorted(asset_id for asset_id, asset in self._assets.items()
                         if asset.tenant == tenant and key in self._roots(asset_id))
        states: dict[str, str] = {}
        for asset_id in targets:
            asset = self.asset(asset_id)
            if self.registry.held(asset_id):
                updated = replace(asset, state="held")
            elif asset.kind == "model":
                updated = replace(asset, state="review_required")
            elif asset.kind == "train_manifest":
                updated = replace(asset, state="blocked", payload=None)
            else:
                updated = replace(asset, state="deleted", payload=None)
            self._assets[asset_id] = updated
            states[asset_id] = updated.state
        unresolved = sorted(asset_id for asset_id, state in states.items()
                            if state in {"held", "review_required"})
        return {"request_id": request_id, "tenant": tenant,
                "status": "partial" if unresolved else "complete_in_simulated_catalog",
                "source_tombstoned": True, "assets": states, "unresolved": unresolved,
                "scope": "registered in-memory assets only; not physical erasure"}


def fixture(*, with_model: bool = True, held: bool = True) -> tuple[Catalog, Feedback]:
    feedback = Feedback("fb-1", "tenant-a", "a-source", "Synthetic VPN symptom",
                        "Ask for the synthetic operating system", "evidence-v1")
    review = Review(feedback.content_digest, "verified", "human_with_evidence",
                    "reviewer-fixture", "evidence-v1")
    registry = TrustedRegistry(
        {("tenant-a", "synthetic:ticket-1"): PURPOSES,
         ("tenant-b", "synthetic:ticket-1"): frozenset({"service"})},
        {("tenant-a", "fb-1"): review},
        frozenset({"a-source"}) if held else frozenset())
    # The hold becomes effective after material was initially admitted.
    registry.release_hold("a-source")
    catalog = Catalog(registry)
    for tenant, prefix in (("tenant-a", "a"), ("tenant-b", "b")):
        catalog.admit(Asset(f"{prefix}-source", tenant, "source", "service",
                            "Same synthetic text", source_locator="synthetic:ticket-1"))
        for kind in ("memory", "index", "cache"):
            catalog.admit(Asset(f"{prefix}-{kind}", tenant, kind, "service",
                                "Synthetic derived text", (f"{prefix}-source",)))
    catalog.publish_eval(feedback)
    catalog.admit(Asset("a-train", "tenant-a", "train_manifest", "training",
                        "Synthetic row membership", ("a-source",)))
    if with_model:
        catalog.admit(Asset("a-model", "tenant-a", "model", "training",
                            "Synthetic model marker; no weights", ("a-train",)))
    if held:
        registry.install_hold("a-source")
    return catalog, feedback


def rejected(action) -> str:
    try:
        action()
    except Rejected as exc:
        return str(exc)
    raise AssertionError("negative demo unexpectedly passed")


def demo() -> dict:
    catalog, feedback = fixture()
    negatives = {
        "service_does_not_grant_training": rejected(lambda: catalog.admit(
            Asset("b-train", "tenant-b", "train_manifest", "training",
                  "Synthetic row", ("b-source",)))),
        "self_reported_reviewed_is_not_approval": rejected(lambda: catalog.publish_eval(
            replace(feedback, feedback_id="fb-unreviewed", self_reported_reviewed=True))),
        "cross_tenant_lineage": rejected(lambda: catalog.admit(
            Asset("cross", "tenant-b", "cache", "service", "Synthetic", ("a-index",)))),
    }
    receipt = catalog.delete("delete-1", "tenant-a", "synthetic:ticket-1")
    negatives["late_reimport"] = rejected(lambda: catalog.admit(
        Asset("late-source", "tenant-a", "source", "service", "Synthetic replay",
              source_locator="synthetic:ticket-1")))
    negatives["withdrawn_training_manifest"] = rejected(
        lambda: catalog.read("tenant-a", "a-train", "training"))
    catalog.registry.release_hold("a-source")
    resumed = catalog.delete("delete-1", "tenant-a", "synthetic:ticket-1")
    return {"expected_rejections": negatives, "first_deletion": receipt,
            "after_hold_release": resumed,
            "other_tenant_still_usable": catalog.read("tenant-b", "b-cache", "service"),
            "model_note": "review_required is unresolved; no claim of model unlearning"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("demo",))
    parser.parse_args()
    print(json.dumps(demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
