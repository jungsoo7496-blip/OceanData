#!/bin/bash
# usage: launch.sh NAME CMD...   (writes the real PID of the exec'd process to $E2E_DIR/NAME.pid, logs to logs/NAME.log)
source /tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/e2e12/env.sh
name=$1; shift
cd "${LAUNCH_CWD:-/home/user/oceandata/backend}"
exec setsid -f bash -c 'echo $$ > "'$E2E_DIR'/'$name'.pid"; exec "$@" > "'$E2E_DIR'/logs/'$name'.log" 2>&1 < /dev/null' _ "$@"
