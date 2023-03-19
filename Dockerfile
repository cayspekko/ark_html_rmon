FROM ubuntu:20.04 as BUILD

RUN apt-get update && apt-get install -y \
  gcc \
  make \
  wget \
  && rm -rf /var/lib/apt/lists/*

RUN wget https://github.com/theZiz/aha/archive/0.5.1.tar.gz \
  && tar xzvf 0.5.1.tar.gz && cd aha-0.5.1 && make && cp aha /usr/local/bin

ENV DOCKERVERSION=23.0.1
RUN wget https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKERVERSION}.tgz \
  && tar xzvf docker-${DOCKERVERSION}.tgz --strip 1 \
                 -C /usr/local/bin docker/docker \
  && rm docker-${DOCKERVERSION}.tgz

FROM tiangolo/uvicorn-gunicorn-fastapi

COPY --from=BUILD /usr/local/bin/aha /usr/local/bin/aha
COPY --from=BUILD /usr/local/bin/docker /usr/local/bin/docker

RUN pip3 install fastapi fastapi-utils uvicorn aiofiles jinja2==3.0.3 python-dotenv tinydb python-multipart passlib bcrypt filelock itsdangerous

COPY ./app /app
