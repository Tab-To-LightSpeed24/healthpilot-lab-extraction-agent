from datetime import datetime, timezone

from app.models.document import Document, DocumentStatus
from app.models.observation import Observation, MappingStatus
from app.services.fhir_export import build_fhir_bundle, to_observation_resource


def _make_observation(**overrides) -> Observation:
    defaults = dict(
        id="obs-1",
        document_id="doc-1",
        page_number=1,
        original_test_name="Hgb",
        normalized_test_name="Hemoglobin",
        value="13.5",
        unit="gm/dl",
        reference_range="12.0-15.5",
        specimen="Blood",
        loinc_code="718-7",
        loinc_display="Hemoglobin [Mass/volume] in Blood",
        mapping_status=MappingStatus.confirmed,
        mapping_confidence=0.98,
    )
    defaults.update(overrides)
    return Observation(**defaults)


def test_confirmed_numeric_observation_becomes_valuequantity_with_loinc_coding():
    obs = _make_observation()
    resource = to_observation_resource(obs)

    assert resource["resourceType"] == "Observation"
    assert resource["code"]["coding"][0]["system"] == "http://loinc.org"
    assert resource["code"]["coding"][0]["code"] == "718-7"
    assert resource["valueQuantity"] == {"value": 13.5, "unit": "g/dL"}  # unit normalized here too
    assert resource["referenceRange"] == [{"low": {"value": 12.0}, "high": {"value": 15.5}}]
    assert resource["specimen"] == {"display": "Blood"}
    assert {"url": "https://healthpilot.ai/fhir/StructureDefinition/mapping-status", "valueString": "confirmed"} in resource["extension"]


def test_unmapped_observation_has_no_loinc_coding_but_still_exports():
    """The whole point of `unmapped` is that we never fabricate a code --
    the FHIR export must reflect that honestly rather than omitting the
    observation or inventing a coding entry."""
    obs = _make_observation(loinc_code=None, loinc_display=None, mapping_status=MappingStatus.unmapped, mapping_confidence=None)
    resource = to_observation_resource(obs)

    assert "coding" not in resource["code"]
    assert resource["code"]["text"] == "Hgb"
    ext_urls = [e["url"] for e in resource["extension"]]
    assert "https://healthpilot.ai/fhir/StructureDefinition/mapping-status" in ext_urls
    assert "https://healthpilot.ai/fhir/StructureDefinition/mapping-confidence" not in ext_urls


def test_non_numeric_value_becomes_valuestring_not_a_truncated_number():
    """Reproduces a real risk in the ported logic: a value like '<0.1' or
    'Negative' must not be silently reduced to a bare, misleading number."""
    obs = _make_observation(value="Negative", unit=None, reference_range=None)
    resource = to_observation_resource(obs)
    assert resource["valueString"] == "Negative"
    assert "valueQuantity" not in resource

    obs2 = _make_observation(value="<0.1", unit="mg/dL", reference_range=None)
    resource2 = to_observation_resource(obs2)
    assert resource2["valueString"] == "<0.1"
    assert "valueQuantity" not in resource2


def test_bundle_from_document_wraps_patient_and_observations():
    doc = Document(
        id="doc-1",
        filename="report.pdf",
        content_type="application/pdf",
        uploaded_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc),
        status=DocumentStatus.complete,
        raw_content=b"fake",
    )
    obs1 = _make_observation(id="obs-1")
    obs2 = _make_observation(id="obs-2", original_test_name="Glucose", loinc_code="2345-7", value="95", unit="mg/dL")

    bundle = build_fhir_bundle(doc, [obs1, obs2])

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    resource_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert resource_types == ["Patient", "Observation", "Observation"]
