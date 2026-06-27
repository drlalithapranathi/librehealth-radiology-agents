# ARCHITECTURE

Diagrams for the LH-Radiology multi-agent system. Contracts: `/contracts`. Decisions + glossary: `CLAUDE.md`. Backlog: GitLab issues.

## Components

```mermaid
flowchart LR
  subgraph Edge["Imaging + EHR"]
    ORTHANC[(Orthanc PACS)]
    OHIF[OHIF Viewer]
    OPENMRS[OpenMRS / LH-Radiology RIS + fhir2]
  end

  subgraph Core["Orchestration"]
    INGRESS[Ingress: Orthanc webhook + RIS poller]
    TEMPORAL[(Temporal)]
    WF[StudyWorkflow: state machine]
    WLAPI[Worklist API]
  end

  subgraph Agents["A2A Agents"]
    TRIAGE[Worklist Triage]
    EHR[EHR Assistant]
    INTERP[Interpretation Assistant]
    IMPR[Impression Generation]
    VERIFY[Report Verification]
    COMMS[Communications - existing]
  end

  ORTHANC -- OnStableStudy webhook --> INGRESS
  OPENMRS -- DiagnosticReport=final poll --> INGRESS
  INGRESS --> TEMPORAL --> WF
  WF -- A2A skill calls --> TRIAGE & EHR & INTERP & IMPR & VERIFY & COMMS
  EHR -. read-only .-> OPENMRS
  INTERP -. metadata .-> ORTHANC
  WF -- priority --> WLAPI --> OHIF
  OHIF -. opens study .-> ORTHANC
  Radiologist[Radiologist] -- authors & signs --> OPENMRS
  OHIF --> Radiologist
```

## State machine (one workflow instance per study)

```mermaid
stateDiagram-v2
  [*] --> RECEIVED: Orthanc OnStableStudy
  RECEIVED --> READY_FOR_READ: fan-out triage / ehr / interpretation
  READY_FOR_READ --> AWAITING_RADIOLOGIST: publish priority to Worklist API
  AWAITING_RADIOLOGIST --> IMPRESSION: RIS report status=final (signal)
  IMPRESSION --> VERIFY: impression.generate
  VERIFY --> COMMUNICATE: PASS
  VERIFY --> AWAITING_SIGNOFF: WARN/FAIL needs human review
  AWAITING_SIGNOFF --> VERIFY: addendum / ack (or escalate on timeout)
  COMMUNICATE --> ARCHIVED: comms.dispatch
  ARCHIVED --> [*]

  note right of AWAITING_RADIOLOGIST: human-gated (durable wait + signal)
  note right of AWAITING_SIGNOFF: human-gated (escalation timer)
```

## Sequence — happy path

```mermaid
sequenceDiagram
  participant O as Orthanc
  participant I as Ingress
  participant W as StudyWorkflow
  participant A as Agents (A2A)
  participant R as RIS (OpenMRS)
  participant C as Communications

  O->>I: OnStableStudy (webhook)
  I->>W: start workflow (id = wf_<orthancStudyId>)
  par pre-read fan-out
    W->>A: triage.score
    W->>A: ehr.assembleContext
    W->>A: interpretation.runTools
  end
  W->>W: READY_FOR_READ (publish priority)
  Note over R: radiologist authors & signs report
  I->>R: poll DiagnosticReport status=final
  I-->>W: signal report_finalized
  W->>A: impression.generate
  W->>A: report.verify
  alt PASS
    W->>C: comms.dispatch
  else WARN/FAIL + human review
    W->>W: AWAITING_SIGNOFF (timer)
    W->>A: report.verify (re-run)
  end
  W->>W: ARCHIVED
```

## Trigger map
Summary: **Orthanc** Python plugin → ingress webhook starts the workflow;
the **RIS poller** (`fhir2 DiagnosticReport?status=final&_lastUpdated=gt{cursor}`) signals the
waiting workflow; **OHIF** reads the **Worklist API** for the priority-ordered reading list
(M2: emits `StudyOpenedEvent` for pre-sign assist).

## Deployment (dev)
`docker-compose.yml` brings up Orthanc, OHIF, OpenMRS + MariaDB, and the Temporal stack
(server + Postgres + UI). Orchestrator (ingress + worker) and the six agents run as services
in M1 (Dockerfiles added then). Temporal hosting is self-hosted for dev; Temporal Cloud is a
prod option. Image tags in compose are starting points — pin them per environment.
