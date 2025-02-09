#!/bin/bash

docker container rm footage-archive-unraid
docker build --platform=linux/amd64 -t footage-archive-unraid .
docker save -o ~/Desktop/footage-archive-unraid.tar footage-archive-unraid
