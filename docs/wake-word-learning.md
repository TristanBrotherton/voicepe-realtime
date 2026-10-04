# Wake-word learning

How false wakes become better wake-word models without ever shipping one that
is worse than the model you have, and without your audio leaving your home.

The loop has five separate decisions, each made on evidence:

1. **Capture** — what the add-on keeps about each wake (default: counters and
   metadata only).
2. **Label** — which wakes were false. Only a person labels; silence alone never does.
3. **Train** — outside this repository, with the community trainer.
4. **Calibrate and gate** — pick one exact operating point (cutoff *and*
   window) and compare it with the deployed model under a fixed gate.
5. **Deploy** — shadow, then one-device canary, then everywhere, with an
   automatic rollback trigger.

Changing the sensitivity select on a device is a sixth, separate decision: it
moves the operating point of the model you already have.

---

## 1. What the device reports with every wake

Firmware protocol v2 sends this metadata with each wake (no audio):

| Field | Meaning |
|---|---|
| `turn` | Turn id (`<boot id>-<counter>`), unique per device boot |
| `src` | `wake_word` or `button` |
| `model`, `model_sha` | Active model and the first 8 hex digits of its SHA-256 (`stock` for Hey Jarvis / Okay Nabu) |
| `cutoff`, `window` | The exact probability cutoff and sliding window in use |
| `tier` | The sensitivity select (`slight`, `moderate`, `very`) |
| `fire_to_mic_ms` | Detection to microphone open on the device |
| `fw` | Firmware version |

The add-on publishes the operating point as `sensor.voicepe_<instance>_wake_word`
and records it with every wake event, so a false-wake rate is always tied to the
model and threshold that produced it.

## 2. What is stored, and for how long

All of it stays on your Home Assistant host, under `/share/voice-probes/`.
`wake_capture` decides how much:

| `wake_capture` | Stored |
|---|---|
| `off` | Nothing (also what guest mode switches to) |
| `metadata` | Counters plus one line per wake event: time, device, turn id, model, cutoff, window, tier, outcome, label. No audio. |
| `audio` | Metadata plus a short clip of the audio **after** each wake, for reviewing false wakes |
| `auto` (default) | `audio` if the legacy `enable_recording` debug option is on, otherwise `metadata` |

`trigger_capture` adds the ~1.5 s **before** a wake: the sound the model
actually fired on, which is what a hard negative needs. It requires
`wake_capture: audio` **and** the device's own "Share wake trigger audio"
switch, so neither side can enable it alone.

Retention runs automatically:

| Pool | Kept for | Cap |
|---|---|---|
| Unlabeled clips (`probe_`, `trigger_`, `candidate_`) | `wake_capture_ttl_days` (30) | 500 files |
| Labeled false wakes (`falsewake_`) | `wake_label_ttl_days` (180) | 1000 files |

The pools are separate: a burst of ordinary wakes can never push a labeled
false wake out. **Guest mode:** set `guest_mode_entity` to an `input_boolean`;
while it is on (checked every 30 s), nothing about wakes is stored.

Administration from inside the add-on container:

```
python3 -m app.wake_events prune                     # apply retention now
python3 -m app.wake_events purge [--include-labeled] # delete clips
python3 -m app.wake_events report [--days 7]         # metadata-only aggregate
```

## 3. Labels

A wake is labeled **false** only by a person, and only on the device that woke:

| Method | Rule |
|---|---|
| Double-press | Labels the turn the firmware names. A press made while offline is queued on the device and accepted for up to 10 minutes. |
| Button during the wake | Silencing a session within 12 s of the wake, before any reply audio, labels that wake. |
| Voice | "That was a false alarm" labels this device's newest wake if it happened within `false_wake_flag_window_s` (30 s). |

A wake that simply ends without speech is recorded as a **candidate** for
review. Candidates are never used as training negatives: people often wake the
device and then change their mind, and treating that as a false wake would
teach the model to ignore real requests.

Labels are scoped by device and turn id, so with several devices on one add-on
a flag in the kitchen can never label a wake in the office.

## 4. Training

Training happens outside this repository, with the community
[microWakeWord trainer](https://github.com/TaterTotterson/microWakeWord-Trainer-AppleSilicon)
(about two hours on an Apple Silicon or NVIDIA machine). Typical inputs:

- synthetic positives from text-to-speech voices,
- real positives from voice enrollment sessions (`/share/voice-enrollment/`),
- labeled false wakes (`falsewake_*` clips) and ordinary household speech as
  negatives.

None of these files belong in a public repository. The firmware repository's
`.gitignore` and CI scan reject audio outside `sounds/`.

## 5. Calibration and the release gate

A trained model is calibrated by evaluating **every** cutoff/window pair on a
validation set (positive recall) and an ambient corpus (false accepts per
hour, FA/h). The release gate is fixed policy. One exact pair must meet, against
the deployed model:

- recall ≥ baseline recall − 0.01, and
- FA/h ≤ baseline FA/h + 0.05,

and actually **improve** on the deployed model. "Passes the gate but is not
better" means keep the incumbent. The firmware repository implements this in
`tools/wakeword/gate.py`; there is no option to widen the margins.

Mind the resolution: on a 9.67-hour ambient corpus one extra false accept
moves FA/h by 0.103, more than the 0.05 margin. On a corpus that size the gate
effectively allows **no** additional false accepts. A longer ambient corpus
gives finer resolution.

The chosen pair is written into the model's JSON *and* into a metrics-only
evaluation manifest (`models/<name>.eval.json`). CI fails if the two disagree,
if the firmware's recorded SHA-256 or window disagree, or if the model is not
pinned to an immutable commit.

### Recall by distance (evaluation protocol)

Validation recall says little about "hard to invoke across the room". To
measure it without publishing audio:

1. Record **held-out** positives: speakers or sessions that were *not* used for
   training. Never reuse training repetitions for evaluation.
2. Record each speaker in three strata, in the rooms where the devices live:
   **close** (≤ 1 m, facing the device), **conversational** (1–3 m, normal
   voice), **across the room** (> 3 m or off-axis, normal voice). At least 30
   utterances per stratum, over at least two sessions, with ordinary background
   noise.
3. Evaluate offline at the packaged cutoff and window only.
4. Publish only the aggregate: utterance counts and recall per stratum go into
   the manifest's `stratified_recall` block. The recordings stay on your
   machine.

The current model's manifest marks these values `not_measured`: no held-out,
distance-labelled positives exist yet.

## 6. Deployment: shadow, canary, fleet

`tools/wakeword/promotion.py` (firmware repository) records each decision from
the add-on's metadata-only weekly report:

| Stage | Criterion to advance |
|---|---|
| Offline | The packaged point passes the gate **and** improves the baseline |
| Shadow (≥ 7 days) | The candidate runs log-only next to the live model (`packages/wake-word-shadow.yaml`). Its extra detections per hour over the live model's wakes stay within 0.05/h. |
| Canary (≥ 7 days) | Live on one device. Its false-wake flags per hour stay within 0.05/h of the incumbent's 7-day baseline on that device. |
| Fleet | Every device, same trigger |

**Automatic rollback trigger:** flags per hour above the incumbent baseline +
0.05 on any device running the candidate. Rollback means restoring the model
in `models/previous/` (it keeps its own manifest), re-pinning, and flashing.

Nothing in either repository trains, promotes or flashes automatically.

## 7. Sensitivity is a separate decision

The device's "Wake word sensitivity" select moves the operating point of the
model you already have:

- **Slightly sensitive** (default) = the model's calibrated cutoff, the point
  that passed the gate.
- **Moderately** / **Very** = derived from the manifest's tier evidence: the
  lowest cutoff within one more false accept on the ambient corpus, and the
  lowest cutoff with at most 1.5× the calibrated FA/h. Both trade more false
  wakes for easier invocation, outside the validated budget.

Raising sensitivity is not a fix for a model that misses real requests: it
buys recall with false wakes. Measure recall by distance first.

## 8. The shipped model

The firmware's `models/hey_leonard.eval.json` (generation 2026-09-29) records,
on the trainer's validation sets:

| | Cutoff / window | Recall | FA/h (9.67 h ambient) |
|---|---|---|---|
| Shipped (2026-09-29) | 0.64 / 3 | 0.974885 | 0.827265 (8 events) |
| Previous (2026-07-10) | 0.71 / 3 | 0.966873 | 0.827265 (8 events) |

The shipped model passes the gate against the previous one and improves its
recall at the same false-accept rate. Its real positives come from the
maintainer's household (two speakers, one room), and household speakers are not
evaluated separately from the synthetic validation voices. Expect lower recall
for other voices and distances until you measure your own.
