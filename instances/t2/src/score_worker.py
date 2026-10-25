"""Execute inside the existing P3 app, using its pinned scoring implementation.

No database/network access. JSON input and output travel over stdin/stdout.
"""
import datetime as dt
import hashlib
import json
import sys
from types import SimpleNamespace

from phase3.datamodel.signals import Signal, SignalSource
from phase3.pipeline.scoring_pipeline import ScoringPipeline
from phase3.pipeline.input_builder import InputBuilder
from phase3.datamodel.graph import EdgeType, make_graph_edge_id


def score(payload):
    signals = []
    for r in payload["signals"]:
        signals.append(Signal(
            signal_id=r["signal_id"], entity_type=r["entity_type"], entity_id=r["entity_id"],
            signal_type=r["signal_type"], value=r["value"], unit=r["unit"], direction=r["direction"],
            timestamp=dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")),
            source=SignalSource(source_id=r["source_id"], source_type=r["source_type"], ref=r["ref"]),
            date_bucket=r["date_bucket"], metadata=r["metadata"]
        ))

    class Loader:
        def load(self, filters):
            return SimpleNamespace(signals=[s for s in signals if
                s.entity_type == filters.entity_type and s.entity_id == filters.entity_id
                and s.date_bucket == filters.date_bucket])

    class Builder(InputBuilder):
        def build_company(self, *args, **kwargs):
            bundle = super().build_company(*args, **kwargs)
            values = bundle.dimensions["valuation"].values
            # The pinned InputBuilder emits pe_ratio/pb_ratio while the
            # pinned CompanyScorer consumes pe/pb. Adapt only this boundary.
            for source, target in [("pe_ratio", "pe"), ("pb_ratio", "pb")]:
                if source in values: values[target] = values.pop(source)
            return bundle

    pipeline = ScoringPipeline(signal_loader=Loader(), input_builder=Builder())
    out = []; nodes = {}; edges = {}
    signal_rows = {r["signal_id"]:r for r in payload["signals"]}
    def node(identifier, kind, label, stamp, meta):
        nodes[identifier] = {"node_id":identifier,"node_type":kind,"label":label,"created_at":stamp,"metadata_json":json.dumps(meta,ensure_ascii=False),"tags_json":'["T2","independent-ingestion"]'}
    def edge(kind, start, end, stamp, meta):
        identifier=make_graph_edge_id(EdgeType(kind),start,end)
        edges[identifier]={"edge_id":identifier,"edge_type":kind,"from_node_id":start,"to_node_id":end,"created_at":stamp,"metadata_json":json.dumps(meta,ensure_ascii=False)}
    for kind, entity, day in sorted({(s.entity_type, s.entity_id, s.date_bucket) for s in signals}):
        if kind == "macro":
            result = pipeline.run_macro(date_bucket=day, entity_id=entity)
        elif kind == "company":
            spec = payload["universe"][entity]
            result = pipeline.run_company(code=entity, date_bucket=day, name=spec["name"], sector=spec["sector"], is_financial_sector=spec["sector"] == "金融")
        elif kind == "industry":
            result = pipeline.run_industry(industry_id=entity, date_bucket=day)
        else:
            raise ValueError("UNSUPPORTED_KIND")
        if not result.evidence_signal_ids:
            # Do not refresh company scores from institutional flow fields that
            # the pinned company scorer does not consume.
            continue
        breakdown = result.score.breakdown.to_dict()
        breakdown["evidence_signal_ids"] = list(result.evidence_signal_ids)
        breakdown["t2_coverage"] = "Only indicators from this independently collected source batch; missing dimensions use pinned scorer defaults."
        breakdown["t2_source"] = payload["source"]
        def provenance(value):
            if isinstance(value, dict):
                if "source" in value: value["source"] = "t2_" + payload["source"]
                if "source_type" in value: value["source_type"] = "t2_" + payload["source"]
                if "source_ref" in value: value["source_ref"] = "T2 batch " + payload["batch_id"] + "; observation " + day
                for v in value.values(): provenance(v)
            elif isinstance(value, list):
                for v in value: provenance(v)
        provenance(breakdown)
        key_input = [(s.signal_id,s.value,s.unit) for s in signals if s.entity_type==kind and s.entity_id==entity and s.date_bucket==day]
        key = hashlib.sha256(json.dumps(["T2-pinned-scorer-boundary-v1",kind,entity,day,sorted(key_input)],separators=(",", ":")).encode()).hexdigest()
        stamp=payload.get("prior_score_times",{}).get(key,payload["fetched_at"])
        # A permanent computation-time suffix makes the existing API's max-ref
        # selection follow the same newest computation as score_snapshot.
        score_ref=f"score:{kind}:{entity}:{stamp}:{day}:{key}"
        entity_ref=f"entity:{kind}:{entity}"
        source_ref="source:t2:"+payload["source"]
        meta={"instance_id":"T2","source_date":day,"score":result.score.breakdown.score,"score_key":key,"coverage":breakdown["t2_coverage"],"evidence_signal_ids":list(result.evidence_signal_ids)}
        node(score_ref,"score",f"T2 {kind} {entity}",stamp,meta)
        node(entity_ref,"macro_factor" if kind=="macro" else kind,entity,stamp,{"instance_id":"T2","entity_id":entity})
        node(source_ref,"source",payload["source"],stamp,{"instance_id":"T2","source":payload["source"],"urls":[r["url"] for r in payload["responses"]]})
        edge("refers_to",score_ref,entity_ref,stamp,meta)
        edge("cites",score_ref,source_ref,stamp,meta)
        for sid in result.evidence_signal_ids:
            signal_ref="signal:"+sid
            r=signal_rows[sid]
            node(signal_ref,"signal",r["signal_type"],stamp,{"instance_id":"T2","signal_id":sid,"source_date":day,"value":r["value"],"unit":r["unit"],"ref":r["ref"]})
            edge("contributes_to",signal_ref,score_ref,stamp,{"instance_id":"T2","source_date":day})
            edge("generated",signal_ref,source_ref,stamp,{"instance_id":"T2"})
        out.append({"scorer": kind, "entity_type": kind, "entity_id": entity,
                    "score_key": key,
                    "score": result.score.breakdown.score, "breakdown_json": json.dumps(breakdown, ensure_ascii=False),
                    "inputs_json": json.dumps({"source_date": day, "signal_ids": list(result.evidence_signal_ids), "warnings": list(result.warnings)}, ensure_ascii=False),
                    "notes": "T2 independent source batch " + payload["batch_id"] + "; partial source coverage",
                    "computed_at": payload["fetched_at"]})
    return {"scores":out,"nodes":list(nodes.values()),"edges":list(edges.values())}


if __name__ == "__main__":
    try:
        print(json.dumps(score(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
    except Exception:
        print('{"error":"PINNED_SCORING_FAILED"}')
        sys.exit(2)
