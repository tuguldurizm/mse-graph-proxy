#!/bin/bash
# Starts Neo4j (localhost only), runs the data pipeline in the background and
# serves the web app on $PORT. Memory is tuned for a 512 MB instance.
#
# With NEO4J_URI (+ NEO4J_USERNAME / NEO4J_PASSWORD) pointing to an external
# database such as Neo4j Aura, the bundled Neo4j is not started.
set -euo pipefail

if [[ -n "${NEO4J_URI:-}" && ! "$NEO4J_URI" =~ ://(localhost|127\.0\.0\.1)(:|/|$) ]]; then
  # External database (e.g. Neo4j Aura): the graph and its history survive restarts.
  : "${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set when NEO4J_URI points to an external database}"
  export NEO4J_URI NEO4J_PASSWORD
  echo "[start] using external neo4j at ${NEO4J_URI}"
else
# Bundled database: lives in the container and is lost when the instance restarts.
: "${NEO4J_PASSWORD:=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)}"
export NEO4J_PASSWORD NEO4J_USER=neo4j NEO4J_URI=bolt://127.0.0.1:7687

CONF="$NEO4J_HOME/conf/neo4j.conf"
if ! grep -q "# mse-graph" "$CONF"; then
  # drop stock values we override (duplicate keys are a startup error)
  sed -i -E '/AlwaysPreTouch|UseG1GC|^#?server\.(default_listen_address|http\.enabled|memory\.)|^#?dbms\.memory\.transaction/d' "$CONF"
  cat >> "$CONF" <<EOF
# mse-graph
server.default_listen_address=127.0.0.1
server.http.enabled=false
server.memory.heap.initial_size=${NEO4J_HEAP:-144m}
server.memory.heap.max_size=${NEO4J_HEAP:-144m}
server.memory.pagecache.size=${NEO4J_PAGECACHE:-24m}
dbms.memory.transaction.total.max=48m
server.bolt.thread_pool_max_size=8
server.jvm.additional=-XX:+UseSerialGC
server.jvm.additional=-XX:ActiveProcessorCount=1
server.jvm.additional=-XX:TieredStopAtLevel=1
server.jvm.additional=-XX:CICompilerCount=1
server.jvm.additional=-XX:ReservedCodeCacheSize=32m
server.jvm.additional=-XX:MaxMetaspaceSize=128m
server.jvm.additional=-XX:MaxDirectMemorySize=24m
server.jvm.additional=-Xss256k
EOF
fi

if [ ! -f /data/.password-set ]; then
  neo4j-admin dbms set-initial-password "$NEO4J_PASSWORD" >/dev/null
  touch /data/.password-set
fi

echo "[start] launching neo4j"
# Run the server JVM directly: `neo4j console` would keep a ~190 MB launcher JVM alive.
NEO4J_CMD=$(neo4j console --dry-run 2>/dev/null | tail -n 1)
eval "$NEO4J_CMD" > /tmp/neo4j.log 2>&1 &
NEO4J_PID=$!
( while kill -0 "$NEO4J_PID" 2>/dev/null; do sleep 5; done
  echo "[start] neo4j exited; last log lines:"; tail -n 40 /tmp/neo4j.log ) &
fi

# The data pipeline runs in a thread inside the web app (saves a Python process).
export RUN_PIPELINE=1

echo "[start] launching web app on port ${PORT:-8000}"
exec uvicorn mse_graph.api:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --no-access-log
