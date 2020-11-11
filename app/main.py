import asyncio
import time 
import json
import os

from dotenv import load_dotenv
load_dotenv(verbose=True)

from asyncio.subprocess import PIPE, STDOUT

from fastapi import FastAPI, Request, WebSocket, Depends, HTTPException, status, Form 
from starlette.websockets import WebSocketDisconnect
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

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

users = TinyDB('db.json') 
settings = users.table('settings')
am_settings = users.table('am_settings')
poll_status = users.table('poll_status')

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

poll_lock = asyncio.Lock()


def _now():
    return datetime.now().strftime("%h/%d/%Y  %I:%M:%S %p")

def _hash(new_payload, key="status"):
    database = poll_status.get(Query().key == key) or {}
    new_hash = hash("".join(new_payload))
    old_hash = hash("".join(database.get("payload") or ["checking..."]))
    return (new_hash == old_hash)


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
        "cards/start.html",
        "cards/players.html",
        "cards/commands.html",
        "cards/settings.html",
        "cards/am_settings.html",
        "cards/password.html"
    ]

    return templates.TemplateResponse("cards.html", {
        "request": request, 
        "cards": cards, "ws_endpoint": os.getenv('WS_ENDPOINT'), 
        "credentials": credentials
        })


async def websocket_poll(websocket, key="status"):
    Poll = Query()
    db_key = poll_status.get(Poll.key == key) or {}

    await websocket.accept()
    updated = db_key.get("updated") or [_now()]
    payload = db_key.get("payload") or ["checking..."]

    old_recent = "</br>".join(updated + payload)
    await websocket.send_text(old_recent)

    while True:
        await asyncio.sleep(5)
        
        await poll_lock.acquire()
        db_key = poll_status.get(Poll.key == key) or {}

        if not db_key or (time.time() - db_key["time"]) > 5:

            cmd = 'docker exec -i ark arkmanager status | aha --no-header'

            if key == "players":
                cmd =  "docker exec -i ark arkmanager rconcmd listplayers | aha --no-header"

            rval = []
            async for l in get_lines(cmd):
                rval.append(l.decode())

            if not _hash(rval, key):
                db_key["updated"] = [_now()]
                db_key["payload"] = rval
                poll_status.upsert({"key":key, "time":time.time(), "updated": db_key["updated"], "payload": db_key["payload"]}, Poll.key == key)
        poll_lock.release()

        updated = db_key.get("updated") or [_now()]
        payload = db_key.get("payload") or ["checking..."]

        recent = "</br>".join(updated + payload)

        if old_recent != recent:
            old_recent = recent
            try:
                print()
                await websocket.send_text("</br>".join(db_key["updated"] + db_key['payload']))
            except (WebSocketDisconnect, ConnectionClosedOK):
                await websocket.close()
                return


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
        try:
            data = await websocket.receive_text()
        except (WebSocketDisconnect, ConnectionClosedOK):
            await websocket.close()
            return
        data = json.loads(data)
        cmd = "docker ps"
        if data.get('cmd') == "start":
            cmd = f"{RUN_CMD} | aha --no-header"
            am_s = ""
            for d in am_settings.all():
                am_s += f"-e {d['key']}=\"{d['value']}\" "
            cmd = cmd.format(am_s)
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
        except (WebSocketDisconnect, ConnectionClosedOK):
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

            async for l in get_lines(cmd):
                logger.info(l)

        await websocket.send_text(json.dumps(settings.all()))


@app.websocket('/am_settings')
async def am_settings_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text(json.dumps(am_settings.all()))

    while True:
        try:
            data = await websocket.receive_text()
        except (WebSocketDisconnect, ConnectionClosedOK):
            await websocket.close()
            return
        data = json.loads(data)
        if data.get('cmd') == 'put':
            am_settings.truncate()
            am_settings.insert_multiple(data.get('data'))

        await websocket.send_text(json.dumps(am_settings.all()))


async def get_lines(shell_command):
    p = await asyncio.create_subprocess_shell(shell_command,
            stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    for l in (await p.communicate())[0].splitlines():
        yield l
