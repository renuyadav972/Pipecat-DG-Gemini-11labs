"""Pipecat voice agent pipeline for outbound pizza ordering calls."""

import os

from typing import Callable

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

# Tool: transfer the call to the customer for card details etc.
transfer_to_customer_tool = FunctionSchema(
    name="transfer_to_customer",
    description=(
        "Transfer the call to the customer so they can speak directly to the restaurant. "
        "Use this when the restaurant asks for credit card details or other sensitive "
        "information that only the customer can provide."
    ),
    properties={},
    required=[],
)


async def run_bot(
    websocket,
    stream_id: str,
    call_id: str,
    system_prompt: str,
    order_type: str = "pickup",
    on_transfer: Callable | None = None,
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
        tools=[transfer_to_customer_tool] if on_transfer else [],
    )

    # Register transfer handler — bridges customer audio into the call
    if on_transfer:
        async def handle_transfer(params: FunctionCallParams):
            logger.info(f"Transfer to customer requested (call_id={call_id})")
            try:
                await on_transfer()
                result = {
                    "status": "transferred",
                    "message": (
                        "The customer is now speaking directly to the restaurant. "
                        "IMPORTANT: Stay completely silent. Do not say anything "
                        "for the rest of the call."
                    ),
                }
            except Exception as e:
                result = {"status": "error", "message": str(e)}
                logger.error(f"Transfer failed: {e}")
            await params.result_callback(result)

        llm.register_function(
            function_name="transfer_to_customer",
            handler=handle_transfer,
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
