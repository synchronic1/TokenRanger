#!/usr/bin/env bash
# TokenRanger Model Benchmark Script
# Runs compression quality + latency tests against Ollama on pvet630
# Usage: ./scripts/benchmark-models.sh [OLLAMA_URL]
set -euo pipefail

OLLAMA_URL="${1:-http://192.168.1.242:11434}"

# 5-turn Discord bot setup conversation (same payload as TESTING.md section 2)
SYSTEM_PROMPT="You are a context compressor for a multi-turn conversation. Each turn is tagged as [T<n>:<role>|<size>] or [T<n>:<role>|<size>|code] (the |code flag means code blocks were stripped before compression). Extract only: key decisions made, current state of work, open questions, and user preferences. Discard greetings, pleasantries, and redundant information. Summarize as factual state. Do NOT use first-person ('I will...', 'I\\'ll...', 'Let me...'). IMPORTANT: Preserve T1, T2 nearly verbatim — these contain the user\\'s original instructions and constraints. Summarize as factual state. Do NOT use first-person ('I\\'ll...', 'I will...'). Output as: '[T<n>] bullet summary' for each turn."

# Short conversation (3 turns, ~800 chars) — tests instruction following on sparse input
SHORT_INPUT="[T1:user|210c] I need a Discord bot that monitors a channel for new messages and forwards them to a webhook. It should only forward messages from non-bot users and include the author name and timestamp.

[T2:asst|320c] I'll help you build that Discord bot. Here are the key decisions: 1) We'll use discord.js v14 for the bot framework. 2) Messages will be filtered to exclude bot authors. 3) Each forwarded message will include author displayName and ISO timestamp. 4) The webhook URL will be stored in an environment variable. Should I start with the message handler?

[T3:user|270c] Yes, start with the handler. Also add rate limiting — max 10 messages per minute to the webhook. And log any failed webhook calls to a file instead of just console."

# Medium conversation (5 turns, ~2000 chars) — tests summarization quality
MEDIUM_INPUT="[T1:user|350c] I'm building a home automation system using Zigbee devices. I have a Sonoff Zigbee coordinator on a Raspberry Pi 4 running Zigbee2MQTT connected to Home Assistant via MQTT. I need to set up 12 Aqara temperature sensors across 4 rooms (3 per room) and create automations that average the readings per room to control the HVAC zones independently.

[T2:asst|480c] Great setup! Here's the plan: 1) Pair all 12 Aqara WSDCGQ11LM sensors via Zigbee2MQTT — they report temp every 60s or on 0.5°C change. 2) Create template sensors in Home Assistant that average 3 readings per room. 3) Use the climate integration with your HVAC zone controller. Key decisions: naming convention \`sensor.temp_{room}_{position}\` where position is wall/ceiling/floor. The Aqara sensors have a range of -20°C to 60°C with ±0.3°C accuracy. Battery life ~2 years with CR2032. Questions: What HVAC zone controller are you using? Do you want humidity averaging too?

[T3:user|290c] The HVAC controller is a Honeywell TrueZONE HZ432. I want humidity averaging too — the Aqara sensors report both. For the template sensors, use a weighted average: ceiling sensor gets 50% weight, wall gets 30%, floor gets 20%. This accounts for heat stratification.

[T4:asst|420c|code] Here's the template sensor configuration for the master bedroom:

Configured weighted average template sensor for master_bedroom using the 50/30/20 weighting scheme. Also set up humidity averaging with equal weights since humidity stratification is minimal. Created automation blueprint for HVAC zone control with configurable deadband (default ±0.5°C) and minimum cycle time (5 minutes) to prevent short cycling.

[T5:user|310c] The weighted average looks good. Two changes: 1) Add a stale sensor check — if any sensor hasn't reported in 10 minutes, exclude it from the average and send a notification. 2) Set the deadband to ±1.0°C instead of ±0.5°C — the TrueZONE dampers are slow so we need a wider band."

# Long conversation (8 turns, ~5000 chars) — tests scaling and factual density
LONG_INPUT="[T1:user|420c] I need to migrate our PostgreSQL 14 database from a single-node setup on an EC2 r6g.xlarge to a 3-node Patroni cluster on r6g.2xlarge instances. The database is 850GB with 120 tables, ~40 of which have heavy write traffic (5000+ TPS). We use logical replication for a read replica that feeds our analytics pipeline. Requirements: zero-downtime migration, maintain the logical replication slot, and keep WAL archiving to S3 intact.

[T2:asst|550c] This is a complex migration. Here's my analysis: The 850GB database with 5000+ TPS on hot tables means we need careful WAL management during the transition. Key constraints: 1) Patroni uses streaming replication internally, which is separate from your existing logical replication. 2) The logical replication slot must survive the leader transition. 3) WAL archiving needs to be configured on all 3 nodes but only active on the leader. Proposed approach: a) Set up Patroni cluster with empty nodes first. b) Use pg_basebackup from current primary to seed the Patroni leader. c) Configure Patroni to manage the existing logical replication slot. d) Cut over DNS/connection string. Questions: What's your current WAL volume per hour? What PgBouncer configuration are you using?

[T3:user|380c] WAL volume is about 15GB/hour during peak. We use PgBouncer in transaction mode with max_client_conn=200, default_pool_size=25. The analytics logical replication consumer is a Debezium connector feeding Kafka. One constraint I forgot: we have 3 foreign data wrapper connections to other databases that need to stay working through the migration.

[T4:asst|500c] 15GB/hour WAL is significant — that's ~4.2MB/s sustained. During migration we'll need to ensure the standby nodes can keep up with replication lag. For the Debezium/Kafka consumer: Patroni can preserve logical replication slots using the \`slots\` configuration, but there's a known issue where logical slots don't automatically follow failover in PG14. We'll need the \`patroni.permanent_replication_slots\` feature. For FDW connections: these are configured in pg_catalog and will be copied with pg_basebackup. However, if the FDW targets use IP-based pg_hba.conf rules, we need to update those for the new node IPs. PgBouncer: deploy one instance per Patroni node, with a consul-template or callback script that updates the primary endpoint on failover.

[T5:user|290c] Good catch on the logical slot failover issue. Let's use Patroni 3.x which has native logical slot management. For PgBouncer, I'd prefer HAProxy in front of the Patroni REST API for leader detection rather than consul-template. We already have HAProxy for other services.

[T6:asst|380c] HAProxy with Patroni REST API health checks is cleaner than consul-template. Configuration: primary detection via HTTP check on Patroni port 8008 with expected response 200 from /primary endpoint. Standby routing via /replica endpoint for read traffic. HAProxy config needs: inter 2000, fall 3, rise 2 for the health check. Backend server definitions for all 3 nodes. Important: set timeout server to match your longest query timeout (PgBouncer query_timeout), otherwise HAProxy will kill long-running analytics queries.

[T7:user|350c] One more thing: we need to handle the sequences. We have 15 tables using bigserial primary keys with heavy inserts. During the cut-over window, we need to ensure no duplicate key conflicts. My plan: 1) Pause application writes. 2) Note current sequence values. 3) Set sequences on new cluster to current + 10000 gap. 4) Resume writes on new cluster. The gap handles any in-flight transactions.

[T8:asst|420c] The sequence gap approach works but 10000 may not be enough at 5000 TPS — a 2-second delay could consume the gap. Safer approach: 1) Pause writes at application layer (return 503). 2) Wait for all in-flight transactions to complete (pg_stat_activity). 3) Run SELECT setval() on new cluster using exact current values from old primary. 4) Resume writes. Since pg_basebackup copies sequences, they should match unless there were writes between basebackup and cutover. Alternative: use uuid_generate_v7() instead of bigserial for new inserts during the migration window — no conflict possible."

# Models to benchmark (test all available + the new ones)
MODELS=("qwen3:8b" "qwen3:4b" "qwen3:1.7b" "mistral:7b-instruct" "qwen2.5:7b" "llama3.1:8b" "llama3.2:3b")

echo "================================================================"
echo "  TokenRanger Model Benchmark"
echo "  Ollama: $OLLAMA_URL"
echo "  Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"
echo ""

# Check which models are available
echo "--- Available Models ---"
AVAILABLE=()
for model in "${MODELS[@]}"; do
    if curl -s "${OLLAMA_URL}/api/tags" | python3 -c "import sys,json; models=[m['name'] for m in json.load(sys.stdin)['models']]; sys.exit(0 if '${model}' in models else 1)" 2>/dev/null; then
        echo "  ✓ $model"
        AVAILABLE+=("$model")
    else
        echo "  ✗ $model (not pulled)"
    fi
done
echo ""

# Run benchmarks
for model in "${AVAILABLE[@]}"; do
    echo "================================================================"
    echo "  MODEL: $model"
    echo "================================================================"

    for label_input in "SHORT:$SHORT_INPUT" "MEDIUM:$MEDIUM_INPUT" "LONG:$LONG_INPUT"; do
        label="${label_input%%:*}"
        input="${label_input#*:}"
        input_chars=${#input}

        echo ""
        echo "--- $label ($input_chars chars) ---"

        # Build JSON payload
        payload=$(python3 -c "
import json, sys
msg = json.dumps({
    'model': '${model}',
    'messages': [
        {'role': 'system', 'content': sys.argv[1]},
        {'role': 'user', 'content': sys.argv[2]}
    ],
    'stream': False,
    'options': {'temperature': 0.1, 'num_ctx': 8192}
})
print(msg)
" "$SYSTEM_PROMPT" "$input")

        # Time the request
        start_ms=$(python3 -c "import time; print(int(time.time()*1000))")
        response=$(curl -s -X POST "${OLLAMA_URL}/api/chat" \
            -H "Content-Type: application/json" \
            -d "$payload" \
            --max-time 120 2>&1)
        end_ms=$(python3 -c "import time; print(int(time.time()*1000))")

        latency=$((end_ms - start_ms))

        # Parse response
        output_text=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',{}).get('content','ERROR'))" 2>/dev/null || echo "PARSE_ERROR")
        output_chars=${#output_text}
        eval_count=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('eval_count',0))" 2>/dev/null || echo "0")
        eval_duration=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('eval_duration',0))" 2>/dev/null || echo "0")
        prompt_eval_count=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('prompt_eval_count',0))" 2>/dev/null || echo "0")
        load_duration=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('load_duration',0))" 2>/dev/null || echo "0")

        # Calculate tokens/sec
        if [ "$eval_duration" -gt 0 ]; then
            tok_per_sec=$(python3 -c "print(round(${eval_count} / (${eval_duration} / 1e9), 1))")
        else
            tok_per_sec="N/A"
        fi

        # Check for first-person leakage
        first_person_count=$(echo "$output_text" | grep -c -iE "I'll |I will |Let me |I have |I've " || true)

        # Calculate reduction
        if [ "$input_chars" -gt 0 ]; then
            reduction=$(python3 -c "print(round((1 - ${output_chars}/${input_chars}) * 100, 1))")
        else
            reduction="N/A"
        fi

        echo "  Input:        ${input_chars}c (${prompt_eval_count} tokens)"
        echo "  Output:       ${output_chars}c (${eval_count} tokens)"
        echo "  Reduction:    ${reduction}%"
        echo "  Latency:      ${latency}ms"
        echo "  Tok/sec:      ${tok_per_sec}"
        echo "  Load time:    $(python3 -c "print(round(${load_duration}/1e6))")ms"
        echo "  1st-person:   ${first_person_count} violations"
        echo ""
        echo "  --- Output Preview (first 300 chars) ---"
        echo "$output_text" | head -c 300
        echo ""
        echo "  ---"
    done

    echo ""
done

echo ""
echo "================================================================"
echo "  Benchmark Complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"
