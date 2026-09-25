#!/usr/bin/env bash
# Execute Flyway against SQL files packaged in the selected immutable artifact.
set -euo pipefail
bundle=${1:?release bundle required}
registry=${2:?registry host required}
: "${MEDW_SQL_SERVER:?required}" "${MEDW_SQL_DATABASE:?required}" "${MEDW_SQL_ACCESS_TOKEN:?required}"
digest=$(jq -er '.images.generation.digest' "$bundle")
source_sha=$(jq -er '.images.generation.source_sha' "$bundle")
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ && "$source_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$MEDW_SQL_SERVER$MEDW_SQL_DATABASE" != *';'* && "$MEDW_SQL_SERVER$MEDW_SQL_DATABASE" != *$'\n'* ]]
temporary=$(mktemp -d)
container=
trap 'if [[ -n "$container" ]]; then docker rm "$container" >/dev/null; fi; rm -rf "$temporary"' EXIT
image="$registry/generation@$digest"
docker pull "$image"
[[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}') == "$source_sha" ]]
container=$(docker create "$image")
docker cp "$container:/app/db" "$temporary/db"
export FLYWAY_JDBC_PROPERTIES_accessToken="$MEDW_SQL_ACCESS_TOKEN"
export FLYWAY_URL="jdbc:sqlserver://$MEDW_SQL_SERVER:1433;databaseName=$MEDW_SQL_DATABASE;encrypt=true;trustServerCertificate=false"
docker run --rm --env FLYWAY_JDBC_PROPERTIES_accessToken --env FLYWAY_URL \
  --mount "type=bind,src=$temporary/db/sql,dst=/flyway/sql,readonly" \
  --mount "type=bind,src=$temporary/db/flyway.conf,dst=/flyway/conf/flyway.conf,readonly" \
  redgate/flyway:13.7.0@sha256:031f7127435cdfcf3aa477b15c88fdd4a42145395d314914426f28dc65c10bfd \
  -configFiles=/flyway/conf/flyway.conf migrate info
