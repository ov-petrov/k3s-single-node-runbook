# Smoke test: Kafka -> ActiveMQ через integration service

Инструкция описывает проверку учебного стенда `mq-kafka-lab` в K3s.
Проверяется не Kafka Connect, а runtime интеграционного сервиса:

```text
PostgreSQL route config -> integration service -> Kafka topic -> ActiveMQ queue
```

PostgreSQL в этом сценарии хранит конфигурацию маршрута. Тестовое сообщение
публикуется в Kafka, после чего `mq-kafka-integration-service` читает topic и
отправляет payload в ActiveMQ Artemis request queue через `JmsTemplate`.

## Что должно быть развернуто

Перед smoke-тестом в namespace `mq-kafka-lab` должны быть готовы:

- `kafka-0`;
- `postgres-0`;
- `activemq-0`;
- `deployment/mq-kafka-integration-service`.

Проверка:

```bash
kubectl get pods --namespace mq-kafka-lab
```

Все Pods должны быть `Running` и `READY 1/1`.

## Как устроен маршрут

Сервис загружает активные маршруты из таблицы `integration_route` только при
старте приложения. Поэтому после изменения route-конфигурации нужно
перезапустить Deployment сервиса.

Для smoke-теста используется маршрут:

| Поле | Значение по умолчанию |
| --- | --- |
| `code` | `smoke-kafka-to-mq` |
| `direction` | `KAFKA_TO_MQ` |
| `kafka_topic_name` | `smoke-kafka-to-mq` |
| `mq_request_queue_name` | `SMOKE.KAFKA.TO.MQ.REQUEST` |
| `mq_response_queue_name` | `SMOKE.KAFKA.TO.MQ.RESPONSE` |
| `status` | `ACTIVE` |
| `payload_type` | `TEXT` |

`KAFKA_TO_MQ` читает Kafka topic consumer group-ой
`mq-kafka-integration-service.<route-code>` и отправляет `TextMessage` в
request queue. В `JMSReplyTo` сервис указывает response queue.

## Подготовка окружения

Скрипт подготовки:

```bash
./script/register-connectors.sh
```

Несмотря на историческое имя файла, Kafka Connect в K3s-стенде не используется.
Скрипт выполняет следующие действия:

1. Проверяет готовность pod'ов Kafka, PostgreSQL, ActiveMQ и сервиса.
2. Создает Kafka topic.
3. Создает ActiveMQ Artemis queues.
4. Очищает smoke queues.
5. Создает или обновляет строку маршрута в `integration_route`.
6. Перезапускает `deployment/mq-kafka-integration-service`, чтобы сервис
   загрузил маршрут из БД.

Параметры можно переопределить через переменные окружения:

```bash
NAMESPACE=mq-kafka-lab \
ROUTE_CODE=smoke-kafka-to-mq \
KAFKA_TOPIC=smoke-kafka-to-mq \
MQ_REQUEST_QUEUE=SMOKE.KAFKA.TO.MQ.REQUEST \
MQ_RESPONSE_QUEUE=SMOKE.KAFKA.TO.MQ.RESPONSE \
./script/register-connectors.sh
```

## Запуск smoke-теста

```bash
./script/smoke-test-jdbc-source.sh
```

Скрипт:

1. Повторно вызывает подготовку окружения, поэтому его можно запускать
   самостоятельно.
2. Показывает route-конфигурацию из PostgreSQL.
3. Очищает request queue.
4. Публикует уникальное сообщение в Kafka topic.
5. Читает одно сообщение из ActiveMQ request queue через
   `artemis consumer --data` во временный XML-файл внутри pod.
6. Считает тест успешным, если payload содержит текущий `TEST_ID`.

Успешный результат:

```text
Smoke test passed: Kafka message smoke-... reached MQ queue SMOKE.KAFKA.TO.MQ.REQUEST.
```

Для проверки payload используется XML dump Artemis CLI. Это сделано
намеренно: обычный `artemis consumer` без `--data` не печатает тело сообщения,
а вариант с `--verbose` выводит командную строку с параметрами подключения.

## Ручные проверки

Route в БД:

```bash
kubectl exec --namespace mq-kafka-lab postgres-0 -- \
  psql -U mq_kafka -d mq_kafka \
  -c "select code, direction, kafka_topic_name, mq_request_queue_name, mq_response_queue_name, status from integration_route;"
```

Kafka topic:

```bash
kubectl exec --namespace mq-kafka-lab kafka-0 -- \
  kafka-topics --bootstrap-server kafka:9092 --list
```

Consumer group маршрута:

```bash
kubectl exec --namespace mq-kafka-lab kafka-0 -- \
  kafka-consumer-groups \
    --bootstrap-server kafka:9092 \
    --describe \
    --group mq-kafka-integration-service.smoke-kafka-to-mq
```

Статистика request queue:

```bash
kubectl exec --namespace mq-kafka-lab activemq-0 -- \
  sh -ec '/var/lib/artemis-instance/bin/artemis queue stat \
    --user "$ARTEMIS_USER" \
    --password "$ARTEMIS_PASSWORD" \
    --queueName SMOKE.KAFKA.TO.MQ.REQUEST'
```

После успешного smoke-теста `MESSAGE COUNT` обычно равен `0`, потому что
скрипт вычитывает сообщение для проверки payload. При этом `MESSAGES ADDED` и
`MESSAGES ACKED` должны увеличиться.

Логи сервиса:

```bash
kubectl logs --namespace mq-kafka-lab \
  deployment/mq-kafka-integration-service \
  --tail=200
```

В логах после перезапуска должна быть строка вида:

```text
KAFKA_TO_MQ route started: code=smoke-kafka-to-mq
```

## Частые проблемы

Если route есть в БД, но сервис не читает Kafka topic, перезапустите Deployment:

```bash
kubectl rollout restart deployment/mq-kafka-integration-service \
  --namespace mq-kafka-lab
kubectl rollout status deployment/mq-kafka-integration-service \
  --namespace mq-kafka-lab
```

Если smoke-тест не находит сообщение в MQ, проверьте:

- route имеет `status = ACTIVE`;
- Deployment перезапущен после изменения `integration_route`;
- в логах есть `KAFKA_TO_MQ route started`;
- consumer group имеет assigned partition;
- request queue существует и доступна в Artemis.

Если нужно запустить независимый тест с другими именами:

```bash
ROUTE_CODE=smoke-kafka-to-mq-2 \
KAFKA_TOPIC=smoke-kafka-to-mq-2 \
MQ_REQUEST_QUEUE=SMOKE.KAFKA.TO.MQ.REQUEST.2 \
MQ_RESPONSE_QUEUE=SMOKE.KAFKA.TO.MQ.RESPONSE.2 \
./script/smoke-test-jdbc-source.sh
```
