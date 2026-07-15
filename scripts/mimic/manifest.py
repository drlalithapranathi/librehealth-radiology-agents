"""Cohort manifest for the MIMIC-CXR showcase ETL (#68).

The manifest is what a curator produces from MIMIC (study list + labels + report text + the MIMIC-IV
clinical slice). The loader tooling consumes it. The manifest and any real MIMIC content stay OFF
this repo (PhysioNet DUA); only the schema and a synthetic sample live here.

A manifest is JSON: {"studies": [ <CohortStudy>, ... ]}. One entry per MIMIC study.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import json


@dataclass
class Lab:
    code: str            # LOINC (e.g. 2160-0 creatinine)
    value: float
    unit: str = ""
    date: str = ""       # ISO; blank -> loader stamps the study date
    display: str = ""


@dataclass
class Med:
    code: str = ""       # RxNorm if known
    display: str = ""    # drug name (anticoagulants make the med-flag rules fire)


@dataclass
class Problem:
    code: str            # ICD-10 (from MIMIC-IV diagnoses_icd)
    display: str = ""


@dataclass
class CohortStudy:
    study_id: str                       # MIMIC study id, e.g. s56699142 -> AccessionNumber
    subject_id: str                     # MIMIC subject id -> Patient identifier
    description: str = "CHEST (PA AND LAT)"   # StudyDescription default (registry selects on it)
    portable: bool = False              # portable AP -> CHEST (PORTABLE AP)
    priority: str = "routine"           # order urgency: routine|stat|urgent|asap
    reason_codes: list[str] = field(default_factory=list)  # ICD-10 on the ORDER (J93*/J95.811 fire pneumothorax-detect)
    report_text: str = ""               # the MIMIC report (FINDINGS + IMPRESSION); report_body.py parses the headers
    labels: dict = field(default_factory=dict)  # chexpert/negbio labels (cohort composition + concordance)
    prior_study_ids: list[str] = field(default_factory=list)  # priors story for the EHR assistant
    labs: list[Lab] = field(default_factory=list)
    meds: list[Med] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def study_description(self) -> str:
        from dicom_fixup import PORTABLE_DESCRIPTION  # local: keep the convention in one module
        if self.portable and self.description == "CHEST (PA AND LAT)":
            return PORTABLE_DESCRIPTION
        return self.description


def _obj(cls, d: dict):
    return cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})


def load_manifest(path: str) -> list[CohortStudy]:
    with open(path) as f:
        raw = json.load(f)
    return [_study_from_dict(s) for s in raw.get("studies", [])]


def _study_from_dict(d: dict) -> CohortStudy:
    s = _obj(CohortStudy, d)
    s.labs = [_obj(Lab, x) for x in d.get("labs", [])]
    s.meds = [_obj(Med, x) for x in d.get("meds", [])]
    s.problems = [_obj(Problem, x) for x in d.get("problems", [])]
    return s
