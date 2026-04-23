# -*- coding: utf-8 -*-
"""
NAIA 읽기 노드 (Phase 1)
========================
- NAIAPromptFetch_WS: WS 캐시에서 NAIA가 편집/생성 중인 현재 프롬프트를 반환

실패 정책: 표준 `raise RuntimeError(...)` (ComfyUI UI에 빨간 에러 표시).
NAIA가 꺼져 있거나 연결 불가 시 워크플로우 명시적 실패.
"""
from __future__ import annotations

import logging
import time

from .ws_client import NAIAWebSocketClient

logger = logging.getLogger("comfyui-naia-bridge.prompt")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7243
# 첫 실행 시 WS 연결 + NAIA 초기 메시지 버스트 + prompt_sync 수신까지 대기(초).
# 실측 ~3.2초 소요(NAIA 2.0 기준). 안전 마진 포함해 7초.
WS_INITIAL_WAIT = 7.0


class NAIAPromptFetch_WS:
    """NAIA의 WebSocket 캐시에서 현재 프롬프트를 즉시 반환.

    NAIA 메인 UI 또는 Web Remote에서 편집 중인 값을 실시간 반영.
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

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("prompt", "negative_prompt")
    FUNCTION = "fetch"
    CATEGORY = "NAIA Bridge/Prompt"

    @classmethod
    def IS_CHANGED(cls, host, port):
        # ComfyUI 캐싱 우회: 매 실행마다 재평가
        return float("nan")

    def fetch(self, host: str, port: int):
        client = NAIAWebSocketClient.get(host, port)

        # 첫 실행 직후 캐시가 비어있을 수 있음 → 최대 WS_INITIAL_WAIT 초 대기
        prompt, negative, connected, last_update = client.get_prompts()
        if not connected or last_update == 0:
            if not client.wait_connected(timeout=WS_INITIAL_WAIT):
                # 정확한 원인 진단을 위해 현재 상태 재조회
                _, _, connected_now, last_update_now = client.get_prompts()
                if not connected_now:
                    raise RuntimeError(
                        f"[NAIA Bridge] WebSocket 연결 실패: {host}:{port}. "
                        f"NAIA가 실행 중이고 Settings > Web Session이 활성화되어 있는지 확인하세요."
                    )
                else:
                    raise RuntimeError(
                        f"[NAIA Bridge] WebSocket 연결됐으나 prompt_sync 메시지를 "
                        f"{WS_INITIAL_WAIT:.0f}초 이내 받지 못했습니다. "
                        f"NAIA 메인 UI에서 프롬프트 편집이나 랜덤 생성을 한번 수행해 보세요."
                    )
            prompt, negative, connected, last_update = client.get_prompts()

        age = time.time() - last_update if last_update else -1
        logger.debug("fetched (age=%.1fs, len=%d)", age, len(prompt))
        return (prompt, negative)
