#!/usr/bin/env sh
set -eu

NAMESPACE="${NAMESPACE:-mq-kafka-lab}"
ROUTE_CODE="${ROUTE_CODE:-smoke-kafka-to-mq}"
KAFKA_TOPIC="${KAFKA_TOPIC:-smoke-kafka-to-mq}"
MQ_REQUEST_QUEUE="${MQ_REQUEST_QUEUE:-SMOKE.KAFKA.TO.MQ.REQUEST}"
MQ_RESPONSE_QUEUE="${MQ_RESPONSE_QUEUE:-SMOKE.KAFKA.TO.MQ.RESPONSE}"
PAYLOAD_TYPE="${PAYLOAD_TYPE:-TEXT}"
RETRY_ATTEMPTS="${RETRY_ATTEMPTS:-3}"
RETRY_BACKOFF_MS="${RETRY_BACKOFF_MS:-1000}"
DLQ_NAME="${DLQ_NAME:-SMOKE.KAFKA.TO.MQ.DLQ}"

kubectl_ns() {
  kubectl --namespace "${NAMESPACE}" "$@"
}

wait_for_rollout() {
  resource="$1"
  kubectl_ns rollout status "${resource}" --timeout=180s
}

wait_for_pod_ready() {
  selector="$1"
  kubectl_ns wait --for=condition=Ready pod -l "${selector}" --timeout=180s
}

ensure_kafka_topic() {
  kubectl_ns exec kafka-0 -- kafka-topics \
    --bootstrap-server kafka:9092 \
    --create \
    --if-not-exists \
    --topic "${KAFKA_TOPIC}" \
    --partitions 1 \
    --replication-factor 1
}

ensure_artemis_queue() {
  queue_name="$1"
  kubectl_ns exec activemq-0 -- env MQ_QUEUE="${queue_name}" sh -ec '
    artemis="/var/lib/artemis-instance/bin/artemis"
    output="$("${artemis}" queue create \
      --silent \
      --name "${MQ_QUEUE}" \
      --address "${MQ_QUEUE}" \
      --anycast \
      --durable \
      --auto-create-address \
      --user "${ARTEMIS_USER}" \
      --password "${ARTEMIS_PASSWORD}" 2>&1)" && {
        echo "Artemis queue is ready: ${MQ_QUEUE}"
        exit 0
      }

    if "${artemis}" queue stat \
      --user "${ARTEMIS_USER}" \
      --password "${ARTEMIS_PASSWORD}" \
      --queueName "${MQ_QUEUE}" 2>/dev/null | grep -F "${MQ_QUEUE}" >/dev/null; then
      echo "Artemis queue already exists: ${MQ_QUEUE}"
      exit 0
    fi

    printf "%s\n" "${output}" >&2
    exit 1
  '
}

purge_artemis_queue() {
  queue_name="$1"
  kubectl_ns exec activemq-0 -- env MQ_QUEUE="${queue_name}" sh -ec '
    artemis="/var/lib/artemis-instance/bin/artemis"
    "${artemis}" queue purge \
      --silent \
      --name "${MQ_QUEUE}" \
      --user "${ARTEMIS_USER}" \
      --password "${ARTEMIS_PASSWORD}" >/dev/null
  '
}

upsert_route_configuration() {
  kubectl_ns exec -i postgres-0 -- psql \
    -U mq_kafka \
    -d mq_kafka \
    -v ON_ERROR_STOP=1 \
    -v route_code="${ROUTE_CODE}" \
    -v kafka_topic="${KAFKA_TOPIC}" \
    -v mq_request_queue="${MQ_REQUEST_QUEUE}" \
    -v mq_response_queue="${MQ_RESPONSE_QUEUE}" \
    -v payload_type="${PAYLOAD_TYPE}" \
    -v retry_attempts="${RETRY_ATTEMPTS}" \
    -v retry_backoff_ms="${RETRY_BACKOFF_MS}" \
    -v dlq_name="${DLQ_NAME}" <<'SQL'
INSERT INTO integration_route (
  code,
  direction,
  mq_request_queue_name,
  kafka_topic_name,
  mq_response_queue_name,
  status,
  payload_type,
  retry_attempts,
  retry_backoff_ms,
  dlq_name,
  additional_properties
)
VALUES (
  :'route_code',
  'KAFKA_TO_MQ',
  :'mq_request_queue',
  :'kafka_topic',
  :'mq_response_queue',
  'ACTIVE',
  :'payload_type',
  :'retry_attempts',
  :'retry_backoff_ms',
  :'dlq_name',
  '{"managedBy":"k3s-smoke-test"}'
)
ON CONFLICT (code) DO UPDATE SET
  direction = EXCLUDED.direction,
  mq_request_queue_name = EXCLUDED.mq_request_queue_name,
  kafka_topic_name = EXCLUDED.kafka_topic_name,
  mq_response_queue_name = EXCLUDED.mq_response_queue_name,
  status = EXCLUDED.status,
  payload_type = EXCLUDED.payload_type,
  retry_attempts = EXCLUDED.retry_attempts,
  retry_backoff_ms = EXCLUDED.retry_backoff_ms,
  dlq_name = EXCLUDED.dlq_name,
  additional_properties = EXCLUDED.additional_properties;
SQL
}

echo "Checking mq-kafka-lab workloads in namespace ${NAMESPACE}..."
wait_for_pod_ready "app.kubernetes.io/name=kafka"
wait_for_pod_ready "app.kubernetes.io/name=postgres"
wait_for_pod_ready "app.kubernetes.io/name=activemq"
wait_for_pod_ready "app.kubernetes.io/name=mq-kafka-integration-service"

echo "Creating Kafka topic ${KAFKA_TOPIC}..."
ensure_kafka_topic

echo "Creating Artemis queues..."
ensure_artemis_queue "${MQ_REQUEST_QUEUE}"
ensure_artemis_queue "${MQ_RESPONSE_QUEUE}"
ensure_artemis_queue "${DLQ_NAME}"

echo "Purging smoke queues..."
purge_artemis_queue "${MQ_REQUEST_QUEUE}"
purge_artemis_queue "${MQ_RESPONSE_QUEUE}"
purge_artemis_queue "${DLQ_NAME}"

echo "Upserting route configuration ${ROUTE_CODE} into PostgreSQL..."
upsert_route_configuration

echo "Restarting integration service so it reloads route configuration from DB..."
kubectl_ns rollout restart deployment/mq-kafka-integration-service
wait_for_rollout "deployment/mq-kafka-integration-service"

echo "Smoke environment is ready:"
echo "  route=${ROUTE_CODE}"
echo "  kafkaTopic=${KAFKA_TOPIC}"
echo "  mqRequestQueue=${MQ_REQUEST_QUEUE}"
echo "  mqResponseQueue=${MQ_RESPONSE_QUEUE}"
