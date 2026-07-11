---
name: splunk-queries
description: Common bounded SPL patterns to use before inventing custom Splunk queries in incident and alert workflows
---

# Splunk Query Patterns

Reference SPL patterns for common incident investigation scenarios. Use this skill before inventing custom SPL from scratch.

## Core Rule

- Start from the closest existing pattern, then specialize it with the service, identifiers, and time bounds from the task
- Keep all searches time-bounded and result-limited
- If one bounded query returns nothing, broaden once and then stop that branch

## General Patterns

### Error Rate Trend
```spl
index=app service={SERVICE} level=ERROR earliest={START} latest={END}
| timechart span=5m count as error_count
```

### Error Breakdown by Type
```spl
index=app service={SERVICE} level=ERROR earliest=-1h
| rex field=message "(?<error_type>[A-Z][a-zA-Z]+Exception|[A-Z][a-zA-Z]+Error)"
| stats count by error_type
| sort -count
| head 20
```

### Latency Analysis
```spl
index=app service={SERVICE} endpoint=* duration_ms=* earliest=-1h
| stats avg(duration_ms) as avg_ms, p50(duration_ms) as p50, p95(duration_ms) as p95, p99(duration_ms) as p99, count by endpoint
| sort -p99
```

### Recent Deployments
```spl
index=deploy service={SERVICE} earliest=-4h
| table _time, version, deployer, status, environment
| sort -_time
```

## Service-Specific Patterns

### Payment Gateway
```spl
# Connection pool exhaustion
index=app service=payment-gateway ("pool exhausted" OR "connection timeout" OR "no available connections") earliest=-2h
| timechart span=1m count

# Failed transactions
index=app service=payment-gateway transaction_status=FAILED earliest=-1h
| stats count by error_code, payment_provider
| sort -count

# Downstream dependency latency
index=app service=payment-gateway caller="payment-gateway" earliest=-1h
| stats avg(duration_ms) p99(duration_ms) by callee
```

### Auth Service
```spl
# Token validation failures
index=app service=auth-service ("token invalid" OR "validation failed" OR "expired token") earliest=-2h
| timechart span=5m count by error_reason

# LDAP connectivity issues
index=app service=auth-service ("LDAP" OR "ldap") ("timeout" OR "refused" OR "unreachable") earliest=-2h
| stats count by _time, error_type

# Certificate errors
index=app service=auth-service ("certificate" OR "SSL" OR "TLS") ("expired" OR "invalid" OR "untrusted") earliest=-2h
| stats count by error_detail
```

### Kafka Cluster
```spl
# Consumer lag
index=kafka group_id={CONSUMER_GROUP} earliest=-1h
| stats max(lag) as max_lag by topic, partition
| where max_lag > 1000
| sort -max_lag

# Consumer rebalance events
index=kafka ("rebalance" OR "JoinGroup" OR "SyncGroup" OR "LeaveGroup") earliest=-2h
| timechart span=5m count by event_type

# Broker health
index=kafka source_type=kafka_broker earliest=-1h
| stats latest(under_replicated_partitions) as urp, latest(active_controller_count) as controllers by broker_id
```

### Notification Service
```spl
# Failed notifications
index=app service=notification-service status=FAILED earliest=-1h
| stats count by channel, error_type
| sort -count

# Queue backlog
index=app service=notification-service "queue.size" earliest=-1h
| timechart span=5m max(queue_size) by channel
```

## Correlation Queries

### Cross-service error correlation
```spl
index=app level=ERROR earliest=-1h
| stats count by service
| sort -count
| head 10
```

### Timeline of events across services
```spl
index=app (level=ERROR OR level=WARN) service IN ({SERVICE1}, {SERVICE2}) earliest={START} latest={END}
| sort _time
| table _time, service, level, message
| head 200
```

## Query Best Practices

1. **Always use time bounds** (`earliest`/`latest`) — unbounded searches are slow and expensive
2. **Limit result sets** — use `| head N` or `| top N` to cap results
3. **Avoid wildcards in index** — always specify the index
4. **Use stats for aggregation** — prefer `| stats count by field` over viewing raw events
5. **Narrow before transform** — filter first, then aggregate
