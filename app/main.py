import asyncio
import time 
import json
import os
import re

from dotenv import load_dotenv
from pydantic import Json
load_dotenv(verbose=True)

from asyncio.subprocess import PIPE, STDOUT

from fastapi import FastAPI, Request, WebSocket, Depends, HTTPException, status, Form 
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.logger import logger as fastapi_logger
from passlib.context import CryptContext
from datetime import datetime
from tinydb import TinyDB, Query
from tinydb.operations import set as db_set
from filelock import FileLock

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

templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

users = None
settings = None
am_settings = None
gu_settings = None
poll_status = None

pwd_context = None

lock = None

async def startup():
    global users, settings, am_settings, gu_settings, poll_status, pwd_context, lock

    logger.info('in startup')

    users = TinyDB('db.json') 
    settings = users.table('settings')
    am_settings = users.table('am_settings')
    gu_settings = users.table('gu_settings')
    poll_status = users.table('poll_status')
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    lock = FileLock('db.json.lock')

    logger.info('end startup')


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_event_handler("startup", startup)

logger.info("___---___--- START APP (worker) ---___---___")


def _now():
    return datetime.now().strftime("%h/%d/%Y  %I:%M:%S %p")


def _hash(new_payload, key="status"):
    database = poll_status.get(Query().key == key) or {}
    new_hash = hash("".join(new_payload))
    old_hash = hash("".join(database.get("payload") or ["checking..."]))
    return (new_hash == old_hash)


def _authorize(credentials):
    User = Query()
    u = users.get(User.username == credentials.username)
    if not (u and pwd_context.verify(credentials.password, u['password'])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"}
        )


def _run_cmd():
    cmd = RUN_CMD
    am_s = ""
    for d in am_settings.all():
        am_s += f"-e {d['key']}=\"{d['value']}\" "
    cmd = cmd.format(am_s)
    return cmd


@app.get("/api/status")
async def api_status(credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    cmd = 'docker exec -i ark arkmanager status'

    logger.debug('api is executing command %s', cmd)

    rval = []
    async for l in get_lines(cmd):
        # beautify output
        l = l.decode()
        l = ansi_escape.sub('', l)
        l = l.strip()
        if not l:
            continue
        rval.append(l)

    return {"data": rval}


@app.get("/api/players")
async def api_players(credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    cmd = 'docker exec -i ark arkmanager rconcmd listplayers'

    logger.debug('api is executing command %s', cmd)

    rval = []
    async for l in get_lines(cmd):
        rval.append(l.decode())

    return {"data": rval}


@app.post('/api/start')
async def api_start(credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    cmd = _run_cmd()

    logger.debug('api is executing command %s', cmd)

    rval = []
    async for l in get_lines(cmd):
        rval.append(l.decode())

    return {"data": rval}


@app.post('/api/stop')
async def api_stop(credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    cmd = f"docker exec -i ark arkmanager stop --saveworld && docker kill ark"

    logger.debug('api is executing command %s', cmd)

    rval = []
    async for l in get_lines(cmd):
        rval.append(l.decode())

    return {"data": rval}


@app.get("/api/logs")
async def api_logs(credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    cmd = 'docker logs ark'

    logger.debug('api is executing command %s', cmd)

    rval = []
    async for l in get_lines(cmd):
        # beautify output
        l = l.decode()
        l = ansi_escape.sub('', l)
        l = l.strip()
        if not l:
            continue
        rval.append(l)

    return {"data": rval}

@app.post("/api/change_password")
async def change_password(psw: str=Form(...),  credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    User = Query()
    new_pw = pwd_context.hash(psw)
    with lock:
        users.update(db_set('password', new_pw), User.username == credentials.username)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND) 


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    _authorize(credentials)

    cards = [
        "cards/default.html",
        "cards/status.html",
        "cards/start.html",
        "cards/players.html",
        "cards/commands.html",
        "cards/am_settings.html",
        "cards/settings.html",
        "cards/gu_settings.html",
        "cards/password.html"
    ]

    return templates.TemplateResponse("cards.html", {
        "request": request, 
        "cards": cards, "ws_endpoint": os.getenv('WS_ENDPOINT'), 
        "credentials": credentials
        })


async def websocket_poll(websocket, key="status"):
    await websocket.accept()
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
            with lock:
                poll_status.update({"time":time.time()}, Poll.key == key)            

            cmd = 'docker exec -i ark arkmanager status | aha --no-header'

            if key == "players":
                cmd =  "docker exec -i ark arkmanager rconcmd listplayers | aha --no-header"

            logger.debug('executing command %s', cmd)

            rval = []
            async for l in get_lines(cmd):
                rval.append(l.decode())

            if not _hash(rval, key):
                db_key["updated"] = [_now()]
                db_key["payload"] = rval
                with lock:
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
async def status_endpoint(websocket: WebSocket):
    await websocket_poll(websocket)


@app.websocket("/players")
async def players_endpoint(websocket: WebSocket):
    await websocket_poll(websocket, key="players")


@app.websocket("/command")
async def command_endpoint(websocket: WebSocket):
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
        cmd = "docker ps"
        if data.get('cmd') == "start":
            cmd = f"{_run_cmd()} | aha --no-header"
        elif data.get('cmd') == "stop":
            cmd = f"docker exec -i ark arkmanager stop --saveworld | aha --no-header && docker kill ark"
        elif data.get('cmd') == "kick":
            player_id = data.get('player_id')
            cmd = f"docker exec -i ark arkmanager rconcmd \"kickplayer {player_id}\" | aha --no-header"
        elif data.get('cmd') == "cancelshutdown":
            cmd = f"docker exec -i ark arkmanager cancelshutdown | aha --no-header"
        elif data.get('cmd') == "logs":
            cmd = f"docker logs ark | aha --no-header"
        rval = []

        logger.debug('executing command %s', cmd)

        async for l in get_lines(cmd):
            rval.append(l.decode())
        await websocket.send_text("</br>".join(rval))


@app.websocket('/settings')
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
            with lock:
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
            with lock:
                am_settings.truncate()
                am_settings.insert_multiple(data.get('data'))

        await websocket.send_text(json.dumps(am_settings.all()))


@app.websocket('/gu_settings')
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
            with lock:
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


async def get_lines(shell_command):
    p = await asyncio.create_subprocess_shell(shell_command,
            stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    for l in (await p.communicate())[0].splitlines():
        yield l
