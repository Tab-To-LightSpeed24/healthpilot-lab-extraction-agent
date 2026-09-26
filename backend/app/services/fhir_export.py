"""Converts our own already-mapped Observation rows into an HL7 FHIR R4
Bundle of Patient/Observation resources.

This is a pure formatting/transform layer over data we already trust (the
LOINC code was already resolved by the 3-stage mapping pipeline before this
ever runs) -- it does not call any model and cannot introduce a new mapping
error, only a serialization one.
"""
import re
from dataclasses import dataclass
from typing import Optional

from app.models.document import Document
from app.models.observation import Observation
from app.services.normalization import standardize_unit

EXT_MAPPING_STATUS = "https://healthpilot.ai/fhir/StructureDefinition/mapping-status"
EXT_MAPPING_CONFIDENCE = "https://healthpilot.ai/fhir/StructureDefinition/mapping-confidence"

# Whole-string match only: a value is a FHIR valueQuantity only when the
# ENTIRE trimmed string is a plain signed decimal. Anything else (a leading
# "<"/">" comparator, "Negative", "Not Detected", ...) stays a valueString so
# a qualifier is never silently dropped in favor of a bare number.
_PURE_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_RANGE = re.compile(r"^\s*(?P<low>[-+]?\d+(?:\.\d+)?)\s*-\s*(?P<high>[-+]?\d+(?:\.\d+)?)\s*$")


@dataclass
class _CodedValue:
    """Result of trying to interpret a raw observation value as a FHIR
    quantity vs. leaving it as free text."""
    key: str  # "valueQuantity" or "valueString"
    payload: object


def _coerce_value(raw_value: Optional[str], raw_unit: Optional[str]) -> Optional[_CodedValue]:
    if raw_value is None:
        return None
    trimmed = raw_value.strip()
    if _PURE_NUMBER.match(trimmed):
        quantity = {"value": float(trimmed)}
        unit = standardize_unit(raw_unit)
        if unit:
            quantity["unit"] = unit
        return _CodedValue("valueQuantity", quantity)
    return _CodedValue("valueString", raw_value)


def _coerce_reference_range(raw_range: Optional[str]) -> Optional[dict]:
    if not raw_range:
        return None
    match = _RANGE.match(raw_range)
    if not match:
        return {"text": raw_range}
    return {
        "low": {"value": float(match.group("low"))},
        "high": {"value": float(match.group("high"))},
    }


def _loinc_coding(obs: Observation) -> list[dict]:
    if not obs.loinc_code:
        return []
    return [{
        "system": "http://loinc.org",
        "code": obs.loinc_code,
        "display": obs.loinc_display or obs.normalized_test_name or obs.original_test_name,
    }]


def _audit_extensions(obs: Observation) -> list[dict]:
    status = obs.mapping_status.value if hasattr(obs.mapping_status, "value") else obs.mapping_status
    extensions = [{"url": EXT_MAPPING_STATUS, "valueString": status}]
    if obs.mapping_confidence is not None:
        extensions.append({"url": EXT_MAPPING_CONFIDENCE, "valueDecimal": obs.mapping_confidence})
    return extensions


def to_observation_resource(obs: Observation) -> dict:
    coding = _loinc_coding(obs)
    resource: dict = {
        "resourceType": "Observation",
        "id": f"obs-{obs.id}",
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory",
                "display": "Laboratory",
            }],
        }],
        "code": {"text": obs.original_test_name, **({"coding": coding} if coding else {})},
        "subject": {"reference": "Patient/patient-1"},
        "extension": _audit_extensions(obs),
    }

    coded = _coerce_value(obs.value, obs.unit)
    if coded:
        resource[coded.key] = coded.payload

    ref_range = _coerce_reference_range(obs.reference_range)
    if ref_range:
        resource["referenceRange"] = [ref_range]
    if obs.specimen:
        resource["specimen"] = {"display": obs.specimen}
    if obs.page_number is not None:
        resource["note"] = [{"text": f"Source page {obs.page_number}"}]

    return resource


def build_fhir_bundle(document: Document, observations: list[Observation]) -> dict:
    patient_resource = {"resourceType": "Patient", "id": "patient-1"}
    entries = [{"resource": patient_resource}]
    entries.extend({"resource": to_observation_resource(obs)} for obs in observations)

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": document.uploaded_at.isoformat() if document.uploaded_at else None,
        "entry": entries,
    }
