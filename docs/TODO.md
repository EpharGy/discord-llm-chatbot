# TODO — Vision and Alternate Model Servers

Date: 2025-09-19

## 1) Vision (VLM) Support

Goal: Allow the bot to include image context (attachments and linked URLs) alongside text in prompts when using vision-capable models.

Proposed design:

- Config
  - `vision.enabled: false`
  - `vision.max_images: 3`
- Prompt Composition
  - Single-message: extract attachments (image/*) and URLs ending with `.png/.jpg/.jpeg/.gif/.webp`.
  - Batch: conservative first pass — include image URLs found in the last event’s text.
  - Compose multimodal content as parts: `[ {type: "text", text: "..."}, {type: "image_url", image_url: {url: "..."}}, ... ]`.
- Token Budgeting
  - Heuristic token estimate per image (e.g., 64) + normal text estimation.
  - Truncate only text parts last.
- Model Capability
  - Gate vision by config; later auto-detect model capability or map by slug.

Tasks:

- [ ] Config: add `vision` keys and getters.
- [ ] Message parsing: attachment + URL extraction helpers.
- [ ] Prompt composer: build multimodal user content (single + batch paths).
- [ ] Tokenizer: support parts list and truncation for text only.
- [ ] Testing: smoke with a VLM-capable model; verify no regressions when disabled.
- [ ] Docs: add short section in README/ROADMAP.

Acceptance Criteria:

- When `vision.enabled=true` and images present, request payloads include image_url parts.
- With `vision.enabled=false`, behavior remains unchanged.
- No errors when images are missing or in unsupported formats.

## 2) Alternate Model Servers

Goal: Support different backends beyond OpenRouter, including self-hosted or local engines.

Candidates:

- Chutes (gateway)
- NanoGPT-based API shims
- Local engines (kobold.cpp, llama.cpp, text-generation-webui)

Proposed design:

- Abstraction
  - Keep `LLMClient` interface and introduce pluggable implementations.
  - Optional `provider_order` to attempt multiple providers in sequence.
- Config (example)

  ```yaml
  model:
    provider_order: ["openrouter", "local", "auto"]
    openrouter:
      name: meta-llama/llama-4-scout:free
      fallbacks: "meta-llama/llama-3.3-8b-instruct:free"
      allow_auto_fallback: true
    local:
      enabled: false
      engine: koboldcpp   # koboldcpp | llama.cpp | textgen-webui
      base_url: http://127.0.0.1:5001
      timeout_seconds: 8
      max_tokens: 300
      temperature: 0.7
      top_p: 0.9
  ```

- Behavior
  - Try providers in order; clear structured logs for each attempt.
  - Graceful offline message if all fail.

Tasks:

- [ ] Extend config schema for multi-provider support.
- [ ] Implement `LocalKoboldClient` (HTTP) with chat endpoint mapping.
- [ ] Add provider orchestrator in `MessageRouter` or a small `ModelRouter` helper.
- [ ] Logging: surface provider attempts and outcomes.
- [ ] Docs: usage notes, examples, and limitations.

Acceptance Criteria:

- Bot can run with only OpenRouter (current path) or with local provider when enabled.
- Clear logs indicate which provider handled the request or why it failed.
- Easy switch via config without code changes for common cases.

## 3) Observability Add-ons (Optional)

- [ ] Runtime toggle to set log level (e.g., command or env hot-reload).
- [ ] Capture token usage per reply in logs and/or metrics.
- [ ] Batch dedupe improvements: track seen message IDs across batch intervals.

## Notes

- Be careful with prompt logging: may contain sensitive content. Keep `LOG_PROMPTS` default off.
- Vision adds request size; ensure token budgeting remains conservative.
