# DICOM AI evidence writeback (#59)

This document exists because opening a write path into Orthanc is a real
safety change, not a helper function, and the write it enables carries
PHI. It records the decisions this MR makes — SC over GSPS, guard
conditions, API shape, sign-off gates — so a future contributor can
follow the logic without re-deriving it, and so the PI/lead sign-off
that gates flipping the write on has a concrete artifact to sign off
against.

## What this write path is for

The Interpretation Assistant's tools produce findings. A finding at
status `COMPLETE` means the tool actually looked at the image and found
something (or, when the classifier is real, found evidence of
something). Today the finding's `evidenceRef` is a plain string —
enough to say WHERE the tool found something, but not enough to draw a
mark on the image. Rendering the mark means writing a new DICOM object
into the study so OHIF picks it up when the radiologist opens the
viewer.

This MR opens the write capability. It does not turn the write on. The
capability is held behind three deployment-level gates (see below), and
no caller wires it yet — the first caller will be the pneumothorax
classifier (#68) or a follow-up MR once #68 lands.

## Identity of the object we write

- **DICOM object class**: Secondary Capture Image Storage
  (`1.2.840.10008.5.1.4.1.1.7`). One new SOPInstance per
  (study, target-SOPInstanceUID, tool_id).
- **Series it lives in**: a new authorship-stamped Series in the same
  Study as the target. SeriesDescription starts with the exact string
  `"LH Radiology AI pre-sign impression draft"` — the discriminator
  our authorship guard reads.
- **UID root**: `2.25.<uuid5-int>`. `2.25` is the DICOM UUID-derived
  root (PS3.5 B.2) — any UUID becomes a valid UID by expressing it as
  an integer under that root. The UUID5 seed is documented in
  `libs/radagent-common/radagent_common/orthanc_client.py` and
  regenerable: `uuid5(uuid5(NAMESPACE_DNS, "librehealth.org"),
  "lh-radiology.ai-evidence-capture.v1::<parts>")`.
  Same pattern as the concept UUIDs in #55.
- **What's in the pixels**: a minimal 32x32 monochrome gradient. The
  clinical signal rides in the tags — `SeriesDescription` carries the
  human-readable label (e.g., `"LH Radiology AI pre-sign impression
  draft: Pneumothorax p=0.72"`), `ImageComments` carries the same for
  a tag browser, `SourceImageSequence` points back at the scored
  instance. Rendering an actual burned-in text overlay would be
  visually louder but adds Pillow as a dependency and picks fonts +
  layout for a feature that stays inert until sign-off. Follow-up when
  the write turns on.

## Why Secondary Capture and not GSPS

The clinical instinct is that Grayscale Softcopy Presentation State is
the "right" answer — it's non-destructive (references the original
image with overlays as separate metadata), it has a smaller PHI
footprint (no new pixel data), and it's what a mature PACS deployment
would use for AI evidence. If we were shipping this into an arbitrary
DICOM viewer, GSPS is what to reach for.

We can't. This project uses OHIF as its viewer, and **OHIF v3.11
(current) does not have a GSPS extension**. Its extension list at
v3.11.0 is `cornerstone-dicom-sr`, `cornerstone-dicom-seg`,
`cornerstone-dicom-rt`, `cornerstone-microscopy`, `dicom-pdf`,
`dicom-video` — no `cornerstone-dicom-pr`, no GSPS.  If we write a
GSPS, OHIF renders the referenced image without the overlay, and the
mark never reaches the radiologist. That fails the last item of #59's
"Then" list ("OHIF picks it up").

**Secondary Capture is a plain DICOM image**, which every DICOM viewer
including OHIF renders out of the box. It has a larger PHI footprint
than GSPS (new pixels containing patient identifiers) and it is
destructive-looking (it appears in the archive as a new image in the
study). Those are real costs, and we accept them because the
alternative doesn't render.

If OHIF adds a GSPS extension in a future release, this decision is
worth revisiting. The write path can be extended to produce GSPS
alongside SC, or to switch, without changing the interface the caller
consumes.

## `evidenceRef` scope, and where this MR differs from #59 as filed

#59's issue text describes evidenceRef as "SOPInstanceUID + frame +
coordinates". The contract at
`contracts/skills/interpretation.schema.json` has `evidenceRef:
["string", "null"]` — a plain string with no coordinates field — and
the interpretation-assistant CLAUDE.md agrees ("evidenceRef is plain
text (e.g. `order.reasonCode=J93.1`), not an image ref"). Nothing in
the codebase produces coordinates and nothing consumes them.

We keep the contract as-is. Two reasons:

1. **The SC we write does not need coordinates on the evidenceRef side.**
   Its DICOM `SourceImageSequence` carries the reference back to the
   scored instance directly. Coordinates from evidenceRef would be a
   different thing — where within the scored instance the tool found
   evidence — and the first real tool (#68's pneumothorax classifier)
   emits at instance granularity, not sub-instance coordinates.
2. **Locking a shape now is premature.** DICOM has multiple standard
   ways to encode spatial evidence (SCOORD point/polyline/circle,
   graphic annotation sequences, presentation-state graphic layers).
   Picking one before a real spatial tool needs it might constrain
   future tool authors. When such a tool arrives, that MR extends the
   contract with the shape it actually needs.

The MR description flags this diff so reviewers can push back.

## Guard conditions

The write is inert until every guard passes. This mirrors the #26 fhir2
write path — the same three-gate pattern (feature flag, transport
guard, authorship stamp) that gates writing a preliminary
DiagnosticReport into the RIS before a radiologist reads:

1. **`ORTHANC_PRESIGN_WRITE_ENABLED`** deployment feature flag,
   default False. Flipped to `1` / `true` only after PI/lead sign-off.
   Same shape as the `PRESIGN_WRITE_ENABLED` pattern that gates the
   fhir2 write, so an operator flipping one flag knows what shape the
   other one takes.
2. **Transport guard** refusing plaintext HTTP to a non-loopback host.
   Opt-out via `ORTHANC_ALLOW_INSECURE_WRITE=1` for a trusted internal
   network. Mirrors `FHIR2_ALLOW_INSECURE_WRITE` from #30 / MR !57 —
   same environment-variable semantics, same audit-log warning on
   proceed under opt-in.
3. **Authorship stamp** via SeriesDescription + our own UID root. An
   idempotent re-run of the write derives the same SOPInstanceUID for
   the same `(study, target, tool)` tuple, so Orthanc de-duplicates on
   ingest and we never accidentally accumulate duplicate captures.
   Never touches an object we did not author.

Beyond these three, the write is **best-effort**: any failure returns
`None` and logs a warning. The pre-sign impression text (the #26 fhir2
write) carries the finding regardless. The radiologist's own read is
the safety net. A failed evidence-capture write must never strand the
human read.

The **COMPLETE gate** on the tool's finding is the caller's
responsibility — same shape as the fhir2 write, where
`workflow._presign_impression` checks `_has_complete_finding()` before
invoking. When the caller wires (follow-up MR), it walks the findings
and calls the write only for `status="COMPLETE"` entries whose
`evidenceRef` is a resolvable SOPInstanceUID.

## API shape

```python
async def write_ai_evidence_capture(
    self,
    target_sop_instance_uid: str,
    orthanc_study_id: str,
    tool_id: str,
    label: str,
    confidence: Optional[float] = None,
) -> Optional[str]:
```

- **Return value**: the new SC's SOPInstanceUID on success, `None` on
  any best-effort failure. Deterministic per `(orthanc_study_id,
  target_sop_instance_uid, tool_id)`.
- **`tool_id`** is both the authorship discriminator (name in
  `ImageComments`) AND the idempotency key. Two different tools scoring
  the same target produce distinct captures; the same tool re-scoring
  produces the same capture (Orthanc de-duplicates).
- **`label` + `confidence`** ride into `SeriesDescription` and
  `ImageComments`. A radiologist scanning the OHIF study panel sees
  the finding without opening the pixels.

## Deployment

### Dev stack

Currently: the write is unused. No agent calls it. When a caller lands
(likely #68's pneumothorax classifier, or a follow-up), that MR adds
`ORTHANC_PRESIGN_WRITE_ENABLED=1` on the calling service under a
sign-off block. Until then, the capability is present but inert.

### Real deployments (post sign-off)

- Install `radagent-common` with `[imaging]` — pulls pydicom + numpy.
- Set `ORTHANC_PRESIGN_WRITE_ENABLED=1` on any service that calls the
  write.
- Set `ORTHANC_BASIC_USER` + `ORTHANC_BASIC_PASS` if Orthanc is
  authenticated in this deployment.
- Ensure Orthanc is reachable over HTTPS OR set
  `ORTHANC_ALLOW_INSECURE_WRITE=1` on a trusted internal network.
- Verify the write account is scoped to `POST /instances` only —
  read-only for everything else. Least privilege is not enforced by
  this client; it is a deployment-side concern for the Orthanc user
  config.

## Sign-off gates (what needs to be true before flipping the flag)

1. **PI + lead sign-off on the class of change** — writing
   AI-authored DICOM into the archive pre-read. Same review shape as
   the fhir2 write (#26 → #30 → !57).
2. **A caller exists.** Nothing calls the write today. When #68 lands
   with its pneumothorax classifier, it (or a companion MR) wires the
   caller.
3. **Transport is TLS in production** — same criterion as the fhir2
   write in #30. The transport guard enforces this.
4. **Live E2E on a past-setup Orthanc + OHIF stack** — walk a real
   COMPLETE finding through, confirm the SC lands in Orthanc, confirm
   OHIF renders the AI series alongside the source imaging.
5. **PHI review of what the SC carries.** Patient identifiers copied
   from the source study, plus the AI label. No new content
   introduced.
6. **Least-privilege Orthanc account** — write-only on `POST
   /instances`, no read.

## Changing the identity

Both places need to change together if the UUID seed ever rotates:

1. `libs/radagent-common/radagent_common/orthanc_client.py` —
   `_AUTHORSHIP_NAMESPACE`, `_AUTHORSHIP_SEED`, and
   `AI_EVIDENCE_SERIES_DESCRIPTION`.
2. Any tests that assert on specific UIDs (`test_orthanc_client_write.py`
   — most tests assert on the SHAPE, not the value, so this is small).

Rotating the SeriesDescription without also retiring existing captures
in Orthanc would break the authorship guard on any deployment that
already has captures under the old string. Do not rotate unless there
is a specific reason.
