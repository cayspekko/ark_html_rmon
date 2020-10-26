import asyncio
import time 
import json
import os
import secrets

from dotenv import load_dotenv
load_dotenv(verbose=True)

from asyncio.subprocess import PIPE, STDOUT

from fastapi import FastAPI, Request, WebSocket, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime

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
        --name ark \
        thmhoag/arkserver"""



app = FastAPI()

app.mount("/static", StaticFiles(directory="static", html=True), name="static")

templates = Jinja2Templates(directory="templates")

security = HTTPBasic()

database = {}
users = {"ripp": "1mth3b3st", "cayspekko": "1mth3b3st"}

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
    for u, p in users.items():
        correct_username = secrets.compare_digest(credentials.username, u)
        correct_password = secrets.compare_digest(credentials.password, p)
        if correct_username and correct_password:
            break
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"}
        )

    cards = [
        "cards/default.html",
        "cards/status.html",
        "cards/players.html",
        "cards/start.html",
    ]
    return templates.TemplateResponse("cards.html", {"request": request, "cards": cards, "ws_endpoint": os.getenv('WS_ENDPOINT')})


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
        rval = []
        async for l in get_lines(cmd):
            rval.append(l.decode())
        await websocket.send_text("</br>".join(rval))


async def get_lines(shell_command):
    p = await asyncio.create_subprocess_shell(shell_command,
            stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    for l in (await p.communicate())[0].splitlines():
        yield l
