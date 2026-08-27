# MaxCheapAI SN34 Miner Implementation Plan

> **Goal:** Add a production-ready MaxCheapAI image/video backend to the current
> SN34 reference miner and make one checkout reusable across multiple hotkeys.

## Task 1: Define the provider contract with failing tests

**Files**

- Create: `tests/generator/test_maxcheapai_service.py`
- Reference: `neurons/generator/task_manager.py`

**Steps**

1. Add literal fixtures matching the documented MaxCheapAI submit, status, and
   result response shapes.
2. Test that an image challenge submits Nano Banana Pro with the requested
   resolution and returns the exact downloaded bytes.
3. Test that a video challenge submits Veo 3.1, maps 480p to 720p, snaps duration
   to 4/6/8 seconds, enables audio, and returns exact bytes.
4. Test that a restored checkpoint polls without making a second submission.
5. Test provider failure, malformed success, HTTP error, and polling timeout.
6. Run the new file and confirm it fails because the provider does not exist.

Command:

```bash
pytest -q tests/generator/test_maxcheapai_service.py
```

## Task 2: Implement the MaxCheapAI service

**Files**

- Create: `neurons/generator/services/maxcheapai_service.py`

**Steps**

1. Implement availability and modality declarations.
2. Normalize image and video challenge parameters using provider-supported
   literal values.
3. Submit jobs with bearer authentication and persist their request ids through
   the existing checkpoint callback.
4. Poll terminal status with configurable interval and deadline.
5. Download final media with bounded timeouts and return the untouched bytes.
6. Clear checkpoints after terminal completion/failure.
7. Run the focused tests until green, then refactor duplicated image/video flow
   only where it improves readability.

## Task 3: Register and configure the service

**Files**

- Modify: `neurons/generator/services/service_registry.py`
- Modify: `.env.gen_miner.template`
- Modify: `docs/Generative-Mining.md`
- Modify: `README.md`
- Test: `tests/generator/test_maxcheapai_service.py`

**Steps**

1. Add `maxcheapai` to the service registry and service descriptions.
2. Add the API key, speed, poll interval, and poll timeout environment examples.
3. Document Nano Banana Pro, Veo 3.1, C2PA byte preservation, and the tested
   configuration.
4. Test that selecting `IMAGE_SERVICE=maxcheapai` and
   `VIDEO_SERVICE=maxcheapai` yields one available shared service instance.

## Task 4: Add multi-hotkey process configuration

**Files**

- Modify: `gen_miner.config.js`
- Create: `.env.gen_miner.hotkey.template`
- Modify: `docs/Generative-Mining.md`

**Steps**

1. Load the environment file named by `GEN_MINER_ENV_FILE`, defaulting to
   `.env.gen_miner`.
2. Name the PM2 process from `MINER_PM2_NAME`, defaulting to the existing name.
3. Provide a hotkey template with unique wallet, port, output, and state paths.
4. Document repeatable PM2 commands for starting and managing each instance.
5. Load the ecosystem config with Node and inspect its exported app name,
   arguments, and environment path.

## Task 5: Verify the miner

**Files**

- All changed files

**Steps**

1. Run focused provider tests.
2. Run existing generator and checkpoint-related tests.
3. Run the full test suite if dependencies and runtime permit.
4. Compile changed Python files and load the PM2 JavaScript configuration.
5. Review the final diff for secrets, accidental media rewriting, unrelated
   changes, and deployment clarity.

Commands:

```bash
pytest -q tests/generator/test_maxcheapai_service.py
pytest -q
python -m compileall neurons/generator/services
node -e "const c=require('./gen_miner.config.js'); console.log(c.apps[0])"
git diff --check
```
