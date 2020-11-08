import asyncio
import time 
import json
import os

from dotenv import load_dotenv
load_dotenv(verbose=True)

from asyncio.subprocess import PIPE, STDOUT

from fastapi import FastAPI, Request, WebSocket, Depends, HTTPException, status, Form 
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from datetime import datetime
from tinydb import TinyDB, Query
from tinydb.operations import set as db_set

import logging
logger = logging.getLogger()

RUN_CMD = """#!/bin/bash
docker run -d --restart=always -v steam:/home/steam/Steam \
        -v ark:/ark \
        -p 7778:7778 -p 7778:7778/udp \
        -p 7777:7777 -p 7777:7777/udp \
        -p 27015:27015 -p 27015:27015/udp \
        -p 32330:32330 -p 32330:32330/udp \
        -e am_arkflag_crossplay=true \
        -e am_arkflag_NoBattlEye=true \
        -e am_ActiveMods=731604991,89384064 \
        -e am_HarvestAmountMultiplier=1.25000 \
        -e am_DayTimeSpeedScale=0.70000 \
        -e am_NightTimeSpeedScale=0.30000 \
        --name ark \
        thmhoag/arkserver"""

app = FastAPI()

app.mount("/static", StaticFiles(directory="static", html=True), name="static")

templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

users = TinyDB('db.json') 
settings = users.table('settings')
database = {}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _now():
    return datetime.now().strftime("%h/%d/%Y  %I:%M:%S %p")


def _hash(new_payload, key="status"):
    new_hash = hash("".join(new_payload))
    old_hash = hash("".join(database[key]["payload"]))
    return (new_hash == old_hash)


@app.on_event("startup")
async def startup_event():
    for key in ("status", "players"):
        database[key] = {"payload": ["checking..."],
                        "updated": [_now()]}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    User = Query()
    u = users.get(User.username == credentials.username)
    if not (u and pwd_context.verify(credentials.password, u['password'])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"}
        )

    cards = [
        "cards/default.html",
        "cards/status.html",
        "cards/players.html",
        "cards/commands.html",
        "cards/settings.html",
        "cards/start.html",
        "cards/password.html"
    ]

    return templates.TemplateResponse("cards.html", {
        "request": request, 
        "cards": cards, "ws_endpoint": os.getenv('WS_ENDPOINT'), 
        "credentials": credentials
        })


async def websocket_poll(websocket, key="status"):
    db_key = database[key]

    await websocket.accept()
    await websocket.send_text("</br>".join(db_key["updated"] + db_key['payload']))

    while True:
        cmd = 'docker exec -i ark arkmanager status | aha --no-header'

        if key == "players":
            cmd =  "docker exec -i ark arkmanager rconcmd listplayers | aha --no-header"

        rval = []
        async for l in get_lines(cmd):
            rval.append(l.decode())

        if not _hash(rval, key):
            db_key["updated"] = [_now()]
            db_key["payload"] = rval
        await websocket.send_text("</br>".join(db_key["updated"] + db_key['payload']))
        await asyncio.sleep(5)


@app.post("/change_password")
async def change_password(psw: str=Form(...),  credentials: HTTPBasicCredentials = Depends(security)):
    User = Query()
    new_pw = pwd_context.hash(psw)
    users.update(db_set('password', new_pw), User.username == credentials.username)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND) 


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
        data = await websocket.receive_text()
        data = json.loads(data)
        cmd = "docker ps"
        if data.get('cmd') == "start":
            cmd = f"{RUN_CMD} | aha --no-header"
        elif data.get('cmd') == "stop":
            cmd = f"docker exec -i ark arkmanager stop --saveworld | aha --no-header && docker kill ark && docker rm ark"
        elif data.get('cmd') == "kick":
            player_id = data.get('player_id')
            cmd = f"docker exec -i ark arkmanager rconcmd \"kickplayer {player_id}\" | aha --no-header"
        elif data.get('cmd') == "cancelshutdown":
            cmd = f"docker exec -i ark arkmanager cancelshutdown | aha --no-header"
        rval = []
        async for l in get_lines(cmd):
            rval.append(l.decode())
        await websocket.send_text("</br>".join(rval))


@app.websocket('/settings')
async def settings_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text(json.dumps(settings.all()))

    while True:
        data = await websocket.receive_text()
        data = json.loads(data)
        if data.get('cmd') == 'put':
            settings.truncate()
            settings.insert_multiple(data.get('data'))

            cmd = "docker run -i --rm -v ark:/ark --name ark_oneshot thmhoag/arkserver /ark/update_game_ini.sh "
            for setting in settings.all():
                cmd += f"{setting['key']}={setting['value']} "
            cmd += " | aha --no-header"

            async for l in get_lines(cmd):
                logger.info(l)

        await websocket.send_text(json.dumps(settings.all()))


async def get_lines(shell_command):
    p = await asyncio.create_subprocess_shell(shell_command,
            stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    for l in (await p.communicate())[0].splitlines():
        yield l
