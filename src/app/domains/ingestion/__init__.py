"""Ingestion context.

Owns the cron-triggered pipeline: source -> dedupe -> agentic filter -> identity
resolution -> local embedding -> persist. Publishes `IngestionService`,
`MessageSource` and the run DTOs.
"""
