-- Additive: apply only to verified independent target, inside a transaction.
CREATE SCHEMA IF NOT EXISTS t2_ingestion;
REVOKE ALL ON SCHEMA t2_ingestion FROM PUBLIC;
GRANT USAGE ON SCHEMA t2_ingestion TO fie_t2_ingestor;
-- Additive; target and role must be independently verified.
CREATE TABLE IF NOT EXISTS t2_ingestion.batches(
 batch_id text PRIMARY KEY, source text NOT NULL, fetched_at text NOT NULL,
 source_dates jsonb NOT NULL,responses jsonb NOT NULL,coverage jsonb NOT NULL,observations jsonb NOT NULL);
 GRANT SELECT,INSERT ON t2_ingestion.batches TO fie_t2_ingestor;

