from sqlalchemy import Column, String, ForeignKey, Integer
from sqlalchemy.orm import relationship

from app.core.db import Base


class LoincCode(Base):
    __tablename__ = "loinc_codes"

    loinc_num = Column(String(32), primary_key=True)
    long_common_name = Column(String(512), nullable=False)
    shortname = Column(String(256), nullable=True)
    component = Column(String(256), nullable=True)
    property = Column(String(64), nullable=True)
    time_aspect = Column(String(64), nullable=True)
    system = Column(String(128), nullable=True)  # specimen/system, e.g. Serum/Plasma
    scale_type = Column(String(64), nullable=True)
    method_type = Column(String(128), nullable=True)
    class_ = Column("class", String(128), nullable=True)
    example_units = Column(String(128), nullable=True)
    # LOINC's own "how often this is actually ordered" signal (0 = unranked).
    # Used only as a tiebreaker to surface commonly-used tests over obscure
    # ones within an already lexically-relevant candidate shortlist -- NOT
    # to override a specific alias match (see normalization.build_alias_index
    # for why that specific use was tried and reverted).
    common_test_rank = Column(Integer, nullable=False, default=0)

    aliases = relationship(
        "LoincAlias", back_populates="loinc_code", cascade="all, delete-orphan"
    )


class LoincAlias(Base):
    __tablename__ = "loinc_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    loinc_num = Column(String(32), ForeignKey("loinc_codes.loinc_num"), nullable=False)
    alias = Column(String(256), nullable=False)

    loinc_code = relationship("LoincCode", back_populates="aliases")
