# MaxCheapAI SN34 Miner Design

## Goal

Extend the current BitMind SN34 generative miner with a MaxCheapAI backend that:

- uses `nano-banana-pro` for image challenges;
- uses `veo-3.1` with audio for video challenges;
- preserves the exact provider bytes and their C2PA manifest;
- survives miner restarts while a provider job is pending;
- can run as multiple independently registered hotkeys from one checkout.

## Provider integration

Add `MaxCheapAIService` beside the existing OpenRouter and Runway services and
register it under the service name `maxcheapai`.

The service uses `MAXCHEAPAI_API_KEY` as a bearer token and the documented REST
API at `https://maxcheapai.com/api`:

- image submit: `POST /generate/image`;
- image status: `GET /generations/{request_id}`;
- video submit: `POST /generate/video`;
- video status: `GET /video-generations/{request_id}`.

Both modalities use the existing miner checkpoint callback immediately after a
successful submission. The checkpoint records the MaxCheapAI request id,
modality, and model. A restored processing task resumes polling the existing
request instead of paying for and submitting another generation.

On success, the service downloads the result URL and returns the response bytes
without decoding, resizing, transcoding, or re-saving them. This is required to
preserve the provider's C2PA hard binding.

## Challenge parameter mapping

Image challenges map directly to Nano Banana Pro:

| SN34 parameter | MaxCheapAI value |
|---|---|
| model | `nano-banana-pro` |
| resolution `1K`, `2K`, `4K` | same value |
| aspect ratio | supported ratio, default `1:1` |
| image count | `1` |
| automatic prompt enhancement | disabled |

Video challenges map to Veo 3.1:

| SN34 parameter | MaxCheapAI value |
|---|---|
| model | `veo-3.1` |
| resolution `480p` | `720p` (lowest supported Veo tier) |
| resolution `720p`, `1080p` | same value |
| duration | nearest of `4`, `6`, or `8` seconds |
| aspect ratio | `16:9` or `9:16`, default `16:9` |
| audio | enabled |

The provider speed defaults to `slow` and can be changed with
`MAXCHEAPAI_SPEED`. Poll interval and timeout are configurable, with conservative
defaults suitable for queued video generation.

## Failure handling

The service fails the task with a useful error when:

- submission or status requests return non-success HTTP responses;
- a response is malformed or lacks a request/result URL;
- the provider reports `failed`;
- the polling deadline expires;
- the final media download fails or is empty.

The external-job checkpoint is cleared after a terminal success or failure. A
polling timeout is terminal for the local task but does not submit a duplicate
provider job.

## Multi-hotkey deployment

`gen_miner.config.js` accepts:

- `GEN_MINER_ENV_FILE`: environment file for one instance;
- `MINER_PM2_NAME`: unique PM2 process name.

Each hotkey environment file must use a unique `BT_WALLET_HOTKEY`,
`BT_AXON_PORT`, `MINER_OUTPUT_DIR`, and `MINER_STATE_DIR`. Instances may share
the same coldkey, checkout, VPS, and MaxCheapAI API key.

Fleet orchestration and automatic on-chain registration are intentionally out
of scope. PM2 already supplies process lifecycle management, and registration
burn should be monitored and performed deliberately.

## Verification

Unit tests cover request payloads, resolution/duration normalization, provider
terminal states, restart checkpoints, and exact-byte downloads. The existing
SN34 C2PA verifier remains the final compatibility check for a real generated
sample; live generation is not part of the automated test suite because it is
external, slow, and account-dependent.
