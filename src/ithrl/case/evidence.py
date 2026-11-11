"""Loaded case evidence and deterministic identity."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from weakref import ReferenceType, ref

from ithrl.case.contracts import CaseFormatError, CaseManifest
from ithrl.case.source import SourceArtifact, prepare_source_artifacts
from ithrl.case.source_context import validate_source_references
from ithrl.context import (
    ContextGraph,
    ContextNode,
    JsonObject,
    content_digest,
    decode_pointer,
    validate_json_limits,
)
from ithrl.decisions import DecisionEvidence
from ithrl.mutation import (
    EditableText,
    MutationError,
    TextAppend,
    TextMutation,
    append_message_text,
    editable_message_text,
    message_text,
    replace_message_text,
    request_text_item,
)
from ithrl.text_sections import (
    TextSectionCatalog,
    TextSectionError,
    TextSectionLimitError,
    TextSectionRemoval,
    catalog_text_sections,
    remove_text_sections,
)


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


@dataclass(frozen=True, slots=True)
class _VerifiedEditableText:
    """Derived text bound to one owner and its exact immutable evidence inputs."""

    owner: ReferenceType[CaseEvidence]
    manifest: CaseManifest
    context: ContextGraph
    decision: DecisionEvidence | None
    sources: tuple[SourceArtifact, ...]
    sites: tuple[EditableText, ...]


class CaseEvidence:
    """Loaded immutable evidence with defensive request copies."""

    __slots__ = (
        "__weakref__",
        "_context",
        "_decision",
        "_default_experiment",
        "_digest",
        "_manifest",
        "_request",
        "_source_artifacts",
        "_verified_editable_text",
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
        self._verified_editable_text: _VerifiedEditableText | None = None

    @property
    def manifest(self) -> CaseManifest:
        return self._manifest

    @property
    def request(self) -> JsonObject:
        return copy.deepcopy(self._exact_request())

    def _exact_request(self) -> JsonObject:
        """Borrow preserved input for internal operations that do not mutate it."""

        if self._request is None:
            raise CaseFormatError("case does not contain an exact provider request")
        return self._request

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

    def materialize(
        self, remove_ids: Iterable[str] = (), *, mutation: TextMutation | None = None
    ) -> JsonObject:
        requested = tuple(remove_ids)
        mutation = (
            TextMutation()
            if mutation is None
            else TextMutation.model_validate(mutation.model_dump())
        )
        if any(isinstance(edit, TextAppend) for edit in mutation.edits) and requested:
            raise MutationError("text mutation requires the unchanged original context")
        removed = self._context.dependency_closure(requested)
        if any(isinstance(edit, TextSectionRemoval) for edit in mutation.edits):
            self._protect_section_fixed_input(removed)
        request = self._context.materialize(self._exact_request(), removed)
        for edit in mutation.edits:
            node = self._context.by_id.get(edit.context_id)
            if node is None:
                raise MutationError("mutation addresses an unknown context node")
            if node.id in removed:
                raise MutationError("section removal overlaps a whole-record dependency closure")
            index, original = request_text_item(self._exact_request(), node)
            inputs = request["input"]
            if not isinstance(inputs, list):
                raise MutationError("mutation requires an input-message list")
            if isinstance(edit, TextAppend):
                inputs[index] = append_message_text(original, edit)
            else:
                catalog = self.validate_text_sections(edit)
                retained = remove_text_sections(
                    message_text(original, edit.content_index), catalog, edit
                )
                # Graph paths refer to the immutable original. Whole earlier input
                # removals shift the copied list, never the section's source identity.
                prior_removed = sum(
                    1
                    for other in self._context.nodes
                    if other.id in removed
                    and other.path is not None
                    and (tokens := decode_pointer(other.path))[:1] == ("input",)
                    and len(tokens) == 2
                    and tokens[1].isascii()
                    and tokens[1].isdigit()
                    and int(tokens[1]) < index
                )
                copied = inputs[index - prior_removed]
                if not isinstance(copied, dict):
                    raise MutationError("mutation requires an ordinary user message")
                inputs[index - prior_removed] = replace_message_text(
                    copied, edit.content_index, retained
                )
        return request

    def _protect_section_fixed_input(self, removed: frozenset[str]) -> None:
        inputs = self._exact_request().get("input")
        if not isinstance(inputs, list) or not inputs:
            return
        fixed = ("input", str(len(inputs) - 1))
        for node in self._context.nodes:
            if node.id not in removed or node.path is None:
                continue
            path = decode_pointer(node.path)
            if path[: len(fixed)] == fixed or fixed[: len(path)] == path:
                raise MutationError("section removal cannot change the current decision input")

    def editable_text(self) -> tuple[EditableText, ...]:
        """Discover prior-user text from preserved evidence without runtime operations."""

        if self._manifest.capture.request == "logical-session":
            from ithrl.extraction import editable_extracted_text

            verified = self._verified_editable_text
            if (
                verified is not None
                and verified.owner() is self
                and verified.manifest is self._manifest
                and verified.context is self._context
                and verified.decision is self._decision
                and verified.sources is self._source_artifacts
            ):
                return verified.sites
            # Retain only immutable text facts, never the adapter's mutable native
            # session. Rebound evidence and copied owners must verify afresh.
            sites = editable_extracted_text(self)
            self._verified_editable_text = _VerifiedEditableText(
                ref(self),
                self._manifest,
                self._context,
                self._decision,
                self._source_artifacts,
                sites,
            )
            return sites
        request = self._exact_request()
        found = []
        for node in self._context.nodes:
            try:
                _, message = request_text_item(request, node)
            except MutationError:
                continue
            found.extend(editable_message_text(node, message))
        return tuple(found)

    def text_sections(self) -> tuple[TextSectionCatalog, ...]:
        """List complete catalogs for eligible fields within the paragraph policy limits."""

        by_id = self._context.by_id
        catalogs = []
        for site in self.editable_text():
            try:
                catalogs.append(self._text_section_catalog(site, by_id[site.context_id]))
            except TextSectionLimitError:
                continue
            except TextSectionError as error:
                raise MutationError(str(error)) from None
        return tuple(catalogs)

    def _text_section_catalog(self, site: EditableText, node: ContextNode) -> TextSectionCatalog:
        return catalog_text_sections(
            case_digest=self.digest,
            context_id=node.id,
            context_digest=node.digest,
            content_index=site.content_index,
            text=site.original_text,
        )

    def validate_text_sections(self, edit: TextSectionRemoval) -> TextSectionCatalog:
        """Re-derive eligibility and the selected units from the immutable case."""

        for site in self.editable_text():
            if (site.context_id, site.content_index) == (edit.context_id, edit.content_index):
                try:
                    catalog = self._text_section_catalog(site, self._context.by_id[site.context_id])
                    catalog.validate_removal(edit)
                except TextSectionError as error:
                    raise MutationError(str(error)) from None
                return catalog
        raise MutationError("section removal requires an editable prior-user text field")


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
