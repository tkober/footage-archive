#!/bin/bash

docker container rm footage-archive

PIP_CONF=$(realpath ~/.pip/pip.conf)

echo $PIP_CONF

if [[ `uname -m` == 'arm64' ]];
then
  docker build --platform linux/arm64 --secret id=pip,src=$(echo $PIP_CONF) -t footage-archive . --no-cache
else
  docker build -t --secret id=pip,src=$PIP_CONF footage-archive .
fi

if [[ ! -f ../.env ]]; then
  docker run --name footage-archive -p 8051:8051 footage-archive
else
  docker run --name footage-archive -p 8051:8051 --env-file ../.env footage-archive
fi
