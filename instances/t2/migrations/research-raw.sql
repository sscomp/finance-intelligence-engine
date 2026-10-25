-- Additive; target and role must be independently verified.
CREATE SCHEMA IF NOT EXISTS t2_research;
REVOKE ALL ON SCHEMA t2_research FROM PUBLIC;
GRANT USAGE ON SCHEMA t2_research TO fie_t2_ingestor;
CREATE TABLE IF NOT EXISTS t2_research.batches(batch_id text PRIMARY KEY,source text NOT NULL,payload jsonb NOT NULL);
GRANT SELECT,INSERT ON t2_research.batches TO fie_t2_ingestor;
