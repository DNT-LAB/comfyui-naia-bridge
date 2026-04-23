# -*- coding: utf-8 -*-
"""
NAIA ComfyUI 전용 sync API 노드
================================
- NAIARequestRandom : POST /api/comfyui/random 동기 요청 (통합 노드)
                     호스트/포트 + apply_peng_override 토글 + pre/post/auto_hide +
                     15종 전처리 옵션을 단일 노드에서 모두 설정.
- NAIACheckHealth   : GET /api/comfyui/health. 연결 진단용.

NAIA 서버 측 Phase 2+2.5 패치 필수 (core/remote_api_server.py).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .nodes_engineering import PREPROCESSING_KEYS, PP_STATE_CHOICES

logger = logging.getLogger("comfyui-naia-bridge.api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7243
# NAIA 실측 응답 <0.3초. 30초는 비정상 상황 대비 안전망.
FIXED_TIMEOUT = 30.0
# ComfyUI 쪽 HTTP 타임아웃은 sync timeout + 여유 5초
HTTP_TIMEOUT = FIXED_TIMEOUT + 5.0

# NAIA 프롬프트에 포함된 '#랜덤프롬프트' 같은 줄 단위 섹션 주석을 제거.
# ComfyUI CLIPTextEncode는 '#'을 주석으로 처리하지 않고 그대로 토크나이저에 넘기므로
# 조건화(conditioning)에 미약한 노이즈가 됨. 브리지에서 선제적으로 제거.
#
# 주의: NovelAI 이스케이프 syntax `#(...)` (인라인 괄호 이스케이프)는 보호.
# '#'이 줄 시작(^) + 공백만 앞선 경우에만 주석으로 간주. re.MULTILINE 필수.
# 보호되는 패턴: `hex maniac #(pokemon#)`, `@samidare #(hoshi#):1.15` 등.
# 제거되는 패턴: `\n#랜덤프롬프트\n`, `  # some note\n` 등.
_HASH_COMMENT_RE = re.compile(r"^[ \t]*#[^\n]*", re.MULTILINE)
_MULTI_COMMA_RE = re.compile(r"(\s*,){2,}")


def _clean_prompt(s: str) -> str:
    if not s:
        return s
    s = _HASH_COMMENT_RE.sub("", s)
    s = _MULTI_COMMA_RE.sub(",", s)
    return s.strip(" ,\n\t")


def _post_random(host: str, port: int, body: dict) -> dict:
    """Blocking POST to /api/comfyui/random. Returns parsed JSON. Raises on error."""
    try:
        import requests
    except ImportError:
        raise RuntimeError(
            "[NAIA Bridge] requests 미설치. `pip install requests` 또는 "
            "ComfyUI-Manager의 requirements 자동 설치를 사용하세요."
        )
    url = f"http://{host}:{int(port)}/api/comfyui/random"
    try:
        r = requests.post(url, json=body, timeout=HTTP_TIMEOUT)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"[NAIA Bridge] NAIA 서버 연결 실패: {host}:{port}. "
            f"NAIA가 실행 중이고 Web Session이 활성화되어 있는지 확인하세요."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"[NAIA Bridge] HTTP 타임아웃 ({HTTP_TIMEOUT:.0f}초). NAIA 응답 지연."
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"[NAIA Bridge] 요청 오류: {e}")

    if r.status_code == 504:
        raise RuntimeError(
            f"[NAIA Bridge] NAIA 서버가 {FIXED_TIMEOUT:.0f}초 내에 프롬프트를 생성하지 못했습니다."
        )
    if r.status_code == 400:
        try:
            detail = r.json().get("error", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"[NAIA Bridge] 잘못된 요청 (400): {detail}")
    if not r.ok:
        raise RuntimeError(f"[NAIA Bridge] HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"[NAIA Bridge] 응답 JSON 파싱 실패: {r.text[:200]}")


class NAIARequestRandom:
    """NAIA에서 새 랜덤 프롬프트를 동기적으로 요청 (통합 노드).

    기본 동작 (use_naia_settings = True):
    - NAIA 데스크톱 UI의 Prompt Engineering 설정 그대로 사용
    - NAIA 메인 UI 불변

    Override 모드 (use_naia_settings = False):
    - 노드 내 pre/post/auto_hide 텍스트 + 15종 전처리 옵션을 NAIA로 전송
    - NAIA의 데스크톱 UI 값은 무시됨 (all-or-nothing 규약)
    - preprocessing: skip = 키 omit (NAIA에서 OFF로 처리), on = True, off = False
    - NAIA 메인 UI 불변 (해당 요청에 한해서만 적용)

    NAIA 자체 이미지 생성 연동:
    - NAIA의 "자동 생성" 체크박스 상태를 그대로 따름
    - 체크되어 있으면 NAIA도 같은 프롬프트로 병렬 이미지 생성
    - ComfyUI만 단독 생성하려면 NAIA에서 "자동 생성" 체크 해제

    반환: (prompt, negative_prompt)
    """

    @classmethod
    def INPUT_TYPES(cls):
        # 순서: 가장 중요한 토글 → override 입력 → 전처리 → 연결 설정(맨 아래)
        required: dict = {
            "use_naia_settings": ("BOOLEAN", {
                "default": True,
                "tooltip": (
                    "true (기본): NAIA 데스크톱 앱의 Prompt Engineering 설정을 그대로 사용. "
                    "아래 pre_prompt / post_prompt / auto_hide / remove_* 위젯은 무시됨.\n"
                    "false: 아래 위젯 값들을 NAIA로 전송해 이번 요청에 한해 override. "
                    "NAIA 데스크톱 UI는 불변. all-or-nothing 규약 (미지정 필드는 빈 값/OFF)."
                ),
            }),
            "pre_prompt": ("STRING", {
                "multiline": True,
                "default": "",
                "placeholder": "pre_prompt — 프롬프트 맨 앞에 붙을 태그들",
                "tooltip": "NAIA P.Eng의 '선행 프롬프트(pre_prompt)' 덮어쓰기.",
            }),
            "post_prompt": ("STRING", {
                "multiline": True,
                "default": "",
                "placeholder": "post_prompt — 프롬프트 맨 뒤에 붙을 태그들",
                "tooltip": "NAIA P.Eng의 '후행 프롬프트(post_prompt)' 덮어쓰기.",
            }),
            "auto_hide": ("STRING", {
                "multiline": True,
                "default": "",
                "placeholder": "auto_hide — 자동 숨김 태그 목록",
                "tooltip": "NAIA P.Eng의 '자동 숨김(auto_hide)' 덮어쓰기.",
            }),
        }
        # 15종 전처리 옵션 — 3-state COMBO
        pp_tooltip_base = (
            "• skip: 이 키를 NAIA로 안 보냄 (NAIA에서 OFF로 해석됨 = 태그 유지)\n"
            "• on: 이 카테고리 태그 제거\n"
            "• off: 이 카테고리 태그 유지 (명시적)"
        )
        for key in PREPROCESSING_KEYS:
            required[key] = (PP_STATE_CHOICES, {
                "default": "skip",
                "tooltip": pp_tooltip_base,
            })
        # 연결 설정 — 99% 사용자는 기본값 그대로 사용
        required["host"] = ("STRING", {
            "default": DEFAULT_HOST,
            "tooltip": (
                "NAIA Remote API 호스트. NAIA가 같은 PC에서 실행되면 기본값 그대로 사용. "
                "다른 PC에서 돌리는 경우만 IP 변경."
            ),
        })
        required["port"] = ("INT", {
            "default": DEFAULT_PORT, "min": 1, "max": 65535,
            "tooltip": (
                "NAIA Remote API 포트. NAIA Settings > Web Session에서 설정한 포트와 일치해야 함. "
                "기본값 7243."
            ),
        })
        return {"required": required}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("prompt", "negative_prompt")
    FUNCTION = "request"
    CATEGORY = "NAIA Bridge/API"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def request(
        self,
        host: str,
        port: int,
        use_naia_settings: bool,
        pre_prompt: str,
        post_prompt: str,
        auto_hide: str,
        **pp_kwargs,
    ):
        body: dict = {
            "timeout": FIXED_TIMEOUT,
            "respect_naia_autogen": True,
            "force_naia_skip_generate": False,
        }

        if not use_naia_settings:
            # 사용자가 데스크톱 설정을 거부 → 노드 widget 값으로 override 구성
            preprocessing_options: dict = {}
            for key in PREPROCESSING_KEYS:
                state = pp_kwargs.get(key, "skip")
                if state == "on":
                    preprocessing_options[key] = True
                elif state == "off":
                    preprocessing_options[key] = False
                # skip: 키 omit (NAIA에서 OFF로 해석됨)
            body["peng_override"] = {
                "pre_prompt": pre_prompt,
                "post_prompt": post_prompt,
                "auto_hide": auto_hide,
                "preprocessing_options": preprocessing_options,
            }

        resp = _post_random(host, port, body)
        prompt = _clean_prompt(resp.get("prompt", "") or "")
        negative = _clean_prompt(resp.get("negative_prompt", "") or "")
        logger.debug(
            "request_id=%s prompt_len=%d naia_started=%s",
            resp.get("request_id"), len(prompt), resp.get("naia_started_generation"),
        )
        return (prompt, negative)


class NAIACheckHealth:
    """NAIA 서버 연결 상태 확인. 진단/게이팅 용도.

    반환:
    - ok : True/False (응답이 정상인지)
    - status_json : 서버 응답 전문

    실패 시 raise 하지 않고 ok=False 반환 (게이팅 특성).
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

    RETURN_TYPES = ("BOOLEAN", "STRING")
    RETURN_NAMES = ("ok", "status_json")
    FUNCTION = "check"
    CATEGORY = "NAIA Bridge/API"

    @classmethod
    def IS_CHANGED(cls, host, port):
        return float("nan")

    def check(self, host: str, port: int):
        try:
            import requests
        except ImportError:
            return (False, json.dumps({"ok": False, "error": "requests not installed"}))
        url = f"http://{host}:{int(port)}/api/comfyui/health"
        try:
            r = requests.get(url, timeout=5)
            data = r.json()
            ok = bool(data.get("ok", False)) and r.ok
            return (ok, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            return (False, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
