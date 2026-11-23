# Selfie Video Development Plan

## Goal

Create an independent AstrBot video-generation plugin that reuses the image plugin's mature configuration, task, records, storage, permission, and Web management patterns without retaining image-generation commands or providers.

## Implementation Phases

1. **Plugin foundation**
   - Create `astrbot_plugin_selfie_video` as a separate installable repository.
   - Reuse generic AstrBot lifecycle, configuration, access control, task/record persistence, Web API, embedded Dashboard, and backup patterns where they fit video workflows.
   - Replace image-facing metadata, help, configuration, and UI labels with video equivalents; keep secrets server-side and masked.

2. **Agnes video channel**
   - Port the proven Agnes request-family handling from image-plugin history, including Agnes v2.0 and v2.5 payload differences.
   - Implement create -> poll -> download as one task flow, with bounded polling, cancellation, timeouts, status/error extraction, and no blind resubmission after an ambiguous create timeout.
   - Support text-to-video first and image/keyframe references only where the selected model/protocol permits them; reject unsupported local-media inputs before submission.

3. **OpenAI-compatible video channel**
   - Implement the common asynchronous `/v1/videos/generations` create-and-poll contract, accepting common task IDs, status fields, and result URL shapes.
   - Keep provider/protocol selection explicit in channel configuration, with per-model overrides; do not infer Agnes solely from a model name.
   - Add synchronous and chat-based variants only when required by observed gateway behavior, keeping transport details behind the same provider interface.

4. **Commands and management UI**
   - Add video generation, task list/cancel, retry, and history workflows with permission/rate/concurrency controls.
   - Reuse the existing channel editor, task lifecycle views, record filters, protected media delivery, and dashboard authentication patterns; adapt media preview and download for MP4.
   - Preserve model priority/fallback, but do not retry a possibly billable create request after network ambiguity.

5. **Verification and release**
   - Add offline tests for payload construction, endpoint normalization, polling states, error handling, cancellation, secret redaction, download validation, and UI/API contracts.
   - Verify compatibility against the supplied test endpoint/model without putting its API key in source, fixtures, docs, commits, or logs. Run one short end-to-end generation only after confirming the endpoint's expected protocol; video generation may incur provider charges.
   - Run the repository test/release checks, update README/metadata/version, then commit and push each completed milestone.

## Initial Acceptance Criteria

- Plugin installs and starts independently of `astrbot_plugin_selfie_image`.
- Agnes text-to-video succeeds through create, poll, MP4 download, record persistence, and chat delivery.
- OpenAI-compatible async video succeeds through the same task/record/delivery path.
- Key material is masked in the UI and absent from tracked files, logs, test snapshots, and Git history.
- Core task, channel, and UI behavior is covered by repeatable offline tests.
