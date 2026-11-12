#!/bin/bash
cd /home/user/oceandata/agent
date -u +"start %FT%TZ"
go version
go test -race -count=1 ./... 
echo "go test exit=$?"
date -u +"end %FT%TZ"
