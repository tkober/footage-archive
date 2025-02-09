#!/bin/bash

docker run -d --name footage-archive -p 8051:8051 -v /mnt/user/backup/:/backup footage-archive-unraid
