import hmac
# python3 -m venv venv
# source venv/bin/activate
# pip install python-dotenv python-multipart Jinja2 modal fastapi uvicorn starlette, sqlalchemy, sqlalchemy_utils
# modal deploy server_modal.py

# # # # # # # # # # # # 
# # server_modal.py # #
# # # # # # # # # # # # 
import os
import asyncio
import uuid
import logging
from dotenv import load_dotenv
from typing import Dict, Any
from pathlib import Path
import modal
from fastapi import FastAPI, Request, Form, logger
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy_utils import database_exists, create_database
from models import Base, User
from LovenseClient import LovenseClient
from nomi_client import NomiClient
from command_parser import parse_nomi_response, parse_nomi_commands
load_dotenv()

# Device sequence safety limits
MAX_SEQUENCE_COMMANDS = 5
MAX_SEQUENCE_COMMAND_SECONDS = 60
MAX_SEQUENCE_TOTAL_SECONDS = 120


def validate_device_sequence(commands):
    """Validate a parsed device command sequence before execution."""
    if not commands:
        return False, "Sequence is empty.", 0

    if len(commands) > MAX_SEQUENCE_COMMANDS:
        return False, f"Sequence exceeds {MAX_SEQUENCE_COMMANDS} commands.", 0

    total_seconds = 0

    for command in commands:
        action = command.get("action", "")
        duration = command.get("timeSec", 0)

        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return False, "Invalid command duration.", 0

        if action == "Stop":
            continue

        if not (action.startswith("Vibrate:") or action.startswith("Preset:")):
            return False, f"Unsupported device action: {action}", 0

        if duration < 2 or duration > MAX_SEQUENCE_COMMAND_SECONDS:
            return False, f"Invalid duration for {action}: {duration}", 0

        total_seconds += duration

    if total_seconds > MAX_SEQUENCE_TOTAL_SECONDS:
        return False, (
            f"Sequence duration {total_seconds}s exceeds "
            f"{MAX_SEQUENCE_TOTAL_SECONDS}s maximum."
        ), total_seconds

    return True, None, total_seconds


def lovense_response_succeeded(response):
    """Return True only when Lovense reports a successful command."""
    if not isinstance(response, dict):
        return False

    code = response.get("code")

    try:
        code = int(code)
    except (TypeError, ValueError):
        return False

    return code == 200


def require_lovense_success(response, action):
    """Raise when a Lovense command fails so the sequence stops."""
    if lovense_response_succeeded(response):
        return

    if isinstance(response, dict):
        code = response.get("code")
        message = response.get("message", "Unknown Lovense error")
    else:
        code = None
        message = str(response)

    raise RuntimeError(
        f"Lovense command failed for {action}: "
        f"code={code}, message={message}"
    )


async def execute_device_sequence(client, uid, toy_id, commands, simulate=False):
    """Execute validated commands sequentially, or simulate them safely."""
    valid, error, total_seconds = validate_device_sequence(commands)

    if not valid:
        raise ValueError(error)

    # Fail closed: the synthetic test device must never reach real Lovense execution.
    if not simulate and toy_id == "_test_":
        raise RuntimeError("Refusing real-device execution for test toy.")

    # Likewise, simulation is reserved for the synthetic test device.
    if simulate and toy_id != "_test_":
        raise RuntimeError("Refusing simulated execution for a real toy.")

    results = []

    # Test mode never contacts Lovense hardware.
    if simulate:
        for index, command in enumerate(commands):
            action = command["action"]
            duration = int(command["timeSec"])

            logger.info(
                f"TEST MODE sequence step {index + 1}: "
                f"action={action}, timeSec={duration}"
            )

            results.append({
                "action": action,
                "timeSec": duration,
                "simulated": True,
            })

            if action == "Stop":
                break

        return {
            "commands": results,
            "totalSeconds": total_seconds,
            "simulated": True,
        }

    # Real-device execution.
    try:
        for index, command in enumerate(commands):
            action = command["action"]
            duration = int(command["timeSec"])

            if action == "Stop":
                response = await client.control_toy_server(
                    uid,
                    action="Stop",
                    time_sec=0,
                    toy_id=toy_id
                )

                require_lovense_success(response, action)

                results.append({
                    "action": action,
                    "timeSec": 0,
                    "response": response,
                })

                break

            if action.startswith("Preset:"):
                preset_name = action.split(":", 1)[1]

                response = await client.send_preset(
                    uid,
                    name=preset_name,
                    time_sec=duration,
                    toy_id=toy_id
                )
            else:
                response = await client.control_toy_server(
                    uid,
                    action=action,
                    time_sec=duration,
                    toy_id=toy_id
                )

            require_lovense_success(response, action)

            results.append({
                "action": action,
                "timeSec": duration,
                "response": response,
            })

            # Wait for this command to finish before beginning the next one.
            if index < len(commands) - 1 and duration > 0:
                await asyncio.sleep(duration)

    except Exception:
        logger.exception(
            "Device sequence failed. Attempting emergency Lovense Stop."
        )

        try:
            stop_response = await client.control_toy_server(
                uid,
                action="Stop",
                time_sec=0,
                toy_id=toy_id
            )

            if lovense_response_succeeded(stop_response):
                logger.warning(
                    "Emergency Lovense Stop command succeeded."
                )
            else:
                logger.error(
                    f"Emergency Lovense Stop returned failure: {stop_response}"
                )

        except Exception:
            logger.exception(
                "Emergency Lovense Stop command also failed."
            )

        raise

    return {
        "commands": results,
        "totalSeconds": total_seconds,
        "simulated": False,
    }


# Define the path to the templates directory
templates_dir = Path(__file__).parent / "templates"

# Set up Jinja2 templates, pointing to the remote path
templates = Jinja2Templates(directory="/templates")

# Define a type for user data
UserData = Dict[str, Any]

# Define the image with required dependencies
image = (
    modal.Image.debian_slim(python_version="3.10")
    .pip_install(
        "fastapi",
        "uvicorn",
        "jinja2",
        "python-dotenv",
        "requests",
        "starlette",
        "itsdangerous",
        "sqlalchemy",
        "aiosqlite",
        "aiohttp",
        "python-multipart",        "sqlalchemy_utils",
    )
    .add_local_python_source(
        "models",
        "LovenseClient",
        "nomi_client",
        "command_parser",
    )
    .add_local_dir(
        templates_dir,
        remote_path="/templates",
    )
)

# Define a Modal secret
secret = modal.Secret.from_name("nomi-lovense-secrets")

# Create a Modal app
app_name = os.environ.get("APP_NAME", "vibro-9000")
app = modal.App(name=app_name)

# Create a shared volume for the database
VOLUME_PATH = os.environ.get("VOLUME_PATH", "/b")
volume = modal.Volume.from_name(f"{app_name}-volume", create_if_missing=True)
DATABASE_NAME_F = os.environ.get("DATABASE_NAME_F", "database.db")
DATABASE_URL = f"sqlite:///{VOLUME_PATH}/{DATABASE_NAME_F}"

# Secret key for session encryption
SECRET_KEY = os.environ.get("SECRET_KEY")
SALT = os.environ.get("LOVENSE_SALT", "le_salty_salt")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def generate_uid():
    return str(uuid.uuid4().hex)[:16]

# Define the ASGI app for Modal
@app.cls(
    image=image,
    secrets=[secret],
    volumes={VOLUME_PATH: volume},
    max_containers=1,
)
class FastAPIApp:
    @modal.enter()
    def initialize(self):
        logger.info("Initializing FastAPIApp")

        self.client = None
        self.templates = Jinja2Templates(directory="/templates")
        self.web_app = None
        self.secret_key = SECRET_KEY

        if not self.secret_key:
            raise ValueError("SECRET_KEY environment variable not set")

        self.salt = SALT
        self.volume = modal.Volume.from_name(
            f"{app_name}-volume",
            create_if_missing=True
        )

        # Initialize database
        engine = create_engine(DATABASE_URL)
        Base.metadata.create_all(bind=engine)

        # Initialize Lovense client
        self.client = LovenseClient(
            os.environ.get("LOVENSE_DEVELOPER_TOKEN"),
            DATABASE_URL,
            app_name
        )

        self.init_fastapi_app()
    @modal.asgi_app(label="gizmo")
    def __call__(self):
        return self.web_app

    def init_fastapi_app(self):
        from fastapi import FastAPI, Depends

        web_app = FastAPI()
        web_app.add_middleware(SessionMiddleware, secret_key=self.secret_key, https_only=True, same_site="lax")

        # Define a dependency to get self
        def get_app_instance():
            return self

        @web_app.get("/", response_class=HTMLResponse)
        async def index(request: Request, app_instance=Depends(get_app_instance)):
            return app_instance.templates.TemplateResponse(request=request, name="index.html", context={"request": request})

        @web_app.post("/start-session")
        async def start_session(request: Request, app_instance=Depends(get_app_instance)):
            form = await request.form()
            name = form.get('name')
            nomi_api_key = form.get('nomi_api_key')

            if not name or not nomi_api_key:
                return HTMLResponse(content="Name and Nomi API Key are required.", status_code=400)

            # Store user data in the db
            request.session['user_data'] = {
                'name': name,
                'nomi_api_key': nomi_api_key,
                'nomis': {},  # Will be populated later
                'chat_threads': {},  # Will store chat messages per Nomi
            }

            # Redirect to start Lovense authentication
            return RedirectResponse(url="/start-auth", status_code=303)

        @web_app.get("/start-auth")
        async def start_auth(request: Request, app_instance=Depends(get_app_instance)):
            user_data = request.session.get('user_data')
            if not user_data:
                return RedirectResponse(url="/", status_code=303)

            name = user_data['name']
            # Generate a unique user ID
            uid = generate_uid()

            # Store the uid in the session
            user_data['uid'] = uid
            request.session['user_data'] = user_data

            # Generate QR code
            try:
                qr_data = await app_instance.client.get_qr_code(uid, name, app_instance.salt)
                qr_url = qr_data['qr']
            except Exception as e:
                return HTMLResponse(content=str(e), status_code=500)

            # Render the QR code page
            context = {
                'request': request,
                'qr_url': qr_url,
                'uid': uid,
            }
            return app_instance.templates.TemplateResponse(request=request, name="qr_code.html", context=context)

        @web_app.post("/lovense/callback")
        async def lovense_callback(request: Request, app_instance=Depends(get_app_instance)):
            try:
                data = await request.json()
                uid = data.get("uid")
                callback_utoken = data.get("utoken")

                if not uid or not callback_utoken:
                    logger.warning("Rejected Lovense callback missing uid or utoken.")
                    return JSONResponse(
                        content={"error": "Invalid callback authentication."},
                        status_code=401,
                    )

                user = app_instance.client.get_user(uid)
                if not user:
                    logger.warning(f"Rejected Lovense callback for unknown UID: {uid}")
                    return JSONResponse(
                        content={"error": "Invalid callback authentication."},
                        status_code=401,
                    )

                expected_utoken = user.data.get("utoken")
                if not expected_utoken or not hmac.compare_digest(
                    str(callback_utoken),
                    str(expected_utoken),
                ):
                    logger.warning(f"Rejected Lovense callback with invalid utoken for UID: {uid}")
                    return JSONResponse(
                        content={"error": "Invalid callback authentication."},
                        status_code=401,
                    )

                await app_instance.client.handle_callback(uid, data)

                logger.info(f"Authenticated Lovense callback handled successfully for UID: {uid}")
                return JSONResponse(content={"message": "Callback handled successfully"})

            except Exception as e:
                logger.error(f"Error in lovense_callback: {str(e)}")
                return JSONResponse(
                    content={"error": "Invalid callback request."},
                    status_code=400,
                )

        @web_app.get("/skip-auth")
        async def skip_auth(request: Request, app_instance=Depends(get_app_instance)):
            logger.info("Received GET request to /skip-auth")

            user_data = request.session.get('user_data')
            if not user_data:
                return JSONResponse(
                    content={"error": "No active session"},
                    status_code=400
                )

            uid = user_data.get('uid')
            if not uid:
                return JSONResponse(
                    content={"error": "No UID in session"},
                    status_code=400
                )

            fake_toy = {
                '_test_': {
                    'nickName': 'Test Device',
                    'status': 1,
                    'id': '_test_',
                    'name': 'Lovense Test Device'
                }
            }

            fake_lovense_data = {
                "toys": fake_toy,
                "domain": "test.local",
                "httpsPort": "",
                "httpPort": "",
                "wssPort": "",
                "wsPort": "",
                "platform": "test",
                "appVersion": "test"
            }

            try:
                # Create/update the fake Lovense device
                await app_instance.client.handle_callback(uid, fake_lovense_data)

                # Load the user's Nomis immediately
                nomi_api_key = user_data.get('nomi_api_key')
                nomi_client = NomiClient(nomi_api_key)

                nomis_data = await nomi_client.list_nomis()
                nomis = nomis_data.get('nomis', [])

                user_data['nomis'] = {
                    nomi['uuid']: nomi for nomi in nomis
                }

                request.session['user_data'] = user_data

                logger.info(
                    f"Test mode enabled for UID: {uid}; "
                    f"loaded {len(nomis)} Nomis"
                )

                return JSONResponse(
                    content={
                        "authenticated": True,
                        "test_mode": True,
                        "nomi_count": len(nomis),
                        "redirect": "/control"
                    }
                )

            except Exception as e:
                logger.exception("Failed to enable test mode")
                return JSONResponse(
                    content={"error": str(e)},
                    status_code=500
                )
        @web_app.get("/check-auth")
        async def check_auth(request: Request, app_instance=Depends(get_app_instance)):
            user_data = request.session.get('user_data')
            if not user_data:
                return {"authenticated": False}

            uid = user_data.get('uid')
            user = app_instance.client.get_user(uid)
            if user and 'toys' in user.data:
                # Fetch user's Nomis
                nomi_api_key = user_data['nomi_api_key']
                nomi_client = NomiClient(nomi_api_key)
                nomis_data = await nomi_client.list_nomis()
                nomis = nomis_data.get('nomis', [])
                # Store Nomis in the session
                user_data['nomis'] = {nomi['uuid']: nomi for nomi in nomis}
                return {"authenticated": True}
            else:
                return {"authenticated": False}
        
        @web_app.get("/control", response_class=HTMLResponse)
        async def control_page(request: Request, app_instance=Depends(get_app_instance)):
            user_data = request.session.get('user_data')
            if not user_data:
                return RedirectResponse(url="/", status_code=303)

            uid = user_data.get('uid')
            user = app_instance.client.get_user(uid)
            if not user or 'toys' not in user.data:
                return HTMLResponse(content="User not authenticated or no toys connected.", status_code=400)

            # Get the list of toys
            toys = user.data.get('toys', {})
            toy_list = []
            for toy_id, toy_info in toys.items():
                toy_list.append({'id': toy_id, 'name': toy_info.get('name', 'Unknown')})

            # Get user's Nomis
            nomis = user_data.get('nomis', {}).values()

            context = {
                'request': request,
                'uid': uid,
                'toys': toy_list,
                'nomis': nomis,
            }
            return templates.TemplateResponse(request=request, name="control.html", context=context)

        @web_app.post("/send-command")
        async def send_command(request: Request, app_instance=Depends(get_app_instance)):
            form = await request.form()
            uid = form.get('uid')
            toy = form.get('toy')
            action = form.get('action')

            user_data = request.session.get('user_data')
            if not user_data:
                return RedirectResponse(url="/", status_code=303)

            if uid != user_data.get('uid'):
                return HTMLResponse(content="Invalid user ID.", status_code=400)

            user = app_instance.client.get_user(uid)
            if not user or 'toys' not in user.data:
                return HTMLResponse(content="User not authenticated or no toys connected.", status_code=400)
            
            toys=user.data.get('toys', {})
            if "_test_" in toys:
                return HTMLResponse(content="Cannot control a test toy.", status_code=400)

            try:
                # Send the command via the server API
                response = await app_instance.client.control_toy_server(uid, action=action, time_sec=20, toy_id=toy)
                if response.get('code') == 200:
                    return HTMLResponse(content="Command sent successfully.")
                else:
                    error_message = response.get('message', 'Unknown error')
                    return HTMLResponse(content=f"Error sending command: {error_message}", status_code=500)
            except Exception as e:
                return HTMLResponse(content=f"Error sending command: {str(e)}", status_code=500)

        @web_app.get("/chat", response_class=HTMLResponse)
        async def chat_page(request: Request, nomi_id: str, app_instance=Depends(get_app_instance)):
            user_data = request.session.get('user_data')
            if not user_data:
                return RedirectResponse(url="/", status_code=303)

            uid = user_data.get('uid')
            lovense_user = app_instance.client.get_user(uid)
            lovense_user_data = lovense_user.data
            toys = lovense_user_data.get('toys', {})
            if not uid or not lovense_user or not toys:
                return HTMLResponse(content="User not authenticated or no toys connected.", status_code=400)

            nomis = user_data.get('nomis', {})
            if nomi_id not in nomis:
                return HTMLResponse(content="Invalid Nomi ID.", status_code=400)

            # Generate a one-time ID for this chat form submission.
            submission_id = str(uuid.uuid4())
            user_data['chat_submission_id'] = submission_id
            request.session['user_data'] = user_data

            # Retrieve chat messages for this Nomi
            chat_threads = user_data.get('chat_threads', {})
            messages = chat_threads.get(nomi_id, [])

            # Consume the most recent device result once after POST/Redirect/GET.
            last_device_result = user_data.pop("last_device_result", None)
            request.session["user_data"] = user_data

            if last_device_result:
                flashed_device_command = last_device_result.get("device_command")
                flashed_device_commands = last_device_result.get("device_commands", [])
            else:
                flashed_device_command = None
                flashed_device_commands = []

            context = {
                'request': request,
                'uid': uid,
                'nomi_id': nomi_id,
                'nomi_name': nomis[nomi_id]['name'],
                'nomis': nomis,
                'messages': messages,
                'device_command': flashed_device_command,
                'device_commands': flashed_device_commands,
                'submission_id': submission_id,
            }
            return templates.TemplateResponse(request=request, name="chat.html", context=context)

        @web_app.post("/send-chat-message", response_class=HTMLResponse)
        async def send_chat_message(request: Request, app_instance=Depends(get_app_instance)):
            form = await request.form()
            uid = form.get('uid')
            nomi_id = form.get('nomi_id')
            message_text = form.get('message_text')
            submission_id = form.get('submission_id')

            user_data = request.session.get('user_data')
            ud_uid = user_data.get('uid')
            ud_nomis = user_data.get('nomis', {})
            chat_threads = user_data.get('chat_threads', {})

            # Reject stale or already-submitted chat forms.
            expected_submission_id = user_data.get('chat_submission_id')

            if not submission_id or submission_id != expected_submission_id:
                logger.warning("Duplicate or stale chat submission ignored.")
                return RedirectResponse(
                    url=f"/chat?nomi_id={nomi_id}",
                    status_code=303
                )

            # Consume the token before contacting Nomi or Lovense.
            user_data['chat_submission_id'] = None
            request.session['user_data'] = user_data


            user = app_instance.client.get_user(uid)
            if not user or 'toys' not in user.data:
                return HTMLResponse(content="User not authenticated or no toys connected.", status_code=400)

            if not uid or not nomi_id or not message_text:
                return HTMLResponse(content="Invalid form data.", status_code=400)

            if uid != user.uid:
                return HTMLResponse(content="Invalid user ID.", status_code=400)

            nomis = ud_nomis
            if nomi_id not in nomis:
                return HTMLResponse(content="Invalid Nomi ID.", status_code=400)

            nomi_api_key = user_data.get('nomi_api_key')
            nomi_client = NomiClient(nomi_api_key)

            # Preserve exactly what the user typed for visible chat history.
            visible_message_text = message_text

            # Send exactly what the user typed to Nomi.
            # Device-control behavior is taught through the Nomi backstory.
            outbound_message = visible_message_text

            # Send message to Nomi AI
            try:
                response = await nomi_client.send_message(nomi_id, outbound_message)
                sent_message = response.get('sentMessage', {})
                reply_message = response.get('replyMessage', {})
            except Exception as e:
                return HTMLResponse(content=f"Error communicating with Nomi AI: {str(e)}", status_code=500)

            # Parse Nomi AI's response to extract commands
            toys = user.data.get('toys', {})
            commands = parse_nomi_commands(reply_message.get('text', ''))
            command = commands[0] if commands else None

            device_command = None
            device_commands = []
            test_mode = "_test_" in toys

            if command:
                logger.info(f"Parsed Nomi command: {command}")
                logger.info(f"Parsed Nomi command sequence: {commands}")

                device_commands = [
                    {
                        'action': item['action'],
                        'timeSec': item['timeSec'],
                        'test_mode': test_mode,
                    }
                    for item in commands
                ]

                device_command = {
                    'action': command['action'],
                    'timeSec': command['timeSec'],
                    'test_mode': test_mode,
                }

                if test_mode:
                    try:
                        sequence_result = await execute_device_sequence(
                            app_instance.client,
                            uid,
                            toy_id="_test_",
                            commands=commands,
                            simulate=True,
                        )

                        logger.info(
                            f"Test mode sequence simulation complete: "
                            f"{sequence_result}"
                        )

                    except ValueError as e:
                        logger.warning(
                            f"Rejected device command sequence: {e}"
                        )
                else:
                    real_toys = {
                        toy_key: toy_data
                        for toy_key, toy_data in toys.items()
                        if toy_key != "_test_"
                    }

                    if not real_toys:
                        logger.warning(f"No real Lovense toy available for UID: {uid}")
                        return HTMLResponse(
                            content="No real Lovense toy is connected.",
                            status_code=400,
                        )

                    connected_toys = {
                        toy_key: toy_data
                        for toy_key, toy_data in real_toys.items()
                        if str(toy_data.get("status")) == "1"
                    }

                    if not connected_toys:
                        logger.warning(f"No connected Lovense toy available for UID: {uid}")
                        return HTMLResponse(
                            content="Lovense toy is not currently connected.",
                            status_code=400,
                        )

                    toy_id = next(iter(connected_toys.keys()))

                    if not toy_id or toy_id == "_test_":
                        raise RuntimeError("Invalid real Lovense toy selection.")

                    try:
                        sequence_result = await execute_device_sequence(
                            app_instance.client,
                            uid,
                            toy_id=toy_id,
                            commands=commands,
                            simulate=False,
                        )

                        logger.info(
                            f"Real Lovense sequence complete: "
                            f"{sequence_result}"
                        )

                    except ValueError as e:
                        logger.warning(
                            f"Rejected device command sequence: {e}"
                        )

                        return HTMLResponse(
                            content=f"Device command sequence rejected: {str(e)}",
                            status_code=400
                        )

                    except Exception as e:
                        logger.exception(
                            "Real Lovense sequence execution failed."
                        )

                        return HTMLResponse(
                            content=f"Error sending command sequence to device: {str(e)}",
                            status_code=500
                        )
            else:
                logger.info("No Lovense command detected in Nomi response.")

            # Update message history using the actual user input.
            logger.info(
                f"CHAT HISTORY BEFORE: nomi_id={nomi_id}, "
                f"count={len(chat_threads.get(nomi_id, []))}"
            )

            logger.info(
                f"NOMI RESPONSE: keys={list(response.keys())}, "
                f"reply_text={reply_message.get('text')!r}"
            )
            messages = chat_threads.setdefault(nomi_id, [])

            messages.append({
                'sender': 'user',
                'text': visible_message_text
            })

            reply_text = reply_message.get('text')

            if reply_text:
                messages.append({
                    'sender': 'nomi',
                    'text': reply_text
                })

            logger.info(
                f"CHAT HISTORY AFTER: nomi_id={nomi_id}, "
                f"count={len(messages)}"
            )

            # Preserve the device result across the 303 redirect for one GET only.
            user_data["last_device_result"] = {
                "device_command": device_command,
                "device_commands": device_commands,
            }

            # Explicitly persist updated chat history in the session
            user_data['chat_threads'] = chat_threads
            request.session['user_data'] = user_data

            # Redirect back to GET /chat after processing the POST.
            # The GET route generates the next one-time submission token.
            return RedirectResponse(
                url=f"/chat?nomi_id={nomi_id}",
                status_code=303
            )
    
        
        # Assign the web_app to the instance variable
        self.web_app = web_app





















