# AI Pizza Ordering Agent - Pipecat + Plivo + Deepgram + Gemini + ElevenLabs

An AI voice agent that calls pizza restaurants and places orders autonomously using phone calls.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Framework** | [Pipecat](https://github.com/pipecat-ai/pipecat) |
| **STT** | Deepgram Nova-3 |
| **TTS** | ElevenLabs (Turbo v2.5) |
| **LLM** | Google Gemini 2.5 Flash |
| **Telephony** | Plivo |
| **Web Server** | FastAPI + Uvicorn |

## Features

- Natural voice conversations with restaurant staff
- IVR/voicemail detection with DTMF navigation (presses buttons on automated phone systems)
- Listen-in mode — get called first so you can hear the conversation live
- Special instructions — tell the agent things like "pick a drink for me"
- Real-time order status tracking via web UI
- Call recording playback

## Setup

1. Clone the repo and install dependencies:

```bash
uv sync
```

2. Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

3. Start the server:

```bash
uv run uvicorn outbound.server:app --host 0.0.0.0 --port 7860
```

4. Expose the server with ngrok:

```bash
ngrok http 7860
```

5. Update `PUBLIC_HOST` in `.env` with your ngrok domain (without `https://`).

6. Open `http://localhost:7860` in your browser and place an order.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DEEPGRAM_API_KEY` | Deepgram API key (STT) |
| `ELEVENLABS_API_KEY` | ElevenLabs API key (TTS) |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice ID |
| `GOOGLE_API_KEY` | Google Gemini API key |
| `PLIVO_AUTH_ID` | Plivo Auth ID |
| `PLIVO_AUTH_TOKEN` | Plivo Auth Token |
| `PLIVO_PHONE_NUMBER` | Plivo phone number (E.164 format) |
| `GOOGLE_PLACES_API_KEY` | Google Places API key (restaurant lookup) |
| `PUBLIC_HOST` | Public hostname for webhooks (ngrok domain) |

## Architecture

```
Web UI → FastAPI Server → Plivo (phone call)
                              ↓
                     Pipecat Pipeline:
                     Deepgram STT → Gemini LLM → ElevenLabs TTS
                              ↓
                     Restaurant picks up and talks to the agent
```

## Deployment

Includes `Dockerfile` and `railway.toml` for deployment on Railway.
