"""Loaded case evidence and deterministic identity."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from ithrl.case.contracts import CaseManifest
from ithrl.case.source import SourceArtifact, prepare_source_artifacts
from ithrl.context import (
    ContextGraph,
    ContextNode,
    JsonObject,
    content_digest,
    validate_json_limits,
)


def case_digest(
    manifest: CaseManifest,
    request: JsonObject,
    nodes: Iterable[ContextNode],
    source_artifacts: Iterable[SourceArtifact] = (),
) -> str:
    """Return a deterministic identity for immutable case evidence."""

    sources = prepare_source_artifacts(source_artifacts)
    payload: dict[str, Any] = {
        "manifest": manifest.model_dump(mode="json", exclude_none=True),
        "request": request,
        "context": [node.model_dump(mode="json") for node in nodes],
    }
    if sources:
        payload["source"] = [artifact.identity.model_dump(mode="json") for artifact in sources]
    return content_digest(payload)


class CaseEvidence:
    """Loaded immutable evidence with defensive request copies."""

    __slots__ = (
        "_context",
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
        request: JsonObject,
        context: ContextGraph,
        default_experiment: str,
        source_artifacts: Iterable[SourceArtifact] = (),
    ) -> None:
        validate_json_limits(request)
        context.validate_request(request)
        sources = prepare_source_artifacts(source_artifacts)
        self._manifest = manifest
        self._request = copy.deepcopy(request)
        self._context = context
        self._default_experiment = default_experiment
        self._source_artifacts = sources
        self._digest = case_digest(manifest, request, context.nodes, sources)

    @property
    def manifest(self) -> CaseManifest:
        return self._manifest

    @property
    def request(self) -> JsonObject:
        return copy.deepcopy(self._request)

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
        return self._context.materialize(self._request, remove_ids)
