# html_rmon

Originally used for controlling ark using a web interface. Has expanded to include valheim. html_rmon is just a simple 
web interface for doing various configurations as well as stopping starting servers.  html_rmon assumes the servers run in
docker and controls docker through the docker socket. Use certbot to create a cert folder and run over https. Use a command like:

```
docker build . -t html_rmon
docker run -p 8888:443 -e GUNICORN_CMD_ARGS="--keyfile=/secrets/privkey.pem --certfile=/secrets/fullchain.pem" -e PORT=443 -v `pwd`/cert:/secrets -v /var/run/docker.sock:/var/run/docker.sock -v `pwd`/app/db.json:/app/db.json -itd --rm --name html_rmon html_rmon
```
