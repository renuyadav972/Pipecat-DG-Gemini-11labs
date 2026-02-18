"""FastAPI server for outbound pizza ordering calls with listen-in support."""

import asyncio
import json
import os
import ssl
import uuid
from pathlib import Path

import certifi

# Fix macOS SSL certificate issue globally — must be before any other imports
# that open SSL connections (Deepgram, ElevenLabs, Google, etc.)
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import aiohttp
import plivo
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from outbound.agent import run_bot
from restaurant_lookup import normalize_phone_number, search_restaurant
from utils import TeeWebSocket

load_dotenv()

# Load system prompt template from markdown file
_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
_SYSTEM_PROMPT_TEMPLATE = _PROMPT_PATH.read_text()

# Resolve static directory relative to project root
_BASE_DIR = Path(__file__).resolve().parent.parent
_STATIC_DIR = _BASE_DIR / "static"

app = FastAPI(title="Pizza Ordering Agent")

# In-memory order tracking
orders: dict = {}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Start order
# ---------------------------------------------------------------------------


@app.post("/start-order")
async def start_order(request: Request):
    """Accept order details, find restaurant, initiate calls."""
    body = await request.json()

    restaurant_query = body.get("restaurant_query", "")
    order_items = body.get("order_items", "")
    payment_method = body.get("payment_method", "cash")
    customer_name = body.get("customer_name", "Alex")
    order_type = body.get("order_type", "pickup")
    delivery_address = body.get("delivery_address", "")
    phone_override = body.get("phone_override", "")
    user_phone = body.get("user_phone", "")
    special_instructions = body.get("special_instructions", "None")

    if not restaurant_query or not order_items:
        return JSONResponse(
            status_code=400,
            content={"error": "restaurant_query and order_items are required"},
        )

    order_id = str(uuid.uuid4())[:8]
    orders[order_id] = {
        "status": "searching",
        "restaurant": None,
        "recording_url": None,
        "listener_recording_url": None,
        "call_uuid": None,
        "order_type": order_type,
        "user_phone": user_phone,
        "listener_call_uuid": None,
        "listener_ws": None,
        "listener_stream_id": None,
    }

    # --- Resolve restaurant phone number ---
    if phone_override:
        restaurant_name = restaurant_query or "Restaurant"
        phone_number = normalize_phone_number(phone_override)
        orders[order_id]["restaurant"] = {
            "name": restaurant_name,
            "address": "",
            "phone_number": phone_number,
        }
    else:
        try:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx)
            ) as session:
                restaurant = await search_restaurant(
                    session,
                    restaurant_query,
                    os.getenv("GOOGLE_PLACES_API_KEY", ""),
                )
        except Exception as e:
            orders[order_id]["status"] = "error"
            logger.error(f"Restaurant search failed: {e}")
            return JSONResponse(
                status_code=500,
                content={"error": f"Restaurant search failed: {str(e)}"},
            )

        if not restaurant:
            orders[order_id]["status"] = "error"
            return JSONResponse(
                status_code=404,
                content={"error": "No restaurant found with a phone number"},
            )

        orders[order_id]["restaurant"] = {
            "name": restaurant.name,
            "address": restaurant.address,
            "phone_number": restaurant.phone_number,
        }

    # --- Build system prompt ---
    restaurant_name = orders[order_id]["restaurant"]["name"]
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        restaurant_name=restaurant_name,
        order_items=order_items,
        payment_method=payment_method,
        customer_name=customer_name,
        order_type=order_type,
        delivery_address=delivery_address,
        special_instructions=special_instructions,
    )
    orders[order_id]["system_prompt"] = system_prompt

    public_host = os.getenv("PUBLIC_HOST", "localhost:7860")
    plivo_auth_id = os.getenv("PLIVO_AUTH_ID", "")
    plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN", "")
    plivo_phone = os.getenv("PLIVO_PHONE_NUMBER", "")
    client = plivo.RestClient(plivo_auth_id, plivo_auth_token)

    # --- Listen-in flow: call the user first ---
    if user_phone:
        user_phone_norm = normalize_phone_number(user_phone)
        orders[order_id]["status"] = "calling_listener"

        try:
            call = client.calls.create(
                from_=plivo_phone,
                to_=user_phone_norm,
                answer_url=f"https://{public_host}/plivo/answer-listener?order_id={order_id}",
                answer_method="GET",
                hangup_url=f"https://{public_host}/plivo/hangup-listener?order_id={order_id}",
                hangup_method="POST",
            )
            orders[order_id]["listener_call_uuid"] = call["request_uuid"]
            logger.info(
                f"Listener call initiated: {call['request_uuid']} to {user_phone_norm}"
            )
        except Exception as e:
            orders[order_id]["status"] = "error"
            logger.error(f"Listener call failed: {e}")
            return JSONResponse(
                status_code=500,
                content={"error": f"Failed to call listener: {str(e)}"},
            )

        return JSONResponse(
            content={
                "order_id": order_id,
                "restaurant": orders[order_id]["restaurant"],
                "status": "calling_listener",
            }
        )

    # --- Direct flow: call restaurant immediately ---
    orders[order_id]["status"] = "calling"

    try:
        call = client.calls.create(
            from_=plivo_phone,
            to_=orders[order_id]["restaurant"]["phone_number"],
            answer_url=f"https://{public_host}/plivo/answer?order_id={order_id}",
            answer_method="GET",
            hangup_url=f"https://{public_host}/plivo/hangup?order_id={order_id}",
            hangup_method="POST",
        )
        orders[order_id]["call_uuid"] = call["request_uuid"]
        logger.info(
            f"Call initiated: {call['request_uuid']} to {orders[order_id]['restaurant']['phone_number']}"
        )
    except Exception as e:
        orders[order_id]["status"] = "error"
        logger.error(f"Plivo call failed: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to initiate call: {str(e)}"},
        )

    return JSONResponse(
        content={
            "order_id": order_id,
            "restaurant": orders[order_id]["restaurant"],
            "status": "calling",
        }
    )


# ---------------------------------------------------------------------------
# Listener endpoints (listen-in flow)
# ---------------------------------------------------------------------------


@app.get("/plivo/answer-listener")
async def plivo_answer_listener(request: Request):
    """Plivo answer webhook for the listener (user) call."""
    order_id = request.query_params.get("order_id", "")
    call_uuid = request.query_params.get("CallUUID", "")
    public_host = os.getenv("PUBLIC_HOST", "localhost:7860")

    logger.info(f"Listener answered for order {order_id}, CallUUID: {call_uuid}")

    if order_id in orders:
        orders[order_id]["listener_call_uuid"] = call_uuid

    # Start recording the listener call
    try:
        plivo_auth_id = os.getenv("PLIVO_AUTH_ID", "")
        plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN", "")
        client = plivo.RestClient(plivo_auth_id, plivo_auth_token)
        client.calls.start_recording(
            call_uuid=call_uuid,
            time_limit=600,
            callback_url=f"https://{public_host}/plivo/recording-callback-listener?order_id={order_id}",
            callback_method="POST",
        )
        logger.info(f"Listener recording started for call {call_uuid}")
    except Exception as e:
        logger.error(f"Failed to start listener recording: {e}")

    ws_url = f"wss://{public_host}/plivo/ws-listener?order_id={order_id}"

    response = plivo.plivoxml.ResponseElement()
    response.add(
        plivo.plivoxml.SpeakElement(
            "Connecting you as a listener. You will hear the conversation shortly."
        )
    )
    response.add(
        plivo.plivoxml.StreamElement(
            content=ws_url,
            bidirectional=True,
            keepCallAlive=True,
            contentType="audio/x-mulaw;rate=8000",
        )
    )

    xml_response = response.to_string()
    logger.info(f"Listener answer XML: {xml_response}")
    return Response(content=xml_response, media_type="application/xml")


@app.websocket("/plivo/ws-listener")
async def plivo_ws_listener(websocket: WebSocket):
    """Handle listener's Plivo WebSocket — stores it and triggers restaurant call."""
    order_id = websocket.query_params.get("order_id", "")
    logger.info(f"Listener WebSocket connection for order {order_id}")

    await websocket.accept()

    # Read the initial start event
    try:
        initial_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        data = json.loads(initial_msg)
        logger.info(f"Listener WS start event: {data}")

        if data.get("event") == "start":
            stream_id = data.get("start", {}).get("streamId", "")
        else:
            stream_id = data.get("streamId", str(uuid.uuid4())[:8])
    except asyncio.TimeoutError:
        logger.error("Timeout waiting for listener start event")
        await websocket.close()
        return
    except Exception as e:
        logger.error(f"Error reading listener start message: {e}")
        await websocket.close()
        return

    logger.info(f"Listener stream started: stream_id={stream_id}")

    # Store listener WS in order state
    if order_id in orders:
        orders[order_id]["listener_ws"] = websocket
        orders[order_id]["listener_stream_id"] = stream_id
        orders[order_id]["status"] = "listener_connected"

    # Trigger the restaurant call in the background
    asyncio.create_task(_call_restaurant(order_id))

    # Keep connection alive — receive and discard until closed
    try:
        while True:
            await websocket.receive()
    except Exception:
        pass
    finally:
        if order_id in orders:
            orders[order_id].pop("listener_ws", None)
        logger.info(f"Listener WebSocket closed for order {order_id}")


async def _call_restaurant(order_id: str):
    """Initiate the Plivo call to the restaurant (called after listener connects)."""
    order = orders.get(order_id)
    if not order:
        return

    restaurant_phone = order["restaurant"]["phone_number"]
    public_host = os.getenv("PUBLIC_HOST", "localhost:7860")
    plivo_auth_id = os.getenv("PLIVO_AUTH_ID", "")
    plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN", "")
    plivo_phone = os.getenv("PLIVO_PHONE_NUMBER", "")

    client = plivo.RestClient(plivo_auth_id, plivo_auth_token)

    try:
        order["status"] = "calling"
        call = client.calls.create(
            from_=plivo_phone,
            to_=restaurant_phone,
            answer_url=f"https://{public_host}/plivo/answer?order_id={order_id}",
            answer_method="GET",
            hangup_url=f"https://{public_host}/plivo/hangup?order_id={order_id}",
            hangup_method="POST",
        )
        order["call_uuid"] = call["request_uuid"]
        logger.info(
            f"Restaurant call initiated: {call['request_uuid']} to {restaurant_phone}"
        )
    except Exception as e:
        order["status"] = "error"
        logger.error(f"Restaurant call failed: {e}")


@app.post("/plivo/hangup-listener")
async def plivo_hangup_listener(request: Request):
    """Plivo hangup webhook for the listener call."""
    order_id = request.query_params.get("order_id", "")
    logger.info(f"Listener hung up for order {order_id}")

    if order_id in orders:
        orders[order_id].pop("listener_ws", None)
        orders[order_id]["listener_call_uuid"] = None

    return JSONResponse(content={"status": "ok"})


@app.post("/plivo/recording-callback-listener")
async def plivo_recording_callback_listener(request: Request):
    """Plivo recording callback for the listener call."""
    order_id = request.query_params.get("order_id", "")
    recording_url = ""
    try:
        form_data = await request.form()
        form_dict = dict(form_data)
        logger.info(f"Listener recording callback: {form_dict}")
        recording_url = form_dict.get("RecordUrl", "") or form_dict.get("RecordingUrl", "") or form_dict.get("record_url", "")
        if not recording_url and "response" in form_dict:
            try:
                nested = json.loads(form_dict["response"])
                recording_url = nested.get("record_url", "") or nested.get("RecordUrl", "") or nested.get("recording_url", "")
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        pass
    if not recording_url:
        try:
            body = await request.json()
            recording_url = body.get("RecordUrl", "") or body.get("RecordingUrl", "") or body.get("record_url", "")
            if not recording_url and "response" in body:
                try:
                    nested = json.loads(body["response"]) if isinstance(body["response"], str) else body["response"]
                    recording_url = nested.get("record_url", "") or nested.get("RecordUrl", "") or nested.get("recording_url", "")
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass

    logger.info(f"Listener recording for order {order_id}: url={recording_url}")
    if order_id in orders and recording_url:
        orders[order_id]["listener_recording_url"] = recording_url
    return JSONResponse(content={"status": "ok"})


# ---------------------------------------------------------------------------
# Restaurant call endpoints (with listen-in support)
# ---------------------------------------------------------------------------


@app.get("/plivo/answer")
async def plivo_answer(request: Request):
    """Plivo answer webhook — returns Stream XML to connect WebSocket."""
    order_id = request.query_params.get("order_id", "")
    call_uuid = request.query_params.get("CallUUID", "")
    public_host = os.getenv("PUBLIC_HOST", "localhost:7860")

    logger.info(f"Call answered for order {order_id}, CallUUID: {call_uuid}")

    if order_id in orders:
        orders[order_id]["status"] = "in_progress"
        orders[order_id]["call_uuid"] = call_uuid

    # Start recording via Plivo API
    try:
        plivo_auth_id = os.getenv("PLIVO_AUTH_ID", "")
        plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN", "")
        client = plivo.RestClient(plivo_auth_id, plivo_auth_token)
        client.calls.start_recording(
            call_uuid=call_uuid,
            time_limit=600,
            callback_url=f"https://{public_host}/plivo/recording-callback?order_id={order_id}",
            callback_method="POST",
        )
        logger.info(f"Recording started for call {call_uuid}")
    except Exception as e:
        logger.error(f"Failed to start recording: {e}")

    # Build Plivo XML response with bidirectional Stream
    ws_url = f"wss://{public_host}/plivo/ws?order_id={order_id}"
    response = plivo.plivoxml.ResponseElement()
    response.add(
        plivo.plivoxml.StreamElement(
            content=ws_url,
            bidirectional=True,
            keepCallAlive=True,
            contentType="audio/x-mulaw;rate=8000",
        )
    )

    xml_response = response.to_string()
    logger.info(f"Answer XML: {xml_response}")
    return Response(content=xml_response, media_type="application/xml")


@app.websocket("/plivo/ws")
async def plivo_websocket(websocket: WebSocket):
    """Handle Plivo bidirectional audio WebSocket."""
    order_id = websocket.query_params.get("order_id", "")
    logger.info(f"WebSocket connection request for order {order_id}")

    await websocket.accept()

    # Read the initial 'start' event from Plivo to get stream_id and call_id
    try:
        initial_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        data = json.loads(initial_msg)
        logger.info(f"Plivo WS start event: {data}")

        if data.get("event") == "start":
            stream_id = data.get("start", {}).get("streamId", "")
            call_id = data.get("start", {}).get("callId", "")
        else:
            stream_id = data.get("streamId", str(uuid.uuid4())[:8])
            call_id = data.get("callId", "")
    except asyncio.TimeoutError:
        logger.error("Timeout waiting for Plivo start event")
        await websocket.close()
        return
    except Exception as e:
        logger.error(f"Error reading initial WS message: {e}")
        await websocket.close()
        return

    logger.info(f"Stream started: stream_id={stream_id}, call_id={call_id}")

    # Wrap with TeeWebSocket if a listener is connected
    order_data = orders.get(order_id, {})
    listener_ws = order_data.get("listener_ws")
    listener_stream_id = order_data.get("listener_stream_id", "")
    if listener_ws:
        logger.info(f"Wrapping WebSocket with TeeWebSocket for listener (stream_id={listener_stream_id})")
        ws_for_bot = TeeWebSocket(websocket, listener_ws, listener_stream_id)
    else:
        ws_for_bot = websocket

    system_prompt = order_data.get("system_prompt", "You are a helpful assistant.")
    order_type = order_data.get("order_type", "pickup")

    # Run the Pipecat bot pipeline
    try:
        await run_bot(
            websocket=ws_for_bot,
            stream_id=stream_id,
            call_id=call_id,
            system_prompt=system_prompt,
            order_type=order_type,
        )
    except Exception as e:
        logger.error(f"Bot pipeline error: {e}")
    finally:
        if order_id in orders:
            orders[order_id]["status"] = "completed"
        logger.info(f"WebSocket closed for order {order_id}")


@app.post("/plivo/hangup")
async def plivo_hangup(request: Request):
    """Plivo hangup webhook — update order status and end listener call."""
    order_id = request.query_params.get("order_id", "")
    if order_id in orders:
        orders[order_id]["status"] = "completed"

        # Also hang up the listener call if active
        listener_uuid = orders[order_id].get("listener_call_uuid")
        if listener_uuid:
            try:
                plivo_auth_id = os.getenv("PLIVO_AUTH_ID", "")
                plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN", "")
                client = plivo.RestClient(plivo_auth_id, plivo_auth_token)
                client.calls.hangup(listener_uuid)
                logger.info(f"Listener call {listener_uuid} hung up")
            except Exception as e:
                logger.warning(f"Failed to hang up listener call: {e}")

    logger.info(f"Call hung up for order {order_id}")
    return JSONResponse(content={"status": "ok"})


# ---------------------------------------------------------------------------
# Recording + order status
# ---------------------------------------------------------------------------


def _extract_recording_url(form_dict: dict) -> str:
    """Extract recording URL from Plivo callback data."""
    url = form_dict.get("RecordUrl", "") or form_dict.get("RecordingUrl", "") or form_dict.get("record_url", "")
    if not url and "response" in form_dict:
        try:
            nested = json.loads(form_dict["response"]) if isinstance(form_dict["response"], str) else form_dict["response"]
            url = nested.get("record_url", "") or nested.get("RecordUrl", "") or nested.get("recording_url", "")
        except (json.JSONDecodeError, TypeError):
            pass
    return url


@app.post("/plivo/recording-callback")
async def plivo_recording_callback(request: Request):
    """Plivo recording callback — store the recording URL."""
    order_id = request.query_params.get("order_id", "")
    recording_url = ""

    try:
        form_data = await request.form()
        form_dict = dict(form_data)
        logger.info(f"Recording callback form data: {form_dict}")
        recording_url = _extract_recording_url(form_dict)
    except Exception:
        pass

    if not recording_url:
        try:
            body = await request.json()
            logger.info(f"Recording callback JSON body: {body}")
            recording_url = _extract_recording_url(body)
        except Exception:
            pass

    logger.info(f"Recording callback for order {order_id}: recording_url={recording_url}")

    if order_id in orders and recording_url:
        orders[order_id]["recording_url"] = recording_url

    return JSONResponse(content={"status": "ok"})


@app.get("/recording/{order_id}")
async def get_recording(order_id: str):
    """Return the recording URLs for an order."""
    if order_id not in orders:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    recording_url = orders[order_id].get("recording_url")
    listener_recording_url = orders[order_id].get("listener_recording_url")
    if not recording_url and not listener_recording_url:
        return JSONResponse(status_code=404, content={"error": "Recording not available yet"})
    return JSONResponse(content={
        "recording_url": recording_url,
        "listener_recording_url": listener_recording_url,
    })


@app.get("/order/{order_id}")
async def get_order(order_id: str):
    """Return the current status of an order."""
    if order_id not in orders:
        return JSONResponse(status_code=404, content={"error": "Order not found"})
    order = orders[order_id]
    return JSONResponse(content={
        "order_id": order_id,
        "status": order["status"],
        "restaurant": order.get("restaurant"),
        "recording_url": order.get("recording_url"),
        "listener_recording_url": order.get("listener_recording_url"),
    })


# Serve static files (web UI) — must be last so it doesn't override API routes
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
