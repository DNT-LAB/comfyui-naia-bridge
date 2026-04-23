# -*- coding: utf-8 -*-
"""
ComfyUI ↔ NAIA 2.0 Bridge
=========================
ComfyUI 워크플로우에서 NAIA 2.0 (https://github.com/DNT-LAB/NAIA2.0)의 Remote Web
서버를 통해 프롬프트를 읽고 P.Eng 설정을 원격 조작하는 커스텀 노드 패키지.

노드 4종:
- NAIAPromptFetch_WS        : NAIA 실시간 편집 프롬프트 미러 (WS 캐시)
- NAIARequestRandom         : 새 랜덤 프롬프트 동기 요청 (통합 노드,
                              host/port + apply_peng_override + pre/post/auto_hide +
                              15종 전처리 옵션을 한 노드에 수용)
- NAIAReadPromptEngineering : 현재 NAIA P.Eng 데스크톱 UI 상태 조회 (디버그용)
- NAIACheckHealth           : 연결 진단

기본 사용:
  [NAIARequestRandom (apply_peng_override=false)] → (prompt, negative_prompt)
  → CLIP Text Encode (우클릭 → "Convert text to input") → KSampler → ...
"""
from .nodes_prompt import NAIAPromptFetch_WS
from .nodes_engineering import NAIAReadPromptEngineering
from .nodes_comfyui_api import NAIARequestRandom, NAIACheckHealth

NODE_CLASS_MAPPINGS = {
    "NAIAPromptFetch_WS": NAIAPromptFetch_WS,
    "NAIARequestRandom": NAIARequestRandom,
    "NAIAReadPromptEngineering": NAIAReadPromptEngineering,
    "NAIACheckHealth": NAIACheckHealth,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NAIAPromptFetch_WS": "NAIA Prompt Fetch (WebSocket)",
    "NAIARequestRandom": "NAIA Request Random Prompt",
    "NAIAReadPromptEngineering": "NAIA Read Prompt Engineering",
    "NAIACheckHealth": "NAIA Check Health",
}

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]
