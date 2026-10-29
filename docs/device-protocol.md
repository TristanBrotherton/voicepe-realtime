# Device protocol: utterances

How the add-on and a Voice PE agree on what the speaker plays. This is the
`utt1` extension of the device WebSocket protocol; older firmware and older
add-ons keep working without it (see "Compatibility" below).

Everything the device plays is an **utterance**: one contiguous piece of
24 kHz mono 16-bit PCM with a number, a kind and a turn. The add-on has one
**speaker owner** per connected device, and it is the only thing that writes
audio to that device. It sends each utterance whole, one after another, and
the device reports what it actually played.

The rule both sides keep: **once an utterance has started, it plays to its
end.** Only the user (stop word, a new wake, the button, mute) or a lost
connection stops it. The add-on never cuts a started utterance.

## Turning it on

Per device, with the `speaker_owner` option in the `devices` list (default
off). With it off, nothing on the wire changes.

With it on, the add-on uses utterance framing only if the device offers it:

- the device's `start` message lists `"utt1"` in `caps`: utterance framing;
- otherwise: **legacy framing**. The same queue and order, plain audio as
  before, no `utt_*` messages, and "played" is estimated from the clock.

There is no other handshake. The add-on's `hello` and the device's `start`
cross on the wire; each side turns the feature on only when it sees the
other's message.

## Device to add-on

`start` (once per connection) may carry:

| field | meaning |
|---|---|
| `caps` | list of capabilities, for example `["ireason1", "utt1"]` |
| `fw` | firmware version string |
| `last_disconnect` | why the previous connection ended: `why` (`ws_error`, `ws_closed`, `wifi`, `tx_timeout`, `reboot`), `esp_err`, `errno`, `reset`, `up_s`, `ago_s` |
| `prev_utt` | the previous connection's last utterance if it never finished: `id`, `fed`, `bytes`, `st` |

`interrupt` may carry:

| field | meaning |
|---|---|
| `reason` | `stop`, `wake`, `button`, `mute`, `no_speech` or `residual` |
| `seq` | increasing per connection |
| `utt`, `fed` | (utterance framing) the utterance that was playing and the bytes of it the speaker had accepted |

`stop` and `button` cancel the request. `wake` is a barge-in: what played is
cut, the request is not cancelled. `mute`, `no_speech` and `residual` are
housekeeping. Every interrupt flushes the speaker.

`utt_ack` reports one utterance:

| field | meaning |
|---|---|
| `id` | the utterance |
| `st` | `playing` (its first real byte reached the speaker), `progress` (about once a second), `done` (all of it played), `cut` (stopped by a user interrupt), `dropped` (flushed before it started) |
| `fed` | bytes the speaker chain has accepted |
| `wait_ms` | on `playing`: from its first byte arriving to its first byte playing (the start prime plus any cushion) |
| `und` | on `done`/`cut`: how often the speaker ran dry inside it |

## Add-on to device

| message | fields | notes |
|---|---|---|
| `hello` | adds `chain_prime_ms` (0 to 600) and `utt_cushion_ms` (0 to 1000) | only when `speaker_owner` is on; older firmware ignores unknown keys |
| `utt_start` | `id`, `kind`, `turn` | before the utterance's first byte |
| binary | PCM, at most 4800 bytes per frame | unchanged |
| `utt_end` | `id`, `bytes` | after its last byte; a short utterance can start without waiting for a cushion |
| `interrupt_ack` | `seq` | the add-on has flushed. The device discards every `utt_*` and binary frame between its interrupt and this, then accepts new utterances |

Kinds: `clip` (a canned acknowledgement or spoken error), `ack` (the voice's
own acknowledgement), `reply`, `result`, `prompt` (a confirmation question),
`announce` (timers, announcements, background results), `coach` (voice
training lines).

No message in this protocol carries a `"value"` key: the firmware finds phase
messages by matching `"value":"<phase>"` in any text frame.

All add-on frames for one device go through one sender, in order. Phase
messages keep their own path; neither side may depend on the order of a phase
relative to `utt_*` messages.

## Order and pacing (add-on)

- A clip or acknowledgement is dropped if it cannot start soon after it was
  asked for, or if its turn already had one, or if the answer is already
  queued. Only one acknowledgement is heard per turn.
- A timer or announcement plays at the next boundary, ahead of later model
  speech, and never inside an utterance that is playing.
- Model speech is sent as soon as it exists (no hold, no real-time pacer).
- No audio is sent more than 2 s ahead of what the device has played, so a
  control message never waits behind a long burst.

## Start rule (device, utterance framing)

- If the speaker chain is cold (stopped, or nothing played for over 600 ms),
  play `chain_prime_ms` of silence first (default 200).
- Then start the utterance when `utt_cushion_ms` of it is buffered (default
  0), or its `utt_end` has arrived, or 300 ms after its first byte.
- An utterance that follows another one while the chain is still playing
  starts at once: no prime, no cushion.

## After a lost connection

The device keeps playing what it holds. On reconnect its `start` says why the
link dropped and which utterance had not finished. The add-on marks
everything it had in flight as lost and delivers again, once and within 30 s,
only what must not be lost: a verified result (through its task list) and an
announcement. Acknowledgements, chat replies and confirmation questions are
never repeated.

## Compatibility

| | older add-on | newer add-on, option off | newer add-on, option on |
|---|---|---|---|
| older firmware | as before | as before | owner with legacy framing |
| newer firmware | as before (extra fields ignored) | as before, interrupt reasons logged | owner with utterance framing |

Newer firmware keeps its previous behaviour exactly whenever no `utt_start`
arrives.
