"""Pipecat voice agent pipeline for outbound pizza ordering calls."""

import os

import plivo
from deepgram import LiveOptions
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

# DTMF tool definition for Gemini function calling
send_dtmf_tool = FunctionSchema(
    name="send_dtmf_digits",
    description="Press phone keypad buttons (DTMF tones) during the call. Use this when an automated system or voicemail asks you to press a number, like 'press 1 for ordering' or 'press 0 for an operator'.",
    properties={
        "digits": {
            "type": "string",
            "description": "The digit(s) to press. Can be 0-9, *, or #. Examples: '1', '0', '2'",
        },
    },
    required=["digits"],
)


async def run_bot(
    websocket,
    stream_id: str,
    call_id: str,
    system_prompt: str,
    order_type: str = "pickup",
):
    """Create and run the Pipecat pipeline for a phone call."""

    plivo_auth_id = os.getenv("PLIVO_AUTH_ID", "")
    plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN", "")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_channels=1,
            add_wav_header=False,
            serializer=PlivoFrameSerializer(
                stream_id=stream_id,
                call_id=call_id,
                auth_id=plivo_auth_id,
                auth_token=plivo_auth_token,
                params=PlivoFrameSerializer.InputParams(
                    plivo_sample_rate=8000,
                    auto_hang_up=True,
                ),
            ),
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.7,
                    start_secs=0.3,
                    stop_secs=2.0,
                    min_volume=0.5,
                ),
            ),
        ),
    )

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        live_options=LiveOptions(
            encoding="linear16",
            language="en",
            model="nova-3-general",
            channels=1,
            interim_results=True,
            smart_format=False,
            punctuate=True,
        ),
    )

    llm = GoogleLLMService(
        api_key=os.getenv("GOOGLE_API_KEY", ""),
        model="gemini-2.5-flash",
        system_instruction=system_prompt,
        tools=[send_dtmf_tool],
    )

    # Register DTMF function handler
    async def handle_send_dtmf(params: FunctionCallParams):
        digits = params.arguments.get("digits", "")
        logger.info(f"Sending DTMF digits: {digits} (call_id={call_id})")
        try:
            client = plivo.RestClient(plivo_auth_id, plivo_auth_token)
            client.calls.send_digits(call_uuid=call_id, digits=digits, leg="aleg")
            result = {"status": "sent", "digits": digits}
            logger.info(f"DTMF sent successfully: {digits}")
        except Exception as e:
            result = {"status": "error", "message": str(e)}
            logger.error(f"Failed to send DTMF: {e}")
        await params.result_callback(result)

    llm.register_function(
        function_name="send_dtmf_digits",
        handler=handle_send_dtmf,
        cancel_on_interruption=False,
    )

    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        sample_rate=8000,
        model="eleven_turbo_v2_5",
    )

    context = OpenAILLMContext(
        messages=[{"role": "system", "content": system_prompt}],
    )
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )

    @task.event_handler("on_pipeline_started")
    async def on_pipeline_started(task, frame):
        logger.info("Pipeline started — agent is live on the call")

    @task.event_handler("on_pipeline_finished")
    async def on_pipeline_finished(task, frame):
        logger.info("Pipeline finished — call ended")

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
