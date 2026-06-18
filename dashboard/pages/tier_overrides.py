"""Per-tier override configuration — provider order, model selection, timeouts."""

from __future__ import annotations

import httpx
import streamlit as st

_BRIDGE_URL = __import__("os").getenv("OPENWEBUI_BRIDGE_URL", "http://bridge:8000").rstrip("/")
_TIMEOUT = 15

_ALL_PROVIDERS = [
    "local", "runpod", "vast", "lambda",
    "groq", "cerebras", "sambanova",
    "openai", "together", "together_dedicated", "mistral", "deepseek",
]

# Curated model lists per API provider (shown as selectbox options)
_PROVIDER_KNOWN_MODELS: dict[str, list[str]] = {
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4o-2024-08-06",
        "gpt-4-turbo",
        "o1-mini",
        "o3-mini",
    ],
    "groq": [
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ],
    "cerebras": [
        "zai-glm-4.7",
        "gpt-oss-120b",
    ],
    "sambanova": [
        "Meta-Llama-3.1-8B-Instruct",
        "Meta-Llama-3.3-70B-Instruct",
        "Meta-Llama-3.1-405B-Instruct",
        "Qwen2.5-72B-Instruct",
        "DeepSeek-R1",
    ],
    "together": [
        "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
        "meta-llama/Llama-Vision-Free",
        "meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
        "deepseek-ai/DeepSeek-V3",
    ],
    "mistral": [
        "mistral-small-latest",
        "mistral-medium-latest",
        "mistral-large-latest",
        "codestral-latest",
        "open-mistral-nemo",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
}

# Known Ollama pod models (for model override dropdowns)
_POD_MODELS = [
    "(use tier default)",
    "qwen2.5-coder:7b-instruct-q4_K_M",
    "qwen2.5-coder:14b-instruct-q4_K_M",
    "qwen2.5-coder:32b-instruct-q4_K_M",
    "qwen2.5:32b-instruct-q4_K_M",
    "qwen2.5:72b-instruct-q4_K_M",
    "gemma4-coder:12b-q4_K_M",
    "deepseek-v3:latest-q4_K_M",
    "llava:13b",
    "mistral:7b-instruct-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
]

_POD_PROVIDERS = {"runpod", "vast", "lambda", "local"}


def _headers() -> dict:
    tok = st.session_state.get("bridge_token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _get_tiers() -> dict:
    try:
        from bridge.router import get_tiers
        return get_tiers()
    except Exception:
        return {}


def _fetch_override(tier: str) -> dict | None:
    try:
        r = httpx.get(
            f"{_BRIDGE_URL}/v1/user/tier-overrides/{tier}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _save_override(tier: str, payload: dict) -> bool:
    try:
        r = httpx.put(
            f"{_BRIDGE_URL}/v1/user/tier-overrides/{tier}",
            headers=_headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        st.error(f"Save failed: {exc}")
        return False


def _delete_override(tier: str) -> bool:
    try:
        r = httpx.delete(
            f"{_BRIDGE_URL}/v1/user/tier-overrides/{tier}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        return r.status_code in (200, 204)
    except Exception as exc:
        st.error(f"Reset failed: {exc}")
        return False


def render() -> None:
    st.title("🎛️ Tier Overrides")
    st.caption(
        "Customize provider order, model selection, and limits for each tier. "
        "Overrides apply only to your requests. Leave fields blank to use tier defaults."
    )

    if flash := st.session_state.pop("tier_ov_flash", None):
        st.success(flash)

    tiers_config = _get_tiers()
    tier_names = list(tiers_config.keys()) if tiers_config else ["simple", "mid", "architecture", "maximum", "ultra", "vision"]

    selected_tier = st.selectbox("Select tier to configure", tier_names, key="tier_ov_select")
    if not selected_tier:
        return

    st.divider()

    tier_def = tiers_config.get(selected_tier, {})
    tier_providers = tier_def.get("provider_overrides", _ALL_PROVIDERS)
    tier_provider_models: dict = tier_def.get("provider_models", {})
    tier_model = tier_def.get("model", "")
    tier_model_no_tools = tier_def.get("model_no_tools", "")
    tier_idle = tier_def.get("idle_timeout_sec", 300)
    tier_max_ctx = tier_def.get("max_context_tokens", 32768)

    # Load current override
    ov = _fetch_override(selected_tier)
    has_override = ov is not None

    if has_override:
        st.info(f"Override active for **{selected_tier}** (last updated {ov['updated_at'][:10]})")

    # ----------------------------------------------------------------
    # Provider order
    # ----------------------------------------------------------------
    st.subheader("Provider Order")
    st.caption("Select providers in the order you want them tried. Unselected = disabled for this tier.")

    if ov and ov.get("provider_order"):
        default_providers = [p for p in ov["provider_order"] if p in _ALL_PROVIDERS]
        # append any tier providers not already in the list
        default_providers += [p for p in tier_providers if p not in default_providers]
    else:
        default_providers = [p for p in tier_providers if p in _ALL_PROVIDERS]

    selected_providers = st.multiselect(
        "Provider order",
        options=_ALL_PROVIDERS,
        default=default_providers,
        key=f"prov_{selected_tier}",
        label_visibility="collapsed",
    )

    # ----------------------------------------------------------------
    # Per-provider model overrides
    # ----------------------------------------------------------------
    st.subheader("Per-Provider Models")
    st.caption("Override the model used per provider. Leave blank to use tier default.")

    ov_provider_models: dict = (ov.get("provider_models") or {}) if ov else {}
    new_provider_models: dict[str, str] = {}

    api_providers_shown = [p for p in (selected_providers or list(tier_provider_models.keys())) if p not in _POD_PROVIDERS]
    if api_providers_shown:
        cols = st.columns(min(len(api_providers_shown), 3))
        for i, prov in enumerate(api_providers_shown):
            tier_default = tier_provider_models.get(prov, "")
            current = ov_provider_models.get(prov) or tier_default
            known = _PROVIDER_KNOWN_MODELS.get(prov, [])
            options = list(dict.fromkeys([tier_default] + known))  # tier default first, deduped
            if current and current not in options:
                options.insert(0, current)
            try:
                idx = options.index(current) if current else 0
            except ValueError:
                idx = 0
            chosen = cols[i % 3].selectbox(
                prov,
                options=options,
                index=idx,
                key=f"pm_{selected_tier}_{prov}",
                help=f"Tier default: {tier_default or '(none)'}",
            )
            if chosen and chosen != tier_default:
                new_provider_models[prov] = chosen
    else:
        st.caption("_No API providers selected._")

    # ----------------------------------------------------------------
    # Pod model overrides
    # ----------------------------------------------------------------
    st.subheader("Pod Model Override")
    st.caption(
        "Override the Ollama model pulled and served on GPU pods (RunPod, Vast, Lambda, local) "
        "for this tier. Useful if you have a personal RunPod account with a network volume "
        "and want a different model than the tier default. "
        "**Does not affect API providers** (OpenAI, Groq, etc.) — use the dropdowns above for those."
    )

    cur_model_ov = (ov.get("model_override") or "(use tier default)") if ov else "(use tier default)"
    cur_mnt_ov = (ov.get("model_no_tools_override") or "(use tier default)") if ov else "(use tier default)"

    pod_options = list(dict.fromkeys(["(use tier default)", tier_model] + _POD_MODELS)) if tier_model else _POD_MODELS
    mnt_options = list(dict.fromkeys(["(use tier default)", tier_model_no_tools or tier_model] + _POD_MODELS)) if tier_model else _POD_MODELS

    def _pod_idx(val: str, opts: list[str]) -> int:
        try:
            return opts.index(val) if val in opts else 0
        except ValueError:
            return 0

    col1, col2 = st.columns(2)
    model_override_val = col1.selectbox(
        f"Pod model (tools)  —  default: `{tier_model or 'none'}`",
        options=pod_options,
        index=_pod_idx(cur_model_ov, pod_options),
        key=f"mo_{selected_tier}",
    )
    model_no_tools_val = col2.selectbox(
        f"Pod model (no tools)  —  default: `{tier_model_no_tools or tier_model or 'same'}`",
        options=mnt_options,
        index=_pod_idx(cur_mnt_ov, mnt_options),
        key=f"mnt_{selected_tier}",
    )

    # ----------------------------------------------------------------
    # Limits
    # ----------------------------------------------------------------
    st.subheader("Limits")
    lcol1, lcol2 = st.columns(2)
    idle_val = lcol1.number_input(
        "Idle timeout (seconds)",
        min_value=60,
        max_value=7200,
        value=(ov.get("idle_timeout_sec") or tier_idle) if ov else tier_idle,
        step=60,
        key=f"idle_{selected_tier}",
    )
    ctx_val = lcol2.number_input(
        "Max context tokens",
        min_value=1024,
        max_value=524288,
        value=(ov.get("max_context_tokens") or tier_max_ctx) if ov else tier_max_ctx,
        step=1024,
        key=f"ctx_{selected_tier}",
    )

    st.divider()

    bcol1, bcol2 = st.columns([1, 3])
    save_pressed = bcol1.button("Save override", type="primary", key=f"save_{selected_tier}")
    reset_pressed = bcol2.button(
        "Reset to defaults",
        type="secondary",
        key=f"reset_{selected_tier}",
        disabled=not has_override,
    )

    if save_pressed:
        _sentinel = "(use tier default)"
        payload = {
            "provider_order": selected_providers or None,
            "disabled_providers": [p for p in _ALL_PROVIDERS if p not in selected_providers] or None,
            "provider_models": new_provider_models or None,
            "model_override": model_override_val if model_override_val != _sentinel else None,
            "model_no_tools_override": model_no_tools_val if model_no_tools_val != _sentinel else None,
            "idle_timeout_sec": int(idle_val) if idle_val != tier_idle else None,
            "max_context_tokens": int(ctx_val) if ctx_val != tier_max_ctx else None,
        }
        if _save_override(selected_tier, payload):
            st.session_state["tier_ov_flash"] = f"Override saved for **{selected_tier}**."
            st.rerun()

    if reset_pressed:
        if _delete_override(selected_tier):
            st.session_state["tier_ov_flash"] = f"Override reset for **{selected_tier}**."
            st.rerun()
