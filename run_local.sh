#!/bin/bash

docker container rm footage-archive
docker build --platform=linux/amd64 -t footage-archive .
docker run --name footage-archive --env-file .env -p 8051:8051 footage-archive