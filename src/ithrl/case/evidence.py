"""Loaded case evidence and deterministic identity."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from ithrl.case.contracts import CaseFormatError, CaseManifest
from ithrl.case.source import SourceArtifact, prepare_source_artifacts
from ithrl.case.source_context import validate_source_references
from ithrl.context import (
    ContextGraph,
    ContextNode,
    JsonObject,
    content_digest,
    validate_json_limits,
)
from ithrl.decisions import DecisionEvidence


def case_digest(
    manifest: CaseManifest,
    request: JsonObject | None,
    nodes: Iterable[ContextNode],
    source_artifacts: Iterable[SourceArtifact] = (),
    decision: DecisionEvidence | None = None,
) -> str:
    """Return a deterministic identity for immutable case evidence."""

    sources = prepare_source_artifacts(source_artifacts)
    payload: dict[str, Any] = {
        "manifest": manifest.model_dump(mode="json", exclude_none=True),
        "context": [node.model_dump(mode="json") for node in nodes],
    }
    if request is not None:
        payload["request"] = request
    if sources:
        payload["source"] = [artifact.identity.model_dump(mode="json") for artifact in sources]
    if decision is not None:
        payload["decision"] = decision.model_dump(mode="json")
    return content_digest(payload)


class CaseEvidence:
    """Loaded immutable evidence with defensive request copies."""

    __slots__ = (
        "_context",
        "_decision",
        "_default_experiment",
        "_digest",
        "_manifest",
        "_request",
        "_source_artifacts",
    )

    def __init__(
        self,
        *,
        manifest: CaseManifest,
        request: JsonObject | None,
        context: ContextGraph,
        default_experiment: str,
        source_artifacts: Iterable[SourceArtifact] = (),
        decision: DecisionEvidence | None = None,
    ) -> None:
        sources = prepare_source_artifacts(source_artifacts)
        validate_case_inputs(
            manifest=manifest,
            request=request,
            context=context,
            source_artifacts=sources,
            decision=decision,
        )
        self._manifest = manifest
        self._request = copy.deepcopy(request) if request is not None else None
        self._context = context
        self._decision = decision
        self._default_experiment = default_experiment
        self._source_artifacts = sources
        self._digest = case_digest(manifest, request, context.nodes, sources, decision)

    @property
    def manifest(self) -> CaseManifest:
        return self._manifest

    @property
    def request(self) -> JsonObject:
        if self._request is None:
            raise CaseFormatError("case does not contain an exact provider request")
        return copy.deepcopy(self._request)

    @property
    def has_request(self) -> bool:
        return self._request is not None

    @property
    def decision(self) -> DecisionEvidence | None:
        return self._decision

    @property
    def context(self) -> ContextGraph:
        return self._context

    @property
    def default_experiment(self) -> str:
        return self._default_experiment

    @property
    def source_artifacts(self) -> tuple[SourceArtifact, ...]:
        return self._source_artifacts

    @property
    def digest(self) -> str:
        return self._digest

    def materialize(self, remove_ids: Iterable[str] = ()) -> JsonObject:
        return self._context.materialize(self.request, remove_ids)


def validate_case_inputs(
    *,
    manifest: CaseManifest,
    request: JsonObject | None,
    context: ContextGraph,
    source_artifacts: tuple[SourceArtifact, ...],
    decision: DecisionEvidence | None,
) -> None:
    """Validate relationships between the immutable case inputs."""

    _validate_capture_shape(manifest, request, decision)
    if request is not None:
        validate_json_limits(request)
        context.validate_request(request)
    elif any(node.path is not None for node in context.nodes):
        raise CaseFormatError("request-backed context requires request.json")
    if request is None and decision is None:
        raise CaseFormatError("case evidence requires request.json or decision.json")
    validate_source_references(context, decision, source_artifacts)


def _validate_capture_shape(
    manifest: CaseManifest,
    request: JsonObject | None,
    decision: DecisionEvidence | None,
) -> None:
    capture_type = manifest.capture.request
    if (request is not None) != (capture_type == "provider-request"):
        raise CaseFormatError("capture classification does not match request.json")
    if capture_type == "logical-session":
        if decision is None:
            raise CaseFormatError("logical-session capture requires decision.json")
        if not all((manifest.harness, manifest.harness_version, manifest.source_schema)):
            raise CaseFormatError("logical-session capture requires harness source metadata")
        if manifest.source_schema != manifest.provenance.trace_schema:
            raise CaseFormatError("logical-session source schema does not match provenance")
        if manifest.capture.completeness != decision.completeness:
            raise CaseFormatError("logical-session completeness does not match decision evidence")
        if manifest.provenance.inference_call_id is not None:
            raise CaseFormatError(
                "logical-session capture cannot identify a provider inference call"
            )
    if decision is None:
        return
    if decision.selector.harness != manifest.harness:
        raise CaseFormatError("decision harness does not match case metadata")
    if decision.selector.source != manifest.provenance.source:
        raise CaseFormatError("decision source does not match case provenance")
