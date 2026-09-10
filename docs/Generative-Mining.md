# Generative Mining Guide

## Before You Proceed

Follow the [Installation Guide](Installation.md) to set up your environment before proceeding with generative miner setup.

## Generative Mining Overview

Generative miners create synthetic media (images and videos) according to prompts from validators. Miners are rewarded based on:
1. **Data validation pass rate** -- content must pass C2PA signature checks and prompt alignment validation
2. **Adversarial performance** -- synthetic media that fools discriminative miners earns a multiplier bonus
3. **Sample volume** -- processing more evaluations provides a logarithmic bonus up to 2x
4. **Model choice** -- more expensive generation models earn higher per-unit rewards (see [Model Pricing](#model-pricing-and-rewards) below)

See [Incentive Mechanism](Incentive.md) for the full reward formula.

Generative miners operate as FastAPI servers that receive generation requests from validators and respond asynchronously via webhooks.

To avoid gaming, validation currently requires that all generated media have valid C2PA metadata signed by trusted services. See [gas/verification/c2pa_verification.py](../gas/verification/c2pa_verification.py) for the list of trusted signers. 

To check a file locally (uses same logic as validator): `gascli generator verify-c2pa <file> --verbose`.

## Configuration Setup

### Environment Configuration

Create a `.env.gen_miner` file in the project root: copy from [.env.gen_miner.template](../.env.gen_miner.template) and fill in your wallet, API keys, and options.

### Network Configuration

**Mainnet (SN34)**:
```bash
BT_CHAIN_ENDPOINT=wss://entrypoint-finney.opentensor.ai:443
BT_NETUID=34
```

## Generation Services

You must configure at least one generation method for your miner to be functional.
**All generated content must have verifiable C2PA metadata** — validators reject unsigned
content, and the miner will block models that don't produce valid C2PA to avoid wasted
API costs.  See [gas/verification/c2pa_verification.py](../gas/verification/c2pa_verification.py)
for the full list of trusted signers.

If you have a C2PA-compliant generation service you'd like supported, open an issue or
PR — we're happy to add new providers and trust anchors.

### OpenAI Service
- **API Key**: `OPENAI_API_KEY`
- **Modalities**: Image, Video
- **Image models**: DALL-E 3 (C2PA-signed)
- **Video models**: Sora 2 (`sora-2`, `sora-2-pro`) — C2PA-signed via Truepic WebClaimSigningCA.
  Deprecated by OpenAI; shutdown scheduled 2026-09-24.
- **Website**: [platform.openai.com](https://platform.openai.com)

### OpenRouter Service
- **API Key**: `OPEN_ROUTER_API_KEY`
- **Modalities**: Image, Video
- **Image models**: Any OpenRouter model with image output capability. Default:
  `google/gemini-3-pro-image-preview`. Also works with `openai/gpt-5-image`,
  `google/gemini-2.5-flash-image`, etc. — pass the model ID via request parameters.
- **Video models** (only C2PA-capable models are enabled — others are blocked at the
  service level to prevent miners from burning API credits on content that validators
  will reject):
  - **Google Veo**: `google/veo-3.1`, `google/veo-3.1-fast`, `google/veo-3.1-lite`
    (trust anchor: Google C2PA Root CA G3)
  - **ByteDance Seedance**: `bytedance/seedance-2.0`, `bytedance/seedance-2.0-fast`,
    `bytedance/seedance-1-5-pro` (trust anchor: GlobalSign Secure Mail Root R45)
  - *Excluded*: Kling (`kwaivgi/kling-*`) — no C2PA manifest; Wan (`alibaba/wan-*`) —
    no C2PA manifest; Hailuo (`minimax/hailuo-*`) — no C2PA manifest;
    Sora (`openai/sora-2-pro` via OpenRouter) — self-signed end-entity cert (CA=false),
    cannot be fixed with trust anchors
- **Website**: [OpenRouter.ai](https://openrouter.ai)
- **Video API**: Uses OpenRouter's dedicated `/api/v1/videos` endpoints (separate from chat completions)

### Stability AI Service
- **API Key**: `STABILITY_API_KEY`
- **Modalities**: Image
- **Models**: Stable Diffusion 3.5, Stable Image Ultra, Stable Image Core (C2PA-signed
  via GlobalSign GCC R6 SMIME chain)
- **Website**: [stability.ai](https://stability.ai)

### Runway Service
- **API Key**: `RUNWAYML_API_KEY` (preferred) or `RUNWAYML_API_SECRET`
- **Modalities**: Video
- **Models**: `gen4.5`, `veo3.1`, `veo3.1_fast`, `veo3` — C2PA-signed via GlobalSign
  GCC R6 SMIME chain (pre-Gen4) or private Stability AI root (Gen-4.5)
- **Website**: [runwayml.com](https://runwayml.com)

### MaxCheapAI Service
- **API Key**: `MAXCHEAPAI_API_KEY`
- **Modalities**: Image, Video
- **Image model**: Google Nano Banana Pro (`nano-banana-pro`), honoring the
  requested 1K/2K/4K tier
- **Video model**: Google Veo 3.1 (`veo-3.1`) with audio; 480p requests use the
  provider's lowest supported 720p tier
- **C2PA**: Provider media is downloaded and returned byte-for-byte without
  resizing or transcoding, preserving Google's signed manifest
- **Timeout and retries**: Each generation may poll for up to 1,800 seconds.
  Provider-reported failures are resubmitted up to three times; transient
  polling and download failures retry the existing request.
- **Website**: [maxcheapai.com](https://maxcheapai.com/en/docs)

### CKey Service
- **API Key**: `CKEY_API_KEY`
- **Modalities**: Image
- **Models**: `wowztools/nano-banana-pro`, falling back to
  `wowztools/Nano-Banana-2` after the initial request and three retries fail
- **Resolution mapping**: SN34's 1K/2K/4K tiers request square
  1024x1024/2048x2048/4096x4096 output
- **C2PA**: The downloaded Google CDN bytes are returned without resizing or
  transcoding. Live samples from both models pass SN34's trusted-issuer verifier.
- **Retries**: Each model receives one initial attempt plus three retries. HTTP,
  response parsing, missing URL, and media download failures all consume an attempt.
- **Endpoint**: [api.xah.io](https://api.xah.io/v1/images/generations)

### Vertex AI Service
- **Authentication**: Google Application Default Credentials (ADC); no Gemini API
  key is used
- **Modality**: Image
- **Model**: Nano Banana Pro (`gemini-3-pro-image`)
- **Resolution mapping**: SN34's 1K/2K/4K tiers map directly to Vertex AI's
  1K/2K/4K image size, with square JPEG output
- **C2PA**: Vertex AI's original JPEG bytes are returned without resizing or
  transcoding. A live generated sample passed SN34's trusted Google issuer,
  signature, and AI-generated assertions.
- **Retries**: One initial request plus three retries; configure the delay with
  `VERTEXAI_RETRY_DELAY` (default: 10 seconds)
- **Timeout**: Each request is bounded by `VERTEXAI_REQUEST_TIMEOUT` (default:
  600 seconds), so a stalled provider call advances through the retry budget
- **Location**: `VERTEXAI_LOCATION=global` by default, independently of
  `GOOGLE_CLOUD_LOCATION`

### VertexGen Service
- **API Key**: `VERTEXGEN_API_KEY`
- **Modality**: Image
- **Model**: Google Nano Banana Pro (`nano-banana-pro`)
- **Resolution and aspect ratio**: SN34's requested 1K/2K/4K tier and supported
  aspect ratio are forwarded directly
- **C2PA**: The authenticated content response is returned byte-for-byte without
  resizing or transcoding. A live 2K PNG validated as Trusted against the official
  C2PA trust list, signed by Google Media Processing Services, with Google
  Generative AI and SynthID assertions.
- **Restart safety**: The provider job ID and retry number are checkpointed. A
  restarted miner resumes the existing job instead of submitting a duplicate.
- **Retries**: Definitive provider failures are retried with a fresh idempotency
  key. Ambiguous submission, polling, and download failures retry the same key or
  job to avoid duplicate charges. Both use `VERTEXGEN_MAX_RETRIES` (default: 3).
- **Endpoint**: `http://107.178.109.250:8686/v1` by default; this is plain HTTP.
  Set `VERTEXGEN_API_BASE_URL` to an HTTPS endpoint when available so bearer keys
  are encrypted in transit.

### GPTi2 Service
- **API Key**: `GPTI2_API_KEY`
- **Modality**: Image
- **Model**: OpenAI GPT Image 2 (`gpt-image-2`)
- **Resolution mapping**: SN34's requested 1K/2K/4K tier and aspect ratio map to
  GPTi2's corresponding supported pixel dimensions. Invalid values fall back to
  square 1K.
- **Quality**: `low`, `medium`, and `high` are supported; omitted or invalid values
  use the provider's lowest-cost `low` tier. High-quality requests use durable
  asynchronous jobs because GPTi2 requires that route.
- **C2PA**: Base64 image bytes and asynchronous job downloads are returned without
  resizing or transcoding, preserving the provider's signed manifest.
- **Restart safety**: High-quality job IDs are checkpointed so a restarted miner
  resumes polling instead of submitting and paying for another image.
- **Endpoint**: [gpti2.store](https://gpti2.store/v1)

### Service Selection

Configure which service handles each modality in your `.env.gen_miner` file:

```bash
IMAGE_SERVICE=gpti2         # openai, openrouter, stabilityai, maxcheapai, ckey, vertexai, vertexgen, gpti2, or none
VIDEO_SERVICE=maxcheapai    # openai, openrouter, runway, maxcheapai, or none
GPTI2_API_KEY=sk-your-key
MAXCHEAPAI_API_KEY=mcai_your_key
```

All configured services must produce **C2PA-signed content**. Setting a modality to `none` disables it (requests will be rejected).


## Model Pricing and Rewards

The validator weights each submitted media sample by the **cost of the model that produced it**.
More expensive models earn proportionally higher per-sample rewards.  This incentivizes miners
to invest in quality generation infrastructure instead of racing to the bottom with the cheapest
available model.

### How It Works

Each model has a reference price in USD per second of 720p video (or per image, normalized to a
video-equivalent).  Your reward multiplier is computed from your model mix:

```
multiplier = sqrt(model_price / baseline_price)
```

- **baseline_price** = cheapest C2PA-capable model (currently `google/veo-3.1-lite` at $0.03/s)
- **sqrt scaling** prevents extreme gaps — a model 4× as expensive earns 2×, not 4×
- **Model mix** — if you run multiple models, the weighted average multiplier across your verified
  samples is used

The multiplier is applied to your base reward (pass rate × volume) before modality weighting.

### Current Model Prices

All prices are USD per second of 720p video with audio.  These live in
[`gas/evaluation/rewards.py`](../gas/evaluation/rewards.py) and are occasionally refreshed from
OpenRouter's free [`/api/v1/videos/models`](https://openrouter.ai/api/v1/videos/models) endpoint.

| Model | Price (USD/s) | Multiplier vs baseline | C2PA Status |
|-------|---------------|------------------------|-------------|
| `google/veo-3.1-lite` | $0.03 | 1.00× | ✓ Google C2PA Root CA G3 |
| `dreamina-seedance-2-0-fast` | $0.12 | 2.00× | ✓ Byteplus Pte. Ltd. |
| `google/veo-3.1-fast` | $0.08 | 1.63× | ✓ Google C2PA Root CA G3 |
| `dreamina-seedance-2-0` | $0.15 | 2.24× | ✓ Byteplus Pte. Ltd. |
| `google/veo-3.1` | $0.20 | 2.58× | ✓ Google C2PA Root CA G3 |

Seedance 1.5 Pro and Runway gen4.5 are currently excluded — 1.5 Pro produces no C2PA;
gen4.5 has a `claimSignature.mismatch` bug in Runway's signing infrastructure.
Both will be re-added when their C2PA issues are resolved.

### Practical Advice

- **Veo 3.1 Lite** is the cheapest model with C2PA.  It earns the baseline multiplier (1×) and
  produces fully verifiable content.  Good for miners optimizing for throughput.
- **Seedance 2.0 Fast** costs 4× more than baseline but earns 2× multiplier — a strong
  quality/efficiency tradeoff.
- **Veo 3.1** (full) costs ~7× more than baseline and earns 2.58× multiplier.  Useful if your
  content quality advantage translates to a higher pass rate or fool rate.
- Running multiple models is supported — your effective multiplier is averaged across all
  verified samples, weighted by how many each model contributed.

## Starting the Miner

### Using gascli (Recommended)

First, activate the virtual environment:
```bash
source .venv/bin/activate
```

Start the generative miner:
```bash
# Start the miner
gascli generator start

# Using aliases
gascli gen start
gascli g start
```

You can also start the miner using PM2 directly:
```bash
pm2 start gen_miner.config.js
```

### Running Multiple Hotkeys

Use one environment file and one PM2 process per registered hotkey. Start from
the included template:

```bash
cp .env.gen_miner.hotkey.template .env.hotkey1
cp .env.gen_miner.hotkey.template .env.hotkey2
```

Set a unique `MINER_PM2_NAME`, `BT_WALLET_HOTKEY`, `BT_AXON_PORT`,
`MINER_OUTPUT_DIR`, and `MINER_STATE_DIR` in each file, then start them:

```bash
GEN_MINER_ENV_FILE=.env.hotkey1 pm2 start gen_miner.config.js
GEN_MINER_ENV_FILE=.env.hotkey2 pm2 start gen_miner.config.js
pm2 save
```

The instances can share `BT_WALLET_NAME`, `MAXCHEAPAI_API_KEY`, the checkout,
and the VPS. Never share an axon port or state directory between instances.

## Miner Management

### Status and Monitoring

```bash
# Check miner status
gascli generator status

# View miner logs  
gascli generator logs

# Follow logs in real-time
gascli generator logs --follow

# Show miner configuration and API key status
gascli generator info
```

### Starting and Stopping

```bash
# Start the miner
gascli generator start

# Stop the miner
gascli generator stop

# Restart the miner
gascli generator restart

# Delete the miner process
gascli generator delete
```

### API Endpoints

Your miner exposes these endpoints for validators:

- `POST /gen_image` - Image generation requests
- `POST /gen_video` - Video generation requests
- `GET /health` - Health check endpoint
- `GET /miner_info` - Miner information and capabilities
- `GET /status/{task_id}` - Task status queries

## Configuration Parameters

### Core Settings

- `BT_WALLET_NAME`: Your Bittensor wallet name
- `BT_WALLET_HOTKEY`: Your wallet hotkey name  
- `BT_NETUID`: Subnet ID (34 for mainnet, 379 for testnet)
- `BT_AXON_PORT`: Port for your miner's API server

### Performance Settings

- `MINER_MAX_CONCURRENT_TASKS`: Maximum parallel generation tasks (default: 5)
- `MINER_TASK_TIMEOUT`: Legacy compatibility timeout (default: 1800 seconds)
- `MAXCHEAPAI_POLL_TIMEOUT`: MaxCheapAI generation deadline (default: 1800 seconds)
- `MAXCHEAPAI_MAX_RETRIES`: Retries after a provider-reported failure (default: 3)
- `CKEY_REQUEST_TIMEOUT`: CKey synchronous generation timeout (default: 1800 seconds)
- `CKEY_DOWNLOAD_TIMEOUT`: CKey media download timeout (default: 600 seconds)
- `CKEY_RETRY_DELAY`: Delay between CKey attempts (default: 10 seconds)
- `VERTEXAI_PROJECT`: Vertex AI Google Cloud project; falls back to
  `GOOGLE_CLOUD_PROJECT`, then the project discovered from ADC
- `VERTEXAI_LOCATION`: Vertex endpoint location (default: `global`)
- `VERTEXAI_REQUEST_TIMEOUT`: Per-attempt Vertex request timeout in seconds
  (default: 600)
- `VERTEXAI_RETRY_DELAY`: Delay between Vertex AI attempts (default: 10 seconds)
- `VERTEXGEN_WAIT_SECONDS`: Seconds the submission call waits before returning an
  asynchronous job (default: 20; maximum: 30)
- `VERTEXGEN_REQUEST_TIMEOUT`: Submission and status request timeout (default: 60 seconds)
- `VERTEXGEN_POLL_INTERVAL`: Delay between job status requests (default: 2 seconds)
- `VERTEXGEN_POLL_TIMEOUT`: Overall job polling deadline (default: 600 seconds)
- `VERTEXGEN_DOWNLOAD_TIMEOUT`: Image download timeout (default: 600 seconds)
- `VERTEXGEN_MAX_RETRIES`: Same-request retries after transient failures and fresh
  submissions after definitive provider failures (default: 3)
- `VERTEXGEN_RETRY_DELAY`: Delay between VertexGen request retries (default: 10 seconds)
- `MINER_OUTPUT_DIR`: Directory for generated content and logs
- `MINER_DEVICE`: Computing device (`auto`, `cuda`, `cpu`) [Deprecated: local modals currently not supported]


```bash
# General help
gascli generator --help

# Command-specific help  
gascli generator start --help
gascli generator logs --help

# Check service status
gascli generator info
```

**Note**: Remember to activate the virtual environment with `source .venv/bin/activate` before running any `gascli` commands. 
