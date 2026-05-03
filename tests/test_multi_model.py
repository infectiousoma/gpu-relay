"""Unit tests for bridge/multi_model.py — orchestration and workflow models.

Covers:
  - PromptPreprocessor.optimize_prompt / extract_intent
  - WorkflowOrchestrator.execute for all four workflows
  - WORKFLOW_MODELS registry structure
  - _strip_fences helper
  - Existing run_pipeline / run_postprocess (regression)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import respx
from httpx import Response

from bridge.multi_model import (
    WORKFLOW_MODELS,
    PromptPreprocessor,
    WorkflowOrchestrator,
    _strip_fences,
    run_pipeline,
    run_postprocess,
)


# ---------------------------------------------------------------------------
# _strip_fences
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_plain_json_unchanged(self):
        s = '{"a": 1}'
        assert _strip_fences(s) == s

    def test_backtick_fence_stripped(self):
        s = "```json\n{\"a\": 1}\n```"
        assert _strip_fences(s) == '{"a": 1}'

    def test_backtick_fence_no_lang_stripped(self):
        s = "```\n{\"x\": 2}\n```"
        assert _strip_fences(s) == '{"x": 2}'

    def test_leading_trailing_whitespace(self):
        s = '  {"b": 3}  '
        assert _strip_fences(s) == '{"b": 3}'


# ---------------------------------------------------------------------------
# WORKFLOW_MODELS registry
# ---------------------------------------------------------------------------

class TestWorkflowModels:
    def test_required_keys_present(self):
        required = {"id", "name", "gpu_tier", "workflow", "description"}
        for model_id, defn in WORKFLOW_MODELS.items():
            assert required <= defn.keys(), f"{model_id} missing keys"

    def test_ids_match_keys(self):
        for key, defn in WORKFLOW_MODELS.items():
            assert defn["id"] == key

    def test_all_four_workflows_present(self):
        workflows = {d["workflow"] for d in WORKFLOW_MODELS.values()}
        assert "optimize_and_execute" in workflows
        assert "code_review" in workflows
        assert "refactoring" in workflows
        assert "architecture_design" in workflows

    def test_llm_smart_tier_is_none(self):
        assert WORKFLOW_MODELS["llm-smart"]["gpu_tier"] is None

    def test_code_review_uses_architecture_tier(self):
        assert WORKFLOW_MODELS["llm-code-review"]["gpu_tier"] == "architecture"

    def test_arch_design_uses_maximum_tier(self):
        assert WORKFLOW_MODELS["llm-arch-design"]["gpu_tier"] == "maximum"


# ---------------------------------------------------------------------------
# PromptPreprocessor
# ---------------------------------------------------------------------------

class TestPromptPreprocessor:
    @pytest.mark.asyncio
    @respx.mock
    async def test_optimize_prompt_happy_path(self):
        response_json = {
            "task_type": "bug_fix",
            "language": "python",
            "complexity": "simple",
            "optimized_prompt": "Fix the null pointer in main.py line 42.",
            "suggested_tier": "simple",
            "context_hints": {"framework": "flask"},
        }

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(200, json={"message": {"content": json.dumps(response_json)}})
            )

            pp = PromptPreprocessor()
            result = await pp.optimize_prompt("fix my code")

        assert result["task_type"] == "bug_fix"
        assert result["suggested_tier"] == "simple"
        assert "null pointer" in result["optimized_prompt"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_optimize_prompt_strips_fences(self):
        response_json = {"task_type": "refactor", "language": "python",
                         "complexity": "moderate", "optimized_prompt": "Refactor X",
                         "suggested_tier": "architecture", "context_hints": {}}
        fenced = f"```json\n{json.dumps(response_json)}\n```"

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(200, json={"message": {"content": fenced}})
            )

            pp = PromptPreprocessor()
            result = await pp.optimize_prompt("refactor my code")

        assert result["task_type"] == "refactor"

    @pytest.mark.asyncio
    @respx.mock
    async def test_extract_intent_happy_path(self):
        response_json = {
            "primary_intent": "Build a REST API with auth",
            "complexity_trend": "increasing",
            "suggested_tier": "architecture",
            "pivot_detected": False,
            "summary": "User wants a FastAPI project with JWT auth.",
        }

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(200, json={"message": {"content": json.dumps(response_json)}})
            )

            pp = PromptPreprocessor()
            result = await pp.extract_intent([
                {"role": "user", "content": "I want a REST API"},
                {"role": "assistant", "content": "Sure, what auth do you need?"},
                {"role": "user", "content": "JWT auth with refresh tokens"},
            ])

        assert result["suggested_tier"] == "architecture"
        assert result["pivot_detected"] is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_ollama_error_propagates(self):
        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(500, text="server error")
            )

            pp = PromptPreprocessor()
            with pytest.raises(Exception):
                await pp.optimize_prompt("hello")


# ---------------------------------------------------------------------------
# WorkflowOrchestrator
# ---------------------------------------------------------------------------

def _make_chat_request(content: str = "Fix bugs", model: str = "llm-smart"):
    from tests.conftest import make_chat_request
    return make_chat_request(content, model=model)


GPU_COMPLETION = {"choices": [{"message": {"content": "GPU response here"}}]}


class TestWorkflowOrchestratorDispatch:
    @pytest.mark.asyncio
    async def test_unknown_workflow_raises(self):
        orch = WorkflowOrchestrator()
        req = _make_chat_request()
        with pytest.raises(ValueError, match="Unknown workflow"):
            await orch.execute("nonexistent_workflow", req, "http://pod:11434")

    @pytest.mark.asyncio
    @respx.mock
    async def test_optimize_and_execute_happy_path(self):
        optimize_resp = {
            "task_type": "bug_fix",
            "language": "python",
            "complexity": "simple",
            "optimized_prompt": "Carefully fix the null pointer in line 42.",
            "suggested_tier": "simple",
            "context_hints": {},
        }

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            # Local Ollama call (optimize)
            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(200, json={"message": {"content": json.dumps(optimize_resp)}})
            )
            # Remote GPU call
            respx.post("http://pod:11434/v1/chat/completions").mock(
                return_value=Response(200, json=GPU_COMPLETION)
            )

            orch = WorkflowOrchestrator()
            req = _make_chat_request("fix my code", model="llm-smart")
            text, meta = await orch.execute("optimize_and_execute", req, "http://pod:11434")

        assert "GPU response here" in text
        assert "local:optimize" in meta["stages"]
        assert "gpu:execute" in meta["stages"]
        assert meta["preprocessor_output"]["task_type"] == "bug_fix"

    @pytest.mark.asyncio
    @respx.mock
    async def test_optimize_and_execute_local_failure_falls_back(self):
        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(500, text="ollama down")
            )
            respx.post("http://pod:11434/v1/chat/completions").mock(
                return_value=Response(200, json=GPU_COMPLETION)
            )

            orch = WorkflowOrchestrator()
            req = _make_chat_request("fix my code", model="llm-smart")
            text, meta = await orch.execute("optimize_and_execute", req, "http://pod:11434")

        # Falls back to original prompt; GPU still called
        assert "GPU response here" in text
        assert len(meta["errors"]) > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_code_review_all_stages_run(self):
        local_cat = {"scope": "single_file", "focus_areas": ["correctness", "style"], "estimated_complexity": "simple"}
        local_summary = "- Issue 1: missing null check\n- Issue 2: unused variable"

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            # Two local calls: categorize + summarize
            respx.post("http://ollama:11434/api/chat").mock(
                side_effect=[
                    Response(200, json={"message": {"content": json.dumps(local_cat)}}),
                    Response(200, json={"message": {"content": local_summary}}),
                ]
            )
            respx.post("http://pod:11434/v1/chat/completions").mock(
                return_value=Response(200, json=GPU_COMPLETION)
            )

            orch = WorkflowOrchestrator()
            req = _make_chat_request("review my code", model="llm-code-review")
            text, meta = await orch.execute("code_review", req, "http://pod:11434")

        assert "Executive Summary" in text
        assert "Detailed Findings" in text
        assert meta["stages"] == ["local:categorize", "gpu:review", "local:summarize"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_code_review_local_failures_dont_break_gpu(self):
        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(500, text="ollama down")
            )
            respx.post("http://pod:11434/v1/chat/completions").mock(
                return_value=Response(200, json=GPU_COMPLETION)
            )

            orch = WorkflowOrchestrator()
            req = _make_chat_request("review code")
            text, meta = await orch.execute("code_review", req, "http://pod:11434")

        assert "GPU response here" in text
        assert len(meta["errors"]) > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_refactoring_four_stages(self):
        local_analysis = {"refactoring_type": "extract_method", "target_language": "python",
                          "key_concerns": ["readability"], "approach": "extract helper functions"}
        local_validation = "The plan looks feasible; ensure tests cover edge cases."

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                side_effect=[
                    Response(200, json={"message": {"content": json.dumps(local_analysis)}}),
                    Response(200, json={"message": {"content": local_validation}}),
                ]
            )
            respx.post("http://pod:11434/v1/chat/completions").mock(
                side_effect=[
                    Response(200, json={"choices": [{"message": {"content": "## Refactoring Plan\n..."}}]}),
                    Response(200, json={"choices": [{"message": {"content": "## Implementation\n..."}}]}),
                ]
            )

            orch = WorkflowOrchestrator()
            req = _make_chat_request("refactor my god object class", model="llm-refactor")
            text, meta = await orch.execute("refactoring", req, "http://pod:11434")

        assert "Refactoring Plan" in text
        assert "Implementation" in text
        assert "Validation" in text
        assert meta["stages"] == ["local:analyze", "gpu:plan", "local:validate", "gpu:implement"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_architecture_design_four_stages(self):
        local_reqs = {"system_type": "web_api", "key_requirements": ["auth", "CRUD"],
                      "constraints": ["PostgreSQL"], "scale_hints": "medium"}
        local_checklist = "- [ ] Set up FastAPI project\n- [ ] Add database models"

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                side_effect=[
                    Response(200, json={"message": {"content": json.dumps(local_reqs)}}),
                    Response(200, json={"message": {"content": local_checklist}}),
                ]
            )
            respx.post("http://pod:11434/v1/chat/completions").mock(
                side_effect=[
                    Response(200, json={"choices": [{"message": {"content": "## Architecture Overview\n..."}}]}),
                    Response(200, json={"choices": [{"message": {"content": "## Implementation Details\n..."}}]}),
                ]
            )

            orch = WorkflowOrchestrator()
            req = _make_chat_request("design a REST API with auth", model="llm-arch-design")
            text, meta = await orch.execute("architecture_design", req, "http://pod:11434")

        assert "Architecture Overview" in text
        assert "Implementation Checklist" in text
        assert "Implementation Details" in text
        assert meta["stages"] == [
            "local:parse_requirements", "gpu:design",
            "local:checklist", "gpu:implementation_details",
        ]


# ---------------------------------------------------------------------------
# run_pipeline / run_postprocess regression
# ---------------------------------------------------------------------------

class TestRunPipelineRegression:
    @pytest.mark.asyncio
    @respx.mock
    async def test_preprocess_rewrites_user_message(self):
        from tests.conftest import make_chat_request

        preprocessor_response = {
            "message": {
                "content": json.dumps({
                    "intent": "fix a bug",
                    "files_referenced": ["main.py"],
                    "complexity_signals": [],
                    "target_tier_hint": "simple",
                    "structured_prompt": "Fix the null pointer in main.py line 42.",
                })
            }
        }

        with patch("bridge.multi_model.settings") as s:
            s.ollama_local_url = "http://ollama:11434"
            s.ollama_preprocessor_model = "test-model"

            respx.post("http://ollama:11434/api/chat").mock(
                return_value=Response(200, json=preprocessor_response)
            )

            req = make_chat_request("fix my code", pipeline="preprocess,infer")
            modified_req, meta = await run_pipeline(req, "preprocess,infer")

        last_user = next(m for m in reversed(modified_req.messages) if m.role == "user")
        assert "null pointer" in last_user.content
        assert meta["preprocessor_output"]["intent"] == "fix a bug"

    @pytest.mark.asyncio
    async def test_infer_only_pipeline_no_preprocessing(self):
        from tests.conftest import make_chat_request

        req = make_chat_request("hello", pipeline="infer")
        modified_req, meta = await run_pipeline(req, "infer")

        assert meta["preprocessor_output"] is None
        assert len(meta["errors"]) == 0
