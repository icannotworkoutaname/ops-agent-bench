from bench.kafka_lag import parse_describe, total_lag

# 取自真实集群的输出（2026-10-23），CONSUMER-ID 很长，用来确认按空白切分没有问题
REAL_OUTPUT = """
GROUP           TOPIC                 PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG             CONSUMER-ID                                                   HOST            CLIENT-ID
query-service   song-completion-stats 0          195912          195912          0               consumer-query-service-1-0404c9de-2bbb-490f-ab4f-ab9ab56a2341 /10.244.0.66    consumer-query-service-1
query-service   song-hourly-counts    0          71208           71208           7               consumer-query-service-2-97355735-1a95-433a-a318-1adc9e33420f /10.244.0.66    consumer-query-service-2
"""


def test_parses_real_output():
    rows = parse_describe(REAL_OUTPUT)
    assert len(rows) == 2
    assert rows[0].topic == "song-completion-stats"
    assert rows[1].lag == 7
    assert total_lag(rows) == 7


def test_no_active_members_still_parses():
    # 消费者组没有活跃成员时，后三列是 "-"
    output = (
        "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID HOST CLIENT-ID\n"
        "g play-events 0 100 250 150 - - -\n"
    )
    rows = parse_describe(output)
    assert total_lag(rows) == 150


def test_unknown_lag_is_none_and_not_counted():
    output = (
        "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID HOST CLIENT-ID\n"
        "g play-events 0 - 250 - - - -\n"
        "g play-events 1 10 20 10 - - -\n"
    )
    rows = parse_describe(output)
    assert rows[0].lag is None
    assert total_lag(rows) == 10


def test_error_text_or_missing_group_returns_empty():
    assert parse_describe("") == []
    assert parse_describe("Error: Consumer group 'x' does not exist.") == []
