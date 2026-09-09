# Evidence Set format

Evidence Set is the small, ordered handoff layer between canonical experiment
records and Report. It stores presentation metadata and references; it does not
copy terminal snapshots, API request/response payloads, or rendered images.

## Source references

The persisted source union is deliberately closed:

```json
{
  "source_type": "lab_capture",
  "session_id": "session-2026-09-01",
  "capture_id": "capture-1"
}
```

or:

```json
{
  "source_type": "api_step",
  "run_id": "run-2026-09-01",
  "step_index": 1
}
```

The complete source identity is used for duplicate detection. Lab identity is
`(source_type, session_id, capture_id)`. API identity is
`(source_type, run_id, step_index)`. API `step_index` is one-based, matching
`ApiEvidenceBuilder` and the existing API evidence convention.

An Evidence Item owns only its `title`, `caption`, and `note` in addition to
the source reference. Editing, removing, or reordering an item never changes
the canonical source.

## Schema versions

New Evidence Set documents and documents explicitly saved by v0.6 use version
`2`. Version `1` was the Lab-only format. v0.6 reads version 1 through a
deterministic in-memory compatibility normalization and preserves its item
order, metadata, timestamps, and Lab source identity. Listing and loading do
not rewrite the old file. An explicit save may migrate it to version 2 using
the normal atomic save path.

Unknown future versions, unknown fields, unknown source types, unsafe source
identifiers, oversized documents, and corrupt documents fail closed. Each file
is isolated while listing, so one corrupt Evidence Set does not hide healthy
sets.

## Resolution and safety

Lab references resolve through the existing SessionRepository and CaptureStore.
API references resolve through the existing ApiRunRepository, which reads the
saved, already-redacted `ApiRun`; the resolver then reuses
`ApiEvidenceBuilder` and its defensive redaction boundary. Evidence browsing
and Report export never rerun an API scenario and never persist a second API
payload representation.

Report preflights every source before staging. An unavailable source remains in
the Evidence Set with a controlled reason such as `session_missing`,
`capture_missing`, `run_missing`, or `step_missing`; it is never silently
removed or substituted. Mixed Lab/API items are rendered in the exact array
order stored by the Evidence Set.
