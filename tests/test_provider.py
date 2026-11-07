"""Provider 层测试（T-P1-04）。"""
from __future__ import annotations

from langchain_openai import ChatOpenAI

from agents.llm_factory import build_light_llm, build_main_llm
from agents.provider import ProviderSpec, filter_supported_kwargs
from config import Settings


def test_filter_drops_unknown_and_empty():
    usable, dropped = filter_supported_kwargs(
        ChatOpenAI, {"model": "gpt-x", "streaming": True, "bogus": 1, "reasoning_effort": ""}
    )
    assert usable == {"model": "gpt-x", "streaming": True}
    assert dropped == ["bogus"]


def test_main_llm_defaults():
    st = Settings(openai_api_key="sk-test", model_name="gpt-5.1")
    llm = build_main_llm(st)
    assert llm.model_name == "gpt-5.1"
    assert llm.streaming is True
    assert llm.stream_usage is True            # 流式必须回传 usage，否则成本统计为 0
    assert llm.use_responses_api is False       # 默认关闭，显式开启


def test_responses_api_flag_is_honoured():
    st = Settings(openai_api_key="sk-test", use_responses_api=True)
    llm = build_main_llm(st)
    assert llm.use_responses_api is True


def test_reasoning_effort_passed_when_set():
    st = Settings(openai_api_key="sk-test", reasoning_effort="medium")
    assert build_main_llm(st).reasoning_effort == "medium"


def test_prompt_cache_key_goes_to_model_kwargs():
    st = Settings(openai_api_key="sk-test", prompt_cache_key="kb-v1")
    llm = build_main_llm(st)
    assert llm.model_kwargs.get("prompt_cache_key") == "kb-v1"


def test_light_tier_is_not_streaming():
    st = Settings(openai_api_key="sk-test", model_name="gpt-5.1", light_model_name="gpt-5.1-mini")
    light = build_light_llm(st)
    assert light.model_name == "gpt-5.1-mini"
    assert light.streaming is False


def test_light_reuses_main_when_unset():
    st = Settings(openai_api_key="sk-test", model_name="gpt-5.1", light_model_name="")
    main = build_main_llm(st)
    assert build_light_llm(st, main_llm=main) is main


def test_demo_mode_needs_no_key():
    st = Settings(demo_mode=True)
    llm = build_main_llm(st)
    assert type(llm).__name__ == "ScriptedChatModel"


def test_spec_for_tier_reflects_settings():
    st = Settings(model_name="a", light_model_name="b", temperature=0.7, llm_max_retries=3)
    main = ProviderSpec.for_tier(st, "main")
    light = ProviderSpec.for_tier(st, "light")
    assert main.model == "a" and main.temperature == 0.7 and main.streaming is True
    assert light.model == "b" and light.temperature == 0.0 and light.streaming is False
    assert main.max_retries == 3
