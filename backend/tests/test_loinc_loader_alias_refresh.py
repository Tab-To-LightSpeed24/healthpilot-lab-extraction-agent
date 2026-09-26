"""Regression test for a real bug found during live evaluation: urine Glucose
was mis-mapped to the serum Glucose LOINC code partly because a colloquial
synonym ("Sugar (Fasting)") wasn't in the alias table, and seed_loinc_table
originally skipped alias refresh entirely once the LoincCode rows existed --
so editing app/data/loinc_lab_active.csv to add a synonym had no effect on an
already-seeded (e.g. already-deployed) database."""
from unittest.mock import patch

from app.models.loinc import LoincAlias
from app.services.loinc_loader import seed_loinc_table


def test_reseeding_refreshes_aliases_even_when_codes_already_exist(db_session):
    seed_loinc_table(db_session)  # first seed, table empty -> inserts codes + aliases

    fake_records = [
        {"loinc_num": "2345-7", "long_common_name": "Glucose [Mass/volume] in Serum or Plasma",
         "shortname": "Glucose", "aliases": ["Glucose", "Totally New Synonym"]},
    ]
    # seed_loinc_table streams via _iter_loinc_rows() (not load_loinc_records())
    # so its memory use stays bounded regardless of table size -- see that
    # function's docstring. Patch the actual data source it reads from.
    with patch("app.services.loinc_loader._iter_loinc_rows", side_effect=lambda: iter(fake_records)):
        seed_loinc_table(db_session)  # second seed, LoincCode rows already exist

    aliases = {a.alias for a in db_session.query(LoincAlias).filter(LoincAlias.loinc_num == "2345-7").all()}
    assert "Totally New Synonym" in aliases, (
        "a new alias added to loinc_lab_active.csv must take effect on the next "
        "deploy even though the LoincCode rows were already seeded"
    )
