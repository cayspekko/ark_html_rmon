import asyncio
import json
import os
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv(verbose=True)

from asyncio.subprocess import PIPE, STDOUT
from datetime import datetime
from functools import wraps

from fastapi import (Body, Depends, FastAPI, Form, HTTPException, Request, WebSocket, status)
from fastapi.logger import logger as fastapi_logger
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_utils.tasks import repeat_every
from filelock import FileLock
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware
from starlette.websockets import WebSocketDisconnect
from tinydb import Query, TinyDB, JSONStorage
from tinydb.operations import set as db_set
from websockets.exceptions import ConnectionClosed


# simple FileLock extension to tinydb to protect read/writes
class FileLockingStorage(JSONStorage):
    def __init__(self, path: str, **kwargs):
        self.lock = FileLock(path + ".lock")
        super().__init__(path, **kwargs)

    def read(self):
        with self.lock:
            return super().read()

    def write(self, data):
        with self.lock:
            super().write(data)


ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])') # matches ansi escape characters in strings

# logging solution in docker https://github.com/tiangolo/uvicorn-gunicorn-fastapi-docker/issues/19#issuecomment-720720048
import logging

gunicorn_logger = logging.getLogger("gunicorn")
log_level = gunicorn_logger.level

root_logger = logging.getLogger()
gunicorn_error_logger = logging.getLogger("gunicorn.error")
uvicorn_access_logger = logging.getLogger("uvicorn.access")

# Use gunicorn error handlers for root, uvicorn, and fastapi loggers
root_logger.handlers = gunicorn_error_logger.handlers
uvicorn_access_logger.handlers = gunicorn_error_logger.handlers
fastapi_logger.handlers = gunicorn_error_logger.handlers

# Pass on logging levels for root, uvicorn, and fastapi loggers
root_logger.setLevel(log_level)
uvicorn_access_logger.setLevel(log_level)
fastapi_logger.setLevel(log_level)

logger = logging.getLogger(__name__)

logging.getLogger("filelock").setLevel("INFO") # filelock debug is too verbose


RUN_CMD = """#!/bin/bash
docker rm ark
docker run -d --restart=always -v steam:/home/steam/Steam \
        -v ark:/ark \
        -p 7778:7778 -p 7778:7778/udp \
        -p 7777:7777 -p 7777:7777/udp \
        -p 27015:27015 -p 27015:27015/udp \
        -p 32330:32330 -p 32330:32330/udp \
        -e am_arkflag_crossplay=true \
        -e am_arkflag_NoBattlEye=true \
        {} \
        --name ark \
        thmhoag/arkserver"""


VALHEIM_RUN_CMD = """#!/bin/bash
docker rm valheim
docker run -d --restart=no \
    -v valheim_saves:/home/steam/.config/unity3d/IronGate/Valheim \
    -v valheim_server:/home/steam/valheim \
    -v valheim_backups:/home/steam/backups \
    -p 2456:2456/udp \
    -p 2457:2457/udp \
    -p 2458:2458/udp \
    -e PORT=2456 \
    -e NAME="World of Doug" \
    -e WORLD="Dedicated" \
    -e PASSWORD="1mth3b3st" \
    -e TZ="America/Boise" \
    -e PUBLIC=1 \
    -e AUTO_UPDATE=0 \
    -e AUTO_UPDATE_SCHEDULE="0 1 * * *" \
    -e AUTO_BACKUP=1 \
    -e AUTO_BACKUP_SCHEDULE="*/15 * * * *" \
    -e AUTO_BACKUP_REMOVE_OLD=1 \
    -e AUTO_BACKUP_DAYS_TO_LIVE=3 \
    -e AUTO_BACKUP_ON_UPDATE=1 \
    -e AUTO_BACKUP_ON_SHUTDOWN=1 \
    -e UPDATE_ON_STARTUP=0 \
    -e FORCE_INSTALL=1 \
    -e TYPE=bepinex \
    -e MODS={} \
    --name valheim \
    mbround18/valheim:1"""


templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

users = None
settings = None
am_settings = None
gu_settings = None
valheim_mods = None
poll_status = None

pwd_context = None

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv('SESSION_KEY'), max_age=60*60, same_site='strict', https_only=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


logger.info("___---___--- START APP (worker) ---___---___")


def _now():
    return datetime.now().strftime("%h/%d/%Y  %I:%M:%S %p")


def _hash(new_payload, key="status"):
    database = poll_status.get(Query().key == key) or {}
    new_hash = hash("".join(new_payload))
    old_hash = hash("".join(database.get("payload") or ["checking..."]))
    return (new_hash == old_hash)


def _run_cmd():
    cmd = RUN_CMD
    am_s = ""
    for d in am_settings.all():
        am_s += f"-e {d['key']}=\"{d['value']}\" "
    cmd = cmd.format(am_s)
    return cmd


def _valheim_run_cmd():
    cmd = VALHEIM_RUN_CMD
    mods = "\""
    for d in valheim_mods.all():
        mods += f"{d['value']},\n"
    mods += "\""
    cmd = cmd.format(mods)
    return cmd


def _authorize(credentials, request: Request = None):
    User = Query()
    u = users.get(User.username == credentials.username)
    if not (u and pwd_context.verify(credentials.password, u['password'])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"}
        )
    if request:
        new_uuid = str(uuid.uuid4())
        users.upsert({'uuid':new_uuid}, User.username == credentials.username)
        request.session['uuid'] = new_uuid


def authorize(credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)


def authorize_ws(func):
    @wraps(func) # not exactly sure why this is needed but it doesn't work without it
    async def wrapped(websocket):
        User = Query()
        u = users.get(User.uuid == websocket.session.get("uuid"))
        if not u:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        return await func(websocket)
    return wrapped


@app.on_event("startup")
async def startup():
    global users, settings, am_settings, valheim_mods, gu_settings, poll_status, pwd_context, lock

    logger.info('in startup')

    users = TinyDB('db.json', storage=FileLockingStorage) 
    settings = users.table('settings')
    am_settings = users.table('am_settings')
    gu_settings = users.table('gu_settings')
    valheim_mods = users.table('valheim_mods')
    poll_status = users.table('poll_status')
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    logger.info('end startup')


@app.on_event("startup")
@repeat_every(seconds=10*60, logger=logger)
async def autoshutdown_server():
    logger.debug('repeat task')

    cmd = r"docker inspect -f '{{.State.Status}}' ark"
    docker_status = await run_command(cmd)
    docker_status = docker_status[0]
    logger.debug('docker_status is %s', docker_status)

    if docker_status == "running":
        cmd = "docker exec -i ark arkmanager rconcmd listplayers"
        rval = await run_command(cmd, beautify=True)
        logger.info('current player info: %s', rval)

        if len(rval) > 1 and "No Players Connected" in rval[1]:

            Poll = Query()
            db_key = poll_status.get(Poll.key == 'autoshutdown') or {}
            t = db_key.get("time") or time.time()

            if time.time() - t > (60*60):
                logger.info('no players connected for an hour, shutting down')
                rval = await run_command("docker exec -i ark arkmanager stop --saveworld && docker kill ark")
                logger.info(rval)
            else:
                logger.info('idle time detected %d / %d', time.time() - t, 60*60)
                return
        else:
            logger.info("server running, players detected, reset autoshutdown timer")

    else:
        logger.info("server not running, reset autoshutdown timer")

    Poll = Query()
    poll_status.upsert({"key":'autoshutdown', "time":time.time()}, Poll.key == 'autoshutdown')
    

@app.get("/api/status", dependencies=[Depends(authorize)])
async def api_status():

    cmd = 'docker exec -i ark arkmanager status'

    rval = await run_command(cmd, beautify=True)

    return {"data": rval}


@app.get("/api/players", dependencies=[Depends(authorize)])
async def api_players():
    cmd = 'docker exec -i ark arkmanager rconcmd listplayers'

    rval = await run_command(cmd)

    return {"data": rval}


@app.post('/api/start', dependencies=[Depends(authorize)])
async def api_start():

    cmd = _run_cmd()

    rval = await run_command(cmd)

    return {"data": rval}


@app.post('/api/stop', dependencies=[Depends(authorize)])
async def api_stop():

    cmd = f"docker exec -i ark arkmanager stop --saveworld && docker kill ark"

    rval = await run_command(cmd)

    return {"data": rval}


@app.get("/api/logs", dependencies=[Depends(authorize)])
async def api_logs():

    cmd = 'docker logs ark'

    rval = await run_command(cmd, beautify=True)

    return {"data": rval}


@app.post('/api/daytime', dependencies=[Depends(authorize)])
async def api_daytime():

    cmd = "docker exec -i ark arkmanager rconcmd \"settimeofday 6:00\""

    rval = await run_command(cmd)

    return {"data": rval}


@app.post("/api/change_password")
async def change_password(request: Request, psw: str = Form(...), credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials, request)
    User = Query()
    new_pw = pwd_context.hash(psw)
    users.update(db_set('password', new_pw), User.username == credentials.username)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND) 


@app.get("/api/valheim_plus_cfg", dependencies=[Depends(authorize)])
async def get_valheim_plus_cfg():
    cmd = "docker exec -u 1000:1000 -i valheim cat /home/steam/valheim/BepInEx/config/valheim_plus.cfg"

    rval = await run_command(cmd)

    return {"data": "\n".join(rval)}


@app.get("/api/valheim_plus_cfg_backups", dependencies=[Depends(authorize)])
async def get_valheim_plus_cfg_backups():
    cmd = 'docker exec -u 1000:1000 -i valheim bash -c "ls -1 /home/steam/valheim/BepInEx/config/valheim_plus.cfg.* | xargs -n 1 basename"'

    rval = await run_command(cmd)

    return {"data": rval}

@app.get("/api/valheim_plus_cfg_backups/{filename}", dependencies=[Depends(authorize)])
async def get_valheim_plus_cfg_backup_filename(filename: str):
    cmd = f"docker exec -u 1000:1000 -i valheim cat /home/steam/valheim/BepInEx/config/{filename}"

    rval = await run_command(cmd)

    return {"data": "\n".join(rval)}


@app.post("/api/valheim_plus_cfg", dependencies=[Depends(authorize)])
async def post_valheim_plus_cfg(data: str = Body(...)):
    data = data.replace('"', '\\"') # delimit "

    cmd = f'docker exec -u 1000:1000 -i valheim bash -c "cp /home/steam/valheim/BepInEx/config/valheim_plus.cfg /home/steam/valheim/BepInEx/config/valheim_plus.cfg.{datetime.now().strftime("%y%m%d%H%M%S")}"'
    rval = await run_command(cmd)

    cmd = f'docker exec -u 1000:1000 -i valheim bash -c "cat << EOF > /home/steam/valheim/BepInEx/config/valheim_plus.cfg\n{data}\nEOF"'
    rval.extend(await run_command(cmd))

    return {"data": rval} 


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials, request)

    ark_cards = [
        "cards/default.html",
        "cards/status.html",
        "cards/start.html",
        "cards/players.html",
        "cards/am_settings.html",
        "cards/settings.html",
        "cards/gu_settings.html"
    ]

    valheim_cards = [
        "cards/valheim_status.html",
        "cards/valheim_commands.html",
        "cards/valheim_mods.html",
        "cards/valheim_plus_cfg.html"
    ]

    misc_cards = [
        "cards/password.html"
    ]

    return templates.TemplateResponse("cards.html", {
        "request": request, # This is required for jinja, but not used in my templates
        "ark_cards": ark_cards,
        "valheim_cards": valheim_cards,
        "misc_cards": misc_cards, 
        "ws_endpoint": os.getenv('WS_ENDPOINT'), 
        "username": credentials.username
    })

def ws_username(websocket):
    uuid = websocket.session.get('uuid')
    Q = Query()
    user = users.get(Q.uuid == uuid)
    return user.get('username')

async def websocket_poll(websocket, key="status"):
    await websocket.accept()
    logger.info("accepted client on %s: %s %s" % (websocket.url, ws_username(websocket), websocket.client.host))
    logger.info('begin websocket_poll on %s', key)
    Poll = Query()
    db_key = poll_status.get(Poll.key == key) or {}

    updated = db_key.get("updated") or [_now()]
    payload = db_key.get("payload") or ["checking..."]

    old_recent = "</br>".join(updated + payload)
    logger.debug('sending text to websocket on key %s', key)
    await websocket.send_text(old_recent)

    while True:
        await asyncio.sleep(5)

        db_key = poll_status.get(Poll.key == key) or {}
        time_now = time.time()

        if not db_key or (time_now - db_key["time"]) > 5:
            poll_status.update({"time":time.time()}, Poll.key == key)            

            cmd = 'docker exec -i ark arkmanager status | aha --no-header'

            if key == "players":
                cmd =  "docker exec -i ark arkmanager rconcmd listplayers | aha --no-header"

            elif key == "valheim_status":
                cmd = "docker exec -u 1000:1000 -i valheim odin status | aha --no-header"

            logger.debug('websocket_poll executing command %s', cmd)

            rval = []
            async for l in get_lines(cmd):
                rval.append(l.decode())

            if not _hash(rval, key):
                db_key["updated"] = [_now()]
                db_key["payload"] = rval
                poll_status.upsert({"key":key, "updated": db_key["updated"], "payload": db_key["payload"]}, Poll.key == key)

        updated = db_key.get("updated") or [_now()]
        payload = db_key.get("payload") or ["checking..."]

        recent = "</br>".join(updated + payload)

        if old_recent != recent:
            old_recent = recent
            try:

                logger.debug('sending text to websocket on key %s', key)
                logger.debug(old_recent)
                await websocket.send_text(old_recent)
            except (WebSocketDisconnect, ConnectionClosed) as e:
                logger.error('got exception while send_text: %s', e)
                await websocket.close()
                return


@app.websocket("/status")
@authorize_ws
async def status_endpoint(websocket: WebSocket):
    await websocket_poll(websocket)


@app.websocket("/valheim_status")
@authorize_ws
async def status_endpoint(websocket: WebSocket):
    await websocket_poll(websocket, key="valheim_status")


@app.websocket("/players")
@authorize_ws
async def players_endpoint(websocket: WebSocket):
    await websocket_poll(websocket, key="players")


async def ws_command(websocket: WebSocket, cmd_dict: dict):
    await websocket.accept()
    while True:
        try:
            data = await websocket.receive_text()
            logger.info("command data: %s" % data)
        except WebSocketDisconnect as e:
            logger.error('got error trying to receive_text %s', e)
            await websocket.close()
            return
        data = json.loads(data)
        cmd = (cmd_dict.get(data['cmd']) or (lambda d: "docker ps"))(data)
        rval = []

        logger.debug('ws_command executing command %s', cmd)

        async for l in get_lines(cmd):
            rval.append(l.decode())
        await websocket.send_text("</br>".join(rval))


@app.websocket("/command")
@authorize_ws
async def command_endpoint(websocket: WebSocket):
    await ws_command(websocket, {
        "start": lambda d: f"{_run_cmd()} | aha --no-header",
        "stop": lambda d: f"docker exec -i ark arkmanager stop --saveworld | aha --no-header && docker kill ark",
        "kick": lambda d: f"docker exec -i ark arkmanager rconcmd \"kickplayer {d.get('player_id')}\" | aha --no-header",
        "daytime": lambda d: f"docker exec -i ark arkmanager rconcmd \"settimeofday 6:00\" | aha --no-header",
        "cancelshutdown": lambda d: f"docker exec -i ark arkmanager cancelshutdown | aha --no-header",
        "logs": lambda d: f"docker logs ark | aha --no-header"
    })


@app.websocket('/valheim_command')
@authorize_ws
async def valheim_command_endpoint(websocket: WebSocket):
    await ws_command(websocket, {
        "logs": lambda d: f"docker logs valheim | aha --no-header",
        "stop": lambda d: f"docker exec -i valheim kill 1 && echo 'Killing Valheim! Check logs!'",
        "start": lambda d: f"{_valheim_run_cmd()} | aha --no-header",
        "restart": lambda d: f"docker exec -u 1000:1000 -i valheim bash -c 'cd /home/steam/valheim && odin stop && odin start'"
    })


@app.websocket('/settings')
@authorize_ws
async def settings_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text(json.dumps(settings.all()))

    while True:
        try:
            data = await websocket.receive_text()
        except WebSocketDisconnect:
            await websocket.close()
            return
        data = json.loads(data)
        if data.get('cmd') == 'put':
            settings.truncate()
            settings.insert_multiple(data.get('data'))

            cmd = "docker run -i --rm -v ark:/ark --name ark_oneshot thmhoag/arkserver /ark/update_game_ini.sh "
            for setting in settings.all():
                cmd += f"{setting['key']}={setting['value']} "
            cmd += " | aha --no-header"

            logger.debug('executing cmd %s:', cmd)

            async for l in get_lines(cmd):
                logger.debug(l)

        await websocket.send_text(json.dumps(settings.all()))


@app.websocket('/am_settings')
@authorize_ws
async def am_settings_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text(json.dumps(am_settings.all()))

    while True:
        try:
            data = await websocket.receive_text()
        except WebSocketDisconnect:
            await websocket.close()
            return
        data = json.loads(data)
        if data.get('cmd') == 'put':
            am_settings.truncate()
            am_settings.insert_multiple(data.get('data'))

        await websocket.send_text(json.dumps(am_settings.all()))


@app.websocket('/gu_settings')
@authorize_ws
async def gu_settings_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text(json.dumps(gu_settings.all()))

    while True:
        try:
            data = await websocket.receive_text()
        except WebSocketDisconnect:
            await websocket.close()
            return
        data = json.loads(data)
        if data.get('cmd') == 'put':
            gu_settings.truncate()
            gu_settings.insert_multiple(data.get('data'))

            cmd = "docker run -i --rm -v ark:/ark --name ark_oneshot thmhoag/arkserver /ark/update_gus_ini.sh "
            for setting in gu_settings.all():
                cmd += f"{setting['key']}={setting['value']} "
            cmd += " | aha --no-header"

            logger.debug('executing cmd %s:', cmd)
            async for l in get_lines(cmd):
                logger.debug(l)

        await websocket.send_text(json.dumps(gu_settings.all()))


@app.websocket('/valheim_mods')
@authorize_ws
async def valheim_mods_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text(json.dumps(valheim_mods.all()))

    while True:
        try:
            data = await websocket.receive_text()
        except WebSocketDisconnect:
            await websocket.close()
            return
        data = json.loads(data)
        if data.get('cmd') == 'put':
            valheim_mods.truncate()
            valheim_mods.insert_multiple(data.get('data'))

        await websocket.send_text(json.dumps(valheim_mods.all()))


async def run_command(cmd, beautify=False):
    logger.debug('api is executing command %s', cmd)

    rval = []
    async for l in get_lines(cmd):
        l = l.decode()
        if beautify:
            # beautify output
            l = ansi_escape.sub('', l)
            l = l.strip()
        rval.append(l)

    return rval


async def get_lines(shell_command):
    p = await asyncio.create_subprocess_shell(shell_command,
            stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    for l in (await p.communicate())[0].splitlines():
        yield l
