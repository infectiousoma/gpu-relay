"""Multi-model pipeline orchestrator.

Pipelines (controlled by X-Pipeline header or user default):
  infer              — send directly to remote GPU model (default)
  preprocess,infer   — local Ollama 7B converts prompt to structured JSON,
                       then remote model performs main inference
  preprocess,infer,postprocess — adds a cheap local critic/formatter pass

Workflow models (selected via the model field):
  llm-smart          — local optimize → auto-tier GPU inference
  llm-code-review    — categorize → GPU review → local summary
  llm-refactor       — local analyze → GPU plan → local validate → GPU implement
  llm-arch-design    — local parse → GPU design → local checklist → GPU details

Preprocessor JSON shape:
  intent, files_referenced, complexity_signals, target_tier_hint, structured_prompt

If preprocessor fails, pipeline falls back to plain infer.
"""

from __future__ import annotations

import json

import httpx
import structlog

from bridge.schemas import ChatCompletionRequest, ChatMessage
from bridge.settings import settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Workflow model registry — exposed as OpenAI-compatible model IDs
# ---------------------------------------------------------------------------

WORKFLOW_MODELS: dict[str, dict] = {
    "llm-smart": {
        "id": "llm-smart",
        "name": "Smart Coding (Auto-optimized)",
        "gpu_tier": None,  # determined by preprocessor's suggested_tier
        "workflow": "optimize_and_execute",
        "description": "Local preprocessor optimizes prompt and selects tier automatically",
    },
    "llm-code-review": {
        "id": "llm-code-review",
        "name": "Code Review Workflow",
        "gpu_tier": "architecture",
        "workflow": "code_review",
        "description": "Categorize → GPU review → local executive summary",
    },
    "llm-refactor": {
        "id": "llm-refactor",
        "name": "Dual-Model Refactoring",
        "gpu_tier": "architecture",
        "workflow": "refactoring",
        "description": "Local analyze → GPU plan → local validate → GPU implement",
    },
    "llm-arch-design": {
        "id": "llm-arch-design",
        "name": "Architecture Design",
        "gpu_tier": "maximum",
        "workflow": "architecture_design",
        "description": "Local parse → GPU design → local checklist → GPU implementation details",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences that some models wrap JSON in."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else len(text)
        text = text[first_nl:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


# ---------------------------------------------------------------------------
# PromptPreprocessor
# ---------------------------------------------------------------------------

class PromptPreprocessor:
    """Local Ollama model that optimizes prompts before remote inference."""

    _OPTIMIZE_SYSTEM = """\
You are a prompt optimizer for coding AI systems.

Analyze the user's request and output ONLY valid JSON with these fields:
  task_type: one of "bug_fix", "refactor", "design", "review", "explain", "implement", "other"
  language: programming language detected or "unknown"
  complexity: one of "simple", "moderate", "complex"
  optimized_prompt: the user's request rewritten as a clear, structured prompt for a coding model
  suggested_tier: one of "simple", "architecture", "maximum", "ultra"
  context_hints: object with inferred details (e.g. {"framework": "fastapi", "paradigm": "async"})

Rules:
- Output ONLY the JSON object, no markdown fences, no extra text.
- suggested_tier:
    "simple"       → single-file bugs/fixes/explanations
    "architecture" → multi-file refactors, API design, component design
    "maximum"      → security audits, large codebases, system design
    "ultra"        → mission-critical full-system analysis"""

    _INTENT_SYSTEM = """\
You are a conversation analyzer for a coding AI system.

Analyze this conversation and output ONLY valid JSON with:
  primary_intent: one-sentence summary of what the user ultimately wants
  complexity_trend: "increasing" | "stable" | "decreasing"
  suggested_tier: one of "simple", "architecture", "maximum", "ultra"
  pivot_detected: true if the user changed direction significantly, false otherwise
  summary: 2-3 sentence summary of the conversation arc

Output ONLY the JSON object, no markdown fences."""

    async def optimize_prompt(
        self,
        user_prompt: str,
        files: list[dict] | None = None,
        context: dict | None = None,
    ) -> dict:
        """Convert a casual user prompt to a structured coding prompt.

        Returns dict with: task_type, language, complexity, optimized_prompt,
        suggested_tier, context_hints.
        """
        files_note = f"\n\nFiles provided: {[f.get('name', '?') for f in files]}" if files else ""
        context_note = f"\n\nAdditional context: {context}" if context else ""
        prompt = f"User request: {user_prompt}{files_note}{context_note}\n\nOptimize:"
        raw = await self._call_ollama(self._OPTIMIZE_SYSTEM, prompt, max_tokens=512)
        return json.loads(_strip_fences(raw))

    async def extract_intent(self, conversation_history: list[dict]) -> dict:
        """Analyze conversation history to extract evolving intent.

        Useful for mid-conversation tier upgrades.
        Returns dict with: primary_intent, complexity_trend, suggested_tier,
        pivot_detected, summary.
        """
        formatted = "\n".join(
            f"{m.get('role', 'user').upper()}: {(m.get('content') or '')[:500]}"
            for m in conversation_history[-10:]
        )
        raw = await self._call_ollama(
            self._INTENT_SYSTEM,
            f"Conversation:\n{formatted}\n\nAnalyze:",
            max_tokens=512,
        )
        return json.loads(_strip_fences(raw))

    async def _call_ollama(self, system: str, prompt: str, max_tokens: int = 512) -> str:
        payload = {
            "model": settings.ollama_preprocessor_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{settings.ollama_local_url}/api/chat", json=payload)
            r.raise_for_status()
        return r.json().get("message", {}).get("content", "{}")


# ---------------------------------------------------------------------------
# WorkflowOrchestrator
# ---------------------------------------------------------------------------

class WorkflowOrchestrator:
    """Chains local Ollama and remote GPU calls for multi-step workflows."""

    def __init__(self) -> None:
        self._preprocessor = PromptPreprocessor()

    async def execute(
        self,
        workflow: str,
        request: ChatCompletionRequest,
        pod_endpoint: str,
    ) -> tuple[str, dict]:
        """Dispatch to a named workflow.

        Returns (response_text, meta) where meta contains:
          workflow, stages, errors, and any workflow-specific data.
        """
        meta: dict = {"workflow": workflow, "stages": [], "errors": []}
        user_content = next(
            (m.text_content() for m in reversed(request.messages) if m.role == "user"), ""
        )

        handlers = {
            "optimize_and_execute": self._optimize_and_execute,
            "code_review": self._code_review,
            "refactoring": self._refactoring,
            "architecture_design": self._architecture_design,
        }
        handler = handlers.get(workflow)
        if handler is None:
            raise ValueError(f"Unknown workflow: {workflow!r}")

        return await handler(user_content, request, pod_endpoint, meta)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _gpu_call(
        self, pod_endpoint: str, messages: list[dict], max_tokens: int = 2048
    ) -> str:
        payload = {
            "model": "assistant",
            "messages": messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{pod_endpoint}/v1/chat/completions", json=payload)
            r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _local_call(self, system: str, user: str, max_tokens: int = 512) -> str:
        payload = {
            "model": settings.ollama_preprocessor_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{settings.ollama_local_url}/api/chat", json=payload)
            r.raise_for_status()
        return r.json().get("message", {}).get("content", "")

    def _prior_messages(self, request: ChatCompletionRequest) -> list[dict]:
        return [{"role": m.role, "content": m.text_content()} for m in request.messages[:-1]]

    # ------------------------------------------------------------------
    # Workflow: optimize_and_execute (llm-smart)
    # ------------------------------------------------------------------

    async def _optimize_and_execute(
        self, user_content: str, request: ChatCompletionRequest, pod_endpoint: str, meta: dict
    ) -> tuple[str, dict]:
        meta["stages"].append("local:optimize")
        try:
            optimized = await self._preprocessor.optimize_prompt(user_content)
            structured_prompt = optimized.get("structured_prompt") or user_content
            meta["preprocessor_output"] = optimized
            log.info(
                "smart_optimize_done",
                task_type=optimized.get("task_type"),
                suggested_tier=optimized.get("suggested_tier"),
            )
        except Exception as exc:
            log.warning("smart_optimize_failed", error=str(exc))
            meta["errors"].append(f"optimize: {exc}")
            structured_prompt = user_content

        meta["stages"].append("gpu:execute")
        messages = [
            *self._prior_messages(request),
            {"role": "user", "content": structured_prompt},
        ]
        result = await self._gpu_call(pod_endpoint, messages, max_tokens=4096)
        return result, meta

    # ------------------------------------------------------------------
    # Workflow: code_review (llm-code-review)
    # ------------------------------------------------------------------

    async def _code_review(
        self, user_content: str, request: ChatCompletionRequest, pod_endpoint: str, meta: dict
    ) -> tuple[str, dict]:
        # Stage 1: Local — categorize scope and focus areas
        meta["stages"].append("local:categorize")
        cat: dict = {"scope": "single_file", "focus_areas": ["correctness", "style"], "estimated_complexity": "moderate"}
        try:
            raw = await self._local_call(
                system="""\
Analyze this code review request. Output ONLY JSON with:
  scope: "single_file" | "multi_file" | "architecture"
  focus_areas: list of review areas e.g. ["security","performance","readability","correctness"]
  estimated_complexity: "simple" | "moderate" | "complex"
Output ONLY the JSON object.""",
                user=user_content,
                max_tokens=256,
            )
            cat = json.loads(_strip_fences(raw))
            meta["categorization"] = cat
        except Exception as exc:
            log.warning("code_review_categorize_failed", error=str(exc))
            meta["errors"].append(f"categorize: {exc}")

        # Stage 2: Remote — perform the review
        meta["stages"].append("gpu:review")
        focus = ", ".join(cat.get("focus_areas") or ["correctness", "style", "performance"])
        review_prompt = f"""\
Perform a thorough code review. Focus on: {focus}

For each issue found, structure as:
## [Category]: [Issue Title]
**Severity**: Critical | High | Medium | Low
**Location**: [file/function if identifiable]
**Problem**: [clear description]
**Recommendation**: [concrete fix with code where applicable]

Original request:
{user_content}"""

        messages = [
            *self._prior_messages(request),
            {"role": "user", "content": review_prompt},
        ]
        review_result = await self._gpu_call(pod_endpoint, messages, max_tokens=4096)

        # Stage 3: Local — executive summary
        meta["stages"].append("local:summarize")
        summary_prefix = ""
        try:
            summary = await self._local_call(
                system="Produce a brief executive summary (3-5 bullet points) of the code review findings below. Be concise.",
                user=review_result,
                max_tokens=256,
            )
            summary_prefix = f"## Executive Summary\n\n{summary.strip()}\n\n---\n\n"
        except Exception as exc:
            log.warning("code_review_summarize_failed", error=str(exc))
            meta["errors"].append(f"summarize: {exc}")

        final = f"{summary_prefix}## Detailed Findings\n\n{review_result}"
        return final, meta

    # ------------------------------------------------------------------
    # Workflow: refactoring (llm-refactor)
    # ------------------------------------------------------------------

    async def _refactoring(
        self, user_content: str, request: ChatCompletionRequest, pod_endpoint: str, meta: dict
    ) -> tuple[str, dict]:
        # Stage 1: Local — analyze structure
        meta["stages"].append("local:analyze")
        ana: dict = {"refactoring_type": "general", "key_concerns": ["readability"], "approach": "systematic refactoring"}
        try:
            raw = await self._local_call(
                system="""\
Analyze this refactoring request. Output ONLY JSON with:
  refactoring_type: "extract_method"|"rename"|"restructure"|"modernize"|"decouple"|"other"
  target_language: programming language
  key_concerns: list of main concerns e.g. ["testability","readability","performance"]
  approach: one-sentence description of the recommended strategy
Output ONLY the JSON object.""",
                user=user_content,
                max_tokens=256,
            )
            ana = json.loads(_strip_fences(raw))
            meta["analysis"] = ana
        except Exception as exc:
            log.warning("refactor_analyze_failed", error=str(exc))
            meta["errors"].append(f"analyze: {exc}")

        # Stage 2: Remote — generate plan
        meta["stages"].append("gpu:plan")
        plan_prompt = f"""\
Create a detailed refactoring plan.

Type: {ana.get("refactoring_type", "general")}
Key concerns: {", ".join(ana.get("key_concerns") or [])}
Approach: {ana.get("approach", "")}

Structure:
## Refactoring Goals
## Step-by-Step Changes
## Before/After Examples (for the most impactful changes)
## Testing Strategy

Original request:
{user_content}"""

        plan = await self._gpu_call(
            pod_endpoint,
            [*self._prior_messages(request), {"role": "user", "content": plan_prompt}],
            max_tokens=3000,
        )

        # Stage 3: Local — validate feasibility
        meta["stages"].append("local:validate")
        validation_note = ""
        try:
            validation = await self._local_call(
                system="Review this refactoring plan. In 2-3 sentences, flag any risks, missing steps, or ordering issues.",
                user=plan,
                max_tokens=200,
            )
            validation_note = f"\n\n> **Validation**: {validation.strip()}\n"
        except Exception as exc:
            log.warning("refactor_validate_failed", error=str(exc))
            meta["errors"].append(f"validate: {exc}")

        # Stage 4: Remote — implement
        meta["stages"].append("gpu:implement")
        impl_prompt = f"""\
Now implement the refactoring.

Plan:
{plan}

Provide the complete refactored code. For large files, show all changed sections with enough context lines.
Add brief comments explaining each non-obvious change."""

        implementation = await self._gpu_call(
            pod_endpoint,
            [*self._prior_messages(request), {"role": "user", "content": impl_prompt}],
            max_tokens=4096,
        )

        final = (
            f"## Refactoring Plan\n\n{plan}{validation_note}"
            f"\n\n---\n\n## Implementation\n\n{implementation}"
        )
        return final, meta

    # ------------------------------------------------------------------
    # Workflow: architecture_design (llm-arch-design)
    # ------------------------------------------------------------------

    async def _architecture_design(
        self, user_content: str, request: ChatCompletionRequest, pod_endpoint: str, meta: dict
    ) -> tuple[str, dict]:
        # Stage 1: Local — parse requirements
        meta["stages"].append("local:parse_requirements")
        reqs: dict = {
            "system_type": "web_api",
            "key_requirements": [],
            "constraints": [],
            "scale_hints": "unknown",
        }
        try:
            raw = await self._local_call(
                system="""\
Parse these architecture requirements. Output ONLY JSON with:
  system_type: e.g. "web_api","microservices","cli_tool","data_pipeline","mobile_backend"
  key_requirements: list of must-have features
  constraints: list of technical or business constraints
  scale_hints: "small"|"medium"|"large"|"unknown"
Output ONLY the JSON object.""",
                user=user_content,
                max_tokens=256,
            )
            reqs = json.loads(_strip_fences(raw))
            meta["requirements"] = reqs
        except Exception as exc:
            log.warning("arch_design_parse_failed", error=str(exc))
            meta["errors"].append(f"parse_requirements: {exc}")

        # Stage 2: Remote — design architecture
        meta["stages"].append("gpu:design")
        design_prompt = f"""\
Design a comprehensive software architecture.

System type: {reqs.get("system_type", "general")}
Key requirements: {", ".join(reqs.get("key_requirements") or [])}
Constraints: {", ".join(reqs.get("constraints") or [])}
Scale: {reqs.get("scale_hints", "unknown")}

Provide:
## Architecture Overview
## Component Diagram (text/ASCII art)
## Data Flow
## Technology Stack (with rationale)
## API Design (key endpoints)
## Database Schema (key tables/entities)
## Security Considerations
## Deployment Strategy

Original request:
{user_content}"""

        architecture = await self._gpu_call(
            pod_endpoint,
            [*self._prior_messages(request), {"role": "user", "content": design_prompt}],
            max_tokens=5000,
        )

        # Stage 3: Local — implementation checklist
        meta["stages"].append("local:checklist")
        checklist_section = ""
        try:
            checklist = await self._local_call(
                system="Generate a prioritized implementation checklist (10-15 items) from this architecture document. Use GitHub markdown checkbox format (- [ ] item).",
                user=architecture,
                max_tokens=400,
            )
            checklist_section = f"\n\n---\n\n## Implementation Checklist\n\n{checklist.strip()}"
        except Exception as exc:
            log.warning("arch_design_checklist_failed", error=str(exc))
            meta["errors"].append(f"checklist: {exc}")

        # Stage 4: Remote — implementation details for key components
        meta["stages"].append("gpu:implementation_details")
        detail_prompt = f"""\
Based on the architecture above, provide concrete implementation details for the 2-3 most critical components:
- Code structure / file layout
- Key interfaces, data models, and contracts
- Critical implementation considerations with code examples

Architecture summary (first 2000 chars):
{architecture[:2000]}

Be specific and practical."""

        details = await self._gpu_call(
            pod_endpoint,
            [*self._prior_messages(request), {"role": "user", "content": detail_prompt}],
            max_tokens=3000,
        )

        final = (
            f"{architecture}{checklist_section}"
            f"\n\n---\n\n## Implementation Details\n\n{details}"
        )
        return final, meta


# ---------------------------------------------------------------------------
# Simple pipeline (preprocess / infer / postprocess stages)
# ---------------------------------------------------------------------------

PREPROCESS_SYSTEM = """\
You are a prompt analyzer for a coding AI system.
Given the user's message, output ONLY valid JSON with these fields:
  intent: one-sentence summary of what the user wants
  files_referenced: list of file paths mentioned (empty list if none)
  complexity_signals: list of keywords indicating complexity (empty if none)
  target_tier_hint: one of "simple", "architecture", "maximum", "ultra"
  structured_prompt: the user's request rewritten clearly for a coding model

Rules:
- Output ONLY the JSON object, no markdown fences, no extra text.
- target_tier_hint is "architecture" if multi-file work; "maximum" if large audit; "ultra" if full-codebase.
"""


async def run_pipeline(
    request: ChatCompletionRequest,
    pipeline: str,
) -> tuple[ChatCompletionRequest, dict]:
    """Return (modified_request, pipeline_meta).

    pipeline_meta keys: stages_run, preprocessor_output, errors
    """
    stages = [s.strip() for s in pipeline.split(",") if s.strip()]
    meta: dict = {"stages_run": stages, "preprocessor_output": None, "errors": []}

    if "preprocess" not in stages:
        return request, meta

    try:
        pp_output = await _run_preprocessor(request)
        meta["preprocessor_output"] = pp_output

        if "structured_prompt" in pp_output:
            new_messages = []
            found_user = False
            for msg in reversed(request.messages):
                if not found_user and msg.role == "user":
                    new_messages.insert(0, ChatMessage(role="user", content=pp_output["structured_prompt"]))
                    found_user = True
                else:
                    new_messages.insert(0, msg)
            request = request.model_copy(update={"messages": new_messages})
    except Exception as exc:
        log.warning("preprocessor_failed", error=str(exc))
        meta["errors"].append(f"preprocessor: {exc}")

    return request, meta


async def run_postprocess(
    completion_text: str,
    pipeline: str,
    meta: dict,
) -> str:
    """Optional post-processing: lightweight cleanup via local Ollama."""
    stages = [s.strip() for s in pipeline.split(",") if s.strip()]
    if "postprocess" not in stages:
        return completion_text

    try:
        return await _run_postprocessor(completion_text)
    except Exception as exc:
        log.warning("postprocessor_failed", error=str(exc))
        meta["errors"].append(f"postprocessor: {exc}")
        return completion_text


async def _run_preprocessor(request: ChatCompletionRequest) -> dict:
    """Call local Ollama; return parsed JSON preprocessor output."""
    last_user = next(
        (m.text_content() for m in reversed(request.messages) if m.role == "user"), ""
    )

    payload = {
        "model": settings.ollama_preprocessor_model,
        "messages": [
            {"role": "system", "content": PREPROCESS_SYSTEM},
            {"role": "user", "content": last_user or ""},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 512},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{settings.ollama_local_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    content = data.get("message", {}).get("content", "")
    return json.loads(content)


_POSTPROCESS_SYSTEM = """\
You are a code formatter. The user will give you an AI response containing code.
Fix any obvious formatting issues: ensure code blocks use correct markdown fences,
remove duplicate blank lines, fix inconsistent indentation in code.
Return ONLY the corrected response — no commentary.
"""


async def _run_postprocessor(text: str) -> str:
    payload = {
        "model": settings.ollama_preprocessor_model,
        "messages": [
            {"role": "system", "content": _POSTPROCESS_SYSTEM},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 4096},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{settings.ollama_local_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()

    return data.get("message", {}).get("content", text)
