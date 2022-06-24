FROM tiangolo/uvicorn-gunicorn-fastapi

RUN apt update && apt -y install gcc make

RUN wget https://github.com/theZiz/aha/archive/0.5.1.tar.gz \
  && tar xzvf 0.5.1.tar.gz && cd aha-0.5.1 && make && cp aha /usr/local/bin

RUN pip3 install fastapi uvicorn aiofiles jinja2==3.0.3 python-dotenv tinydb python-multipart passlib bcrypt filelock

RUN wget https://github.com/vi/websocat/releases/download/v1.6.0/websocat_1.6.0_ssl1.1_amd64.deb
RUN dpkg -i websocat_1.6.0_ssl1.1_amd64.deb

ENV DOCKERVERSION=19.03.9
RUN wget https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKERVERSION}.tgz \
  && tar xzvf docker-${DOCKERVERSION}.tgz --strip 1 \
                 -C /usr/local/bin docker/docker \
  && rm docker-${DOCKERVERSION}.tgz

COPY ./app /app
