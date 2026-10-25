-- Additive: apply only to verified independent target, inside a transaction.
CREATE SCHEMA IF NOT EXISTS t2_ingestion;
REVOKE ALL ON SCHEMA t2_ingestion FROM PUBLIC;
GRANT USAGE ON SCHEMA t2_ingestion TO fie_t2_ingestor;
-- Additive; target and role must be independently verified.
CREATE TABLE IF NOT EXISTS t2_ingestion.receipts(
 batch_id text PRIMARY KEY,source text NOT NULL,committed_at text NOT NULL,signals integer NOT NULL,scores integer NOT NULL,source_dates jsonb NOT NULL);
 CREATE TABLE IF NOT EXISTS t2_ingestion.score_keys(score_key text PRIMARY KEY,batch_id text NOT NULL);
 GRANT SELECT,INSERT ON t2_ingestion.receipts,t2_ingestion.score_keys TO fie_t2_ingestor;
 GRANT USAGE ON SCHEMA public TO fie_t2_ingestor;
 GRANT SELECT,INSERT ON public.signal_log,public.score_snapshot,public.graph_nodes,public.graph_edges TO fie_t2_ingestor;
 GRANT USAGE,SELECT ON SEQUENCE public.score_snapshot_snapshot_id_seq TO fie_t2_ingestor;

