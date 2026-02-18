"""Audio utilities — mulaw mixing and TeeWebSocket for listen-in feature."""

import asyncio
import base64
import json

from fastapi import WebSocket
from loguru import logger


# ---------------------------------------------------------------------------
# Mulaw codec — decode/encode μ-law for real-time audio mixing
# ---------------------------------------------------------------------------

# Decode table: mulaw byte → 16-bit linear PCM
_ULAW_DECODE = []
for _i in range(256):
    _u = ~_i & 0xFF
    _sign = _u & 0x80
    _exp = (_u >> 4) & 0x07
    _man = _u & 0x0F
    _s = ((_man << 3) + 0x84) << _exp
    _s -= 0x84
    _ULAW_DECODE.append(-_s if _sign else _s)


def _linear_to_ulaw(pcm: int) -> int:
    """Encode a single 16-bit PCM sample to a mulaw byte."""
    BIAS = 0x84
    sign = 0
    if pcm < 0:
        sign = 0x80
        pcm = -pcm
    pcm = min(pcm, 0x7FFF) + BIAS
    exp = 7
    mask = 0x4000
    while exp > 0 and not (pcm & mask):
        exp -= 1
        mask >>= 1
    mantissa = (pcm >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mantissa) & 0xFF


def mix_mulaw(a: bytes, b: bytes) -> bytes:
    """Mix two mulaw buffers sample-by-sample into one."""
    la, lb = len(a), len(b)
    n = max(la, lb)
    out = bytearray(n)
    for i in range(n):
        sa = _ULAW_DECODE[a[i]] if i < la else 0
        sb = _ULAW_DECODE[b[i]] if i < lb else 0
        out[i] = _linear_to_ulaw(max(-32768, min(32767, sa + sb)))
    return bytes(out)


# ---------------------------------------------------------------------------
# TeeWebSocket — wraps a real WebSocket and copies all audio to a listener
# ---------------------------------------------------------------------------

# How often the sender loop fires (ms). At 8000 Hz, 100 ms = 800 samples.
TICK_MS = 100
TICK_SAMPLES = 8000 * TICK_MS // 1000  # 800 bytes of mulaw per tick


class TeeWebSocket:
    """Proxy that quacks like a Starlette WebSocket.

    Pipecat's FastAPIWebsocketTransport calls receive(), send_text(),
    send_bytes(), close(), and reads client_state / application_state.

    Audio from the restaurant (inbound) and the agent (outbound TTS) is
    buffered separately. A fixed-rate sender loop mixes them into one
    stream and sends exactly 1x real-time audio to the listener, preventing
    the slow-mo caused by sending 2x data.
    """

    def __init__(self, real_ws: WebSocket, listener_ws: WebSocket, listener_stream_id: str):
        self._ws = real_ws
        self._listener_ws = listener_ws
        self._listener_stream_id = listener_stream_id
        self._listener_alive = True
        # Separate buffers for each direction (raw mulaw bytes)
        self._inbound_buf = bytearray()   # restaurant voice
        self._outbound_buf = bytearray()  # agent TTS
        self._sender_task = asyncio.create_task(self._sender_loop())

    # -- properties Pipecat reads ------------------------------------------

    @property
    def client_state(self):
        return self._ws.client_state

    @property
    def application_state(self):
        return self._ws.application_state

    # -- receive (inbound from restaurant) ---------------------------------

    async def receive(self):
        data = await self._ws.receive()
        if self._listener_alive and data.get("text"):
            try:
                msg = json.loads(data["text"])
                if msg.get("event") == "media":
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        self._inbound_buf.extend(base64.b64decode(payload))
            except Exception:
                pass
        return data

    async def receive_text(self):
        text = await self._ws.receive_text()
        if self._listener_alive:
            try:
                msg = json.loads(text)
                if msg.get("event") == "media":
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        self._inbound_buf.extend(base64.b64decode(payload))
            except Exception:
                pass
        return text

    # -- send (outbound to restaurant, i.e. agent TTS) ---------------------

    async def send_text(self, text: str):
        await self._ws.send_text(text)
        if self._listener_alive:
            try:
                msg = json.loads(text)
                if msg.get("event") == "playAudio":
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        self._outbound_buf.extend(base64.b64decode(payload))
            except Exception:
                pass

    async def send_bytes(self, data: bytes):
        await self._ws.send_bytes(data)

    async def close(self, code: int = 1000, reason: str | None = None):
        if self._sender_task:
            self._sender_task.cancel()
        await self._ws.close(code=code, reason=reason)

    # -- internal: fixed-rate mixer + sender -------------------------------

    async def _sender_loop(self):
        """Every TICK_MS, take up to TICK_SAMPLES from each buffer, mix, send."""
        try:
            while True:
                await asyncio.sleep(TICK_MS / 1000)
                if not self._listener_alive:
                    break

                # Drain up to one tick's worth from each buffer
                in_chunk = bytes(self._inbound_buf[:TICK_SAMPLES])
                del self._inbound_buf[:TICK_SAMPLES]
                out_chunk = bytes(self._outbound_buf[:TICK_SAMPLES])
                del self._outbound_buf[:TICK_SAMPLES]

                if not in_chunk and not out_chunk:
                    continue

                # Mix both directions into one stream (or pass through single)
                if in_chunk and out_chunk:
                    mixed = mix_mulaw(in_chunk, out_chunk)
                else:
                    mixed = in_chunk or out_chunk

                payload_b64 = base64.b64encode(mixed).decode("utf-8")
                await self._listener_ws.send_text(json.dumps({
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": payload_b64,
                    },
                    "streamId": self._listener_stream_id,
                }))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Listener sender loop failed: {e}")
            self._listener_alive = False
