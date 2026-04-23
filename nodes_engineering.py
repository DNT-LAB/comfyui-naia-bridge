# -*- coding: utf-8 -*-
"""
NAIA Prompt Engineering 읽기 노드 (디버그/검토용)
==================================================
- NAIAReadPromptEngineering : 현재 NAIA 데스크톱 UI의 P.Eng 상태 조회 (WS module_state)

쓰기는 `NAIARequestRandom`에 통합됨 (nodes_comfyui_api.py).
"""
from __future__ import annotations

import json
import logging

from .ws_client import NAIAWebSocketClient

logger = logging.getLogger("comfyui-naia-bridge.peng")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7243
MODULE_STATE_TIMEOUT = 5.0

# NAIA의 15종 전처리 키 (modules/prompt_engineering_module.py:214 option_key_map 기준)
# nodes_comfyui_api.py 에서 import 하여 재사용.
PREPROCESSING_KEYS = [
    "remove_author",
    "remove_work_title",
    "remove_character_name",
    "remove_character_features",
    "remove_clothes",
    "remove_color",
    "remove_location_and_background_color",
    "remove_expression",
    "remove_pose_action",
    "remove_meta_tags",
    "remove_object_tags",
    "remove_noise_tags",
    "e621_auto_boost",
    "danbooru_auto_weight",
    "tag_implication_compression",
]

PP_STATE_CHOICES = ["skip", "on", "off"]


class NAIAReadPromptEngineering:
    """NAIA의 현재 Prompt Engineering 모듈 상태를 WebSocket으로 조회.

    반환값:
    - pre_prompt, post_prompt, auto_hide : 현재 데스크톱 UI 텍스트
    - preprocessing_json : 15종 옵션 상태 JSON (복사해서 NAIARequestRandom에 수동 주입 가능)
    - preset_list : 프리셋 이름 개행 분리
    - current_preset : 현재 선택된 프리셋

    용도: 디버그, NAIA UI 값을 NAIARequestRandom 노드에 복사하기 전 조회.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "host": ("STRING", {
                    "default": DEFAULT_HOST,
                    "tooltip": "NAIA Remote API 호스트 (기본 127.0.0.1).",
                }),
                "port": ("INT", {
                    "default": DEFAULT_PORT, "min": 1, "max": 65535,
                    "tooltip": "NAIA Remote API 포트 (기본 7243).",
                }),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "pre_prompt",
        "post_prompt",
        "auto_hide",
        "preprocessing_json",
        "preset_list",
        "current_preset",
    )
    FUNCTION = "read"
    CATEGORY = "NAIA Bridge/Engineering"

    @classmethod
    def IS_CHANGED(cls, host, port):
        return float("nan")

    def read(self, host: str, port: int):
        client = NAIAWebSocketClient.get(host, port)
        if not client.wait_connected(timeout=MODULE_STATE_TIMEOUT):
            raise RuntimeError(
                f"[NAIA Bridge] WebSocket 연결 실패: {host}:{port}."
            )
        state = client.wait_for_module_state(
            "prompt_engineering", timeout=MODULE_STATE_TIMEOUT
        )
        if state is None:
            raise RuntimeError(
                f"[NAIA Bridge] prompt_engineering 모듈 상태 응답 없음 "
                f"({MODULE_STATE_TIMEOUT:.0f}초 타임아웃)."
            )
        preprocessing = state.get("preprocessing", {}) or {}
        preset_list = state.get("preset_options", []) or []
        return (
            state.get("pre_prompt", "") or "",
            state.get("post_prompt", "") or "",
            state.get("auto_hide", "") or "",
            json.dumps(preprocessing, ensure_ascii=False, indent=2),
            "\n".join(preset_list),
            state.get("preset", "") or "",
        )
