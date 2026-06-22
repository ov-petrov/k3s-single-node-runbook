#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

NAMESPACE="${NAMESPACE:-mq-kafka-lab}"
ROUTE_CODE="${ROUTE_CODE:-smoke-kafka-to-mq}"
KAFKA_TOPIC="${KAFKA_TOPIC:-smoke-kafka-to-mq}"
MQ_REQUEST_QUEUE="${MQ_REQUEST_QUEUE:-SMOKE.KAFKA.TO.MQ.REQUEST}"
MQ_RESPONSE_QUEUE="${MQ_RESPONSE_QUEUE:-SMOKE.KAFKA.TO.MQ.RESPONSE}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-60}"
TEST_ID="${TEST_ID:-smoke-$(date +%s)}"
PAYLOAD="${PAYLOAD:-{\"testId\":\"${TEST_ID}\",\"source\":\"k3s-smoke-test\",\"route\":\"${ROUTE_CODE}\"}}"

kubectl_ns() {
  kubectl --namespace "${NAMESPACE}" "$@"
}

purge_request_queue() {
  kubectl_ns exec activemq-0 -- env MQ_QUEUE="${MQ_REQUEST_QUEUE}" sh -ec '
    artemis="/var/lib/artemis-instance/bin/artemis"
    "${artemis}" queue purge \
      --silent \
      --name "${MQ_QUEUE}" \
      --user "${ARTEMIS_USER}" \
      --password "${ARTEMIS_PASSWORD}" >/dev/null
  '
}

produce_kafka_message() {
  printf '%s|%s\n' "${TEST_ID}" "${PAYLOAD}" | kubectl_ns exec -i kafka-0 -- \
    kafka-console-producer \
      --bootstrap-server kafka:9092 \
      --topic "${KAFKA_TOPIC}" \
      --property parse.key=true \
      --property key.separator='|'
}

consume_one_mq_message() {
  kubectl_ns exec activemq-0 -- env MQ_QUEUE="${MQ_REQUEST_QUEUE}" sh -ec '
    artemis="/var/lib/artemis-instance/bin/artemis"
    message_file="/tmp/k3s-smoke-message.xml"
    rm -f "${message_file}"

    timeout 20s "${artemis}" consumer \
      --break-on-null \
      --destination "queue://${MQ_QUEUE}" \
      --message-count 1 \
      --receive-timeout 5000 \
      --data "${message_file}" \
      --user "${ARTEMIS_USER}" \
      --password "${ARTEMIS_PASSWORD}" >/tmp/k3s-smoke-consumer.out 2>&1

    cat /tmp/k3s-smoke-consumer.out
    if [ -f "${message_file}" ]; then
      cat "${message_file}"
    fi
  ' 2>&1
}

print_route_configuration() {
  kubectl_ns exec -i postgres-0 -- psql \
    -U mq_kafka \
    -d mq_kafka \
    -v route_code="${ROUTE_CODE}" <<'SQL'
select
  code,
  direction,
  kafka_topic_name,
  mq_request_queue_name,
  mq_response_queue_name,
  status
from integration_route
where code = :'route_code';
SQL
}

echo "Preparing K3s smoke environment..."
"${SCRIPT_DIR}/register-connectors.sh"

echo "Route configuration in DB:"
print_route_configuration

echo "Purging request queue ${MQ_REQUEST_QUEUE}..."
purge_request_queue

echo "Producing Kafka message to ${KAFKA_TOPIC}: ${TEST_ID}"
produce_kafka_message

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
last_output=""
while [ "$(date +%s)" -lt "${deadline}" ]; do
  last_output="$(consume_one_mq_message || true)"
  if printf '%s' "${last_output}" | grep -F "${TEST_ID}" >/dev/null; then
    echo "Smoke test passed: Kafka message ${TEST_ID} reached MQ queue ${MQ_REQUEST_QUEUE}."
    echo "Payload:"
    printf '%s\n' "${PAYLOAD}"
    exit 0
  fi
  sleep 2
done

echo "Smoke test failed: message ${TEST_ID} was not received from MQ queue ${MQ_REQUEST_QUEUE}." >&2
echo "Last Artemis consumer output:" >&2
printf '%s\n' "${last_output}" >&2
echo "Recent integration service logs:" >&2
kubectl_ns logs deployment/mq-kafka-integration-service --tail=120 >&2 || true
exit 1
