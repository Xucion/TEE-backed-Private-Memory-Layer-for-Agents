# Unsolved Problem 2: Similar Memory Deduplication

## Background

The project currently supports extracting long-term user memories and storing them in an encrypted local memory store. Each memory is embedded with DashScope `text-embedding-v4`, and retrieval uses vector similarity.

A new issue appeared during testing:

1. The user says: "我有糖尿病"
2. The system stores: "用户有糖尿病"
3. Later, the user says: "我有糖尿疾病"
4. The system stores another memory: "用户有糖尿疾病"

These two memories refer to the same underlying user fact, but the system treats them as separate memories.

## Problem

The memory store can still save duplicate or near-duplicate memories when the wording is different.

Examples:

- "用户有糖尿病"
- "用户有糖尿疾病"
- "用户患有糖尿病"
- "用户血糖方面有糖尿病史"

These may all describe the same durable user fact, but a simple embedding threshold may fail to merge them consistently.

## Current Temporary Mitigation

The current short-term mitigation is to lower the embedding similarity threshold from `0.9` to `0.8`.

This may reduce some missed duplicates because more semantically similar memories will be treated as duplicates.

However, this is only a temporary mitigation, not a complete solution.

## Why Lowering The Threshold Is Not Enough

Lowering the threshold helps catch more similar text, but it introduces tradeoffs:

- It may still miss duplicates if the embedding model does not place two expressions close enough.
- It may merge memories that are related but not identical.
- It does not understand entity relationships or medical terminology.
- It cannot distinguish "用户有糖尿病" from "用户父亲有糖尿病" reliably in all cases.
- It does not create a stable identity for a memory fact.

Embedding similarity answers "are these texts close in vector space?" It does not reliably answer "are these the same long-term user fact?"

## Root Cause

The root cause is that the system stores natural-language memory text as the primary identity of a fact.

Currently, the memory store does not have a stable semantic key such as:

- `health.diabetes`
- `preference.food.porridge`
- `business.acquisition_chip_company`

Without a stable key or canonical form, two differently worded memories can only be compared with approximate similarity.

This makes deduplication fragile.

## Common Industry Approaches

Industrial memory systems usually combine several layers instead of relying on a single vector threshold.

## 1. Canonicalization

Normalize extracted memories into a standard form before storage.

Example:

```text
用户有糖尿疾病 -> 用户有糖尿病
用户患有糖尿病 -> 用户有糖尿病
用户糖尿病史 -> 用户有糖尿病
```

Canonicalization can be rule-based for known domains, such as health terms, product names, locations, or user preferences.

## 2. Semantic Fact Keys

Store a stable fact key alongside the human-readable memory.

Example:

```json
{
  "content": "用户有糖尿病",
  "fact_key": "health.diabetes",
  "memory_type": "health",
  "sensitivity": "high"
}
```

Then deduplication can primarily use `fact_key` instead of raw text similarity.

If a new memory has the same `fact_key`, the system updates the existing memory instead of creating a new one.

## 3. Entity And Relation Extraction

Represent memory as structured facts:

```json
{
  "subject": "user",
  "relation": "has_condition",
  "object": "diabetes",
  "memory_type": "health",
  "sensitivity": "high"
}
```

This makes it easier to distinguish:

- User has diabetes.
- User's father has diabetes.
- User asks about diabetes.
- User wants diabetes-friendly diet advice.

These are related, but they are not the same memory.

## 4. Hybrid Deduplication

Use multiple checks in order:

1. Exact match on normalized text.
2. Match on `fact_key`.
3. Match on structured entity and relation.
4. Embedding similarity as a fallback.
5. Optional human review for high-sensitivity conflicts.

This avoids depending entirely on embedding similarity.

## 5. Merge Instead Of Append

When a duplicate is found, update the existing memory rather than appending a new one.

Useful metadata includes:

- `seen_count`
- `created_at`
- `last_seen_at`
- `aliases`
- `confidence`
- `source_turn_ids`

Example:

```json
{
  "content": "用户有糖尿病",
  "fact_key": "health.diabetes",
  "aliases": ["用户有糖尿疾病", "用户患有糖尿病"],
  "seen_count": 3,
  "created_at": "...",
  "last_seen_at": "..."
}
```

This keeps one canonical memory while preserving useful evidence.

## 6. Domain Dictionaries

For known sensitive domains, maintain a small controlled vocabulary.

For example, health-related terms can be mapped:

```json
{
  "糖尿疾病": "糖尿病",
  "糖尿症": "糖尿病",
  "血糖病": "糖尿病"
}
```

This is common in production systems where certain categories need high precision.

## Recommended Future Direction

For this project, a practical next step is:

1. Keep the temporary threshold at `0.8`.
2. Add a `canonical_content` field during extraction or before storage.
3. Add a `fact_key` field for common high-value categories.
4. Deduplicate by `fact_key` first, then by canonical text, then by embedding similarity.
5. When duplicates are detected, merge metadata instead of appending a new record.

For the diabetes example, both inputs should map to:

```json
{
  "canonical_content": "用户有糖尿病",
  "fact_key": "health.diabetes"
}
```

Then "用户有糖尿疾病" would update the existing "用户有糖尿病" memory rather than create a new memory.

## Current Status

This issue is not fully solved yet.

The threshold has been lowered to `0.8` as a temporary workaround. This may reduce duplicate storage, but the system still needs canonicalization, fact keys, and merge behavior for robust long-term memory management.
