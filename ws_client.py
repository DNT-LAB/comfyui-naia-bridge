# -*- coding: utf-8 -*-
"""
NAIA WebSocket 클라이언트 (싱글턴)
==================================
- 백그라운드 데몬 스레드에서 ws://host:port/ws 에 상시 연결 유지
- prompt_sync 메시지를 캐시 (읽기용)
- set_module_param 메시지 송신 함수 제공 (쓰기용, Phase 3)
- module_state 메시지를 module_id별 캐시 (Prompt Engineering 등 조회용)

스레드 안전:
- 인스턴스 dict: _instances_lock
- 인스턴스 데이터: _lock
- WS send는 lock 밖 (websocket-client는 send/recv 동시성 허용)

재연결: 연결 끊기면 WS_RECONNECT_DELAY 초 후 자동 재시도.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("comfyui-naia-bridge.ws")

WS_RECONNECT_DELAY = 2.0  # 재연결 대기(초)
WS_RECV_TIMEOUT = 30      # recv 타임아웃(초) — 유휴 연결 keepalive용
CLIENT_STATE_TIMEOUT = 3  # NAIA 서버가 기대하는 client_state 송신 시한(초)


class NAIAWebSocketClient:
    """host:port 조합 하나당 인스턴스 1개 (싱글턴)."""

    _instances: "dict[tuple[str, int], NAIAWebSocketClient]" = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get(cls, host: str, port: int) -> "NAIAWebSocketClient":
        """싱글턴 인스턴스 반환. 처음 호출 시 백그라운드 스레드 기동."""
        key = (host, int(port))
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(host, int(port))
                cls._instances[key] = inst
                inst.start()
        return inst

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self.url = f"ws://{host}:{port}/ws"
        self._lock = threading.Lock()
        self._stop = False

        # 캐시
        self._prompt: str = ""
        self._negative: str = ""
        self._module_states: "dict[str, dict]" = {}  # module_id → state dict
        self._module_state_ts: "dict[str, float]" = {}  # module_id → last update epoch
        self._connected: bool = False
        self._last_prompt_update: float = 0.0
        self._connected_at: float = 0.0  # 연결 성공 시각 (초기 대기용)

        # 송신 큐 (연결 전 보낸 메시지를 보관 — 연결되면 일괄 송신)
        self._pending_outbox: list[str] = []
        self._ws = None  # 현재 활성 websocket (송신용)
        self._thread: Optional[threading.Thread] = None

    # =====================================================================
    # 외부 API
    # =====================================================================
    def start(self) -> None:
        """백그라운드 스레드 기동 (이미 기동되어 있으면 no-op)."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_forever,
            name=f"NAIA-WS-{self.host}:{self.port}",
            daemon=True,
        )
        self._thread.start()

    def get_prompts(self) -> "tuple[str, str, bool, float]":
        """(prompt, negative_prompt, connected, last_update_epoch)."""
        with self._lock:
            return (
                self._prompt,
                self._negative,
                self._connected,
                self._last_prompt_update,
            )

    def get_module_state(self, module_id: str) -> Optional[dict]:
        """module_state 캐시 반환 (없으면 None). 호출자에게 dict 사본을 넘김."""
        with self._lock:
            state = self._module_states.get(module_id)
            return dict(state) if state else None

    def request_module_state(self, module_id: str) -> None:
        """서버에 모듈 상태 갱신 요청. 응답은 비동기로 캐시에 반영됨."""
        self._send_json({"type": "get_module_state", "module_id": module_id})

    def wait_for_module_state(
        self, module_id: str, timeout: float = 5.0
    ) -> Optional[dict]:
        """모듈 상태를 서버에 요청하고 새 응답이 캐시에 도착할 때까지 대기.

        반환: 새 state dict (사본) 또는 None (타임아웃).
        """
        with self._lock:
            before_ts = self._module_state_ts.get(module_id, 0.0)
        self.request_module_state(module_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                cur_ts = self._module_state_ts.get(module_id, 0.0)
                if cur_ts > before_ts:
                    state = self._module_states.get(module_id)
                    return dict(state) if state else None
            time.sleep(0.05)
        return None

    def set_module_param(self, module_id: str, key: str, value: Any) -> None:
        """모듈 파라미터 설정. value는 문자열로 강제 변환 (NAIA 서버 규약).

        Phase 1에서는 직접 사용 X. Phase 3 쓰기 노드에서 활용.
        """
        if isinstance(value, bool):
            value_str = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            value_str = json.dumps(value, ensure_ascii=False)
        else:
            value_str = str(value)
        self._send_json({
            "type": "set_module_param",
            "module_id": module_id,
            "key": key,
            "value": value_str,
        })

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def wait_connected(self, timeout: float = 3.0) -> bool:
        """최대 `timeout` 초 동안 WS 연결 성공 + prompt_sync 1회 수신을 대기.

        반환: True=조건 충족, False=타임아웃.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._connected and self._last_prompt_update > 0:
                    return True
            time.sleep(0.05)
        return False

    # =====================================================================
    # 내부: 송신 / 수신 / 재연결 루프
    # =====================================================================
    def _send_json(self, payload: dict) -> None:
        """연결되어 있으면 즉시 송신, 아니면 outbox에 큐잉."""
        text = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            ws = self._ws if self._connected else None
            if ws is None:
                self._pending_outbox.append(text)
                return
        # 실제 send는 lock 밖 (blocking I/O 회피)
        try:
            ws.send(text)
        except Exception as e:
            logger.warning("send failed, requeue: %s", e)
            with self._lock:
                self._pending_outbox.append(text)

    def _flush_outbox(self, ws) -> None:
        """연결 직후 누적 메시지 일괄 송신. 실패 시 남은 메시지 전체 재큐잉."""
        with self._lock:
            queued = self._pending_outbox[:]
            self._pending_outbox.clear()
        for i, msg in enumerate(queued):
            try:
                ws.send(msg)
            except Exception as e:
                # 실패 시점 이후 모든 메시지를 큐 앞으로 되돌림 (기존 버그 수정)
                logger.warning("outbox flush failed at idx %d: %s", i, e)
                with self._lock:
                    self._pending_outbox = queued[i:] + self._pending_outbox
                return

    def _run_forever(self) -> None:
        """재연결 루프. stop 신호 전까지 연결 유지."""
        try:
            import websocket  # websocket-client
        except ImportError:
            logger.error(
                "websocket-client 미설치. ComfyUI-Manager의 requirements 자동 설치 또는 "
                "`pip install websocket-client` 로 설치하세요."
            )
            return

        while not self._stop:
            ws = None
            try:
                ws = websocket.WebSocket()
                ws.settimeout(WS_RECV_TIMEOUT)
                ws.connect(self.url)
                with self._lock:
                    self._ws = ws
                    self._connected = True
                    self._connected_at = time.time()
                logger.info("connected: %s", self.url)

                # NAIA 서버는 연결 직후 client_state 메시지를 기대 (3초 타임아웃)
                ws.send(json.dumps({"type": "client_state", "history_count": 0}))
                self._flush_outbox(ws)

                while not self._stop:
                    try:
                        raw = ws.recv()
                    except Exception:
                        # recv 타임아웃 또는 끊김 → 재연결
                        break
                    if raw is None or raw == "":
                        break
                    if isinstance(raw, bytes):
                        continue  # 이미지 바이너리 등 무시
                    self._handle_message(raw)
            except Exception as e:
                logger.info("connection error: %s", e)
            finally:
                with self._lock:
                    self._connected = False
                    self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if not self._stop:
                    time.sleep(WS_RECONNECT_DELAY)

    def _handle_message(self, raw: str) -> None:
        """서버 수신 메시지 디스패치."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "prompt_sync":
            with self._lock:
                self._prompt = msg.get("prompt", "") or ""
                self._negative = msg.get("negative_prompt", "") or ""
                self._last_prompt_update = time.time()
        elif mtype == "module_state":
            mid = msg.get("module_id")
            if mid:
                with self._lock:
                    self._module_states[mid] = dict(msg)
                    self._module_state_ts[mid] = time.time()
