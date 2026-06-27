# OHIF Extension (M2)

Placeholder. In M2 this becomes a custom OHIF extension that:

1. **Worklist data source** — reads the **Worklist API** (`/worklist`) so the reading
   list is ordered by orchestrator priority.
2. **Priors / overlays** — surfaces EHR Assistant priors and Interpretation Assistant
   overlay/SC references at read time.
3. **`StudyOpenedEvent`** — emits `contracts/events/ohif-opened.schema.json` when a
   radiologist opens a study (enables read-time, pre-sign assist).

Owner: Parvati. Not in the M0 scope.
