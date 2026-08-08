/**
 * replay.ts — the shape of a bundle written by scripts/build_replay.py, and the
 * two rendering rules the Command Center makes non-negotiable.
 *
 * Every numeric field here is `number | null` on purpose. The metrics strip: "Unknown
 * values render N/A — never a placeholder number." A `number` that defaults to
 * 0 somewhere in the pipeline is exactly the placeholder that rule forbids, so
 * the absence is carried all the way into the type and `fmt` is the only thing
 * allowed to turn it into text.
 */

export type IncidentState =
  | "DETECTED"
  | "DIAGNOSING"
  | "AWAITING_APPROVAL"
  | "REMEDIATING"
  | "VERIFIED"
  | "KNOWLEDGE_WRITTEN";

export type NodeState = "done" | "blocked" | "refused" | "degraded" | "ran_no_record" | "idle";

export interface ToolCall {
  tool: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  duration_ms: number;
  raw_ref: string | null;
  error: string | null;
}

export interface HandoffRecord {
  from_agent: string;
  to_agent: string;
  incident_id: string;
  evidence_ids: string[];
  decision: string;
  rationale: string;
  started_at: string;
  ended_at: string;
  tokens: number;
  model: string | null;
  tool_calls: ToolCall[];
  duration_ms: number;
}

export interface RailNode {
  agent: string;
  state: NodeState;
  duration_ms: number | null;
  tokens: number | null;
  model: string | null;
  tool_call_count: number;
  evidence_count: number;
  evidence_ids: string[];
  records: HandoffRecord[];
}

export interface EvidenceItem {
  id: string;
  source: string;
  trust: string;
  confidence: string;
  claim: string;
  raw_ref: string;
  datahub_urn: string | null;
  datahub_link: string | null;
  collected_at: string;
  untrusted: boolean;
  distinct: boolean;
}

export interface ImpactedEntity {
  urn: string;
  entity_type: string;
  hops: number | null;
  is_ml_model: boolean;
}

export interface BlastRadius {
  root_urn: string | null;
  column: string | null;
  dataset_level: ImpactedEntity[];
  column_level: ImpactedEntity[];
  column_path: string[];
  terminal_ml_models: ImpactedEntity[];
  max_hops: number | null;
  queries: { urn: string; statement: string; source: string }[];
  queries_raw_ref: string | null;
}

export interface WriteBackArtifact {
  number: number;
  name: string;
  target_urn: string;
  outcome: string;
  idempotency_key: string;
  detail: string;
  raw_ref: string;
  created_urn: string | null;
  datahub_link: string | null;
}

export interface ReplayBundle {
  schema_version: number;
  run_id: string;
  built_at: string;
  source_pack: string;
  pack_written_at: string | null;
  artifact_count: number | null;
  banners: {
    replay: boolean;
    replay_text: string;
    dry_run: boolean;
    seeded_catalog: boolean;
  };
  incident: {
    incident_id: string | null;
    dataset_urn: string | null;
    datahub_link: string | null;
    severity: string | null;
    state: IncidentState | null;
    states: IncidentState[];
    states_reached: IncidentState[];
    started_at: string | null;
    ended_at: string | null;
    elapsed_ms: number | null;
  };
  rail: RailNode[];
  evidence: EvidenceItem[];
  chain: {
    digest: string | null;
    is_sufficient: boolean | null;
    missing_for_sufficiency: string[];
    datahub_error: string | null;
  };
  prior_knowledge: {
    state: "PRIOR" | "NONE" | "UNAVAILABLE" | null;
    rationale: string | null;
    documents: { urn: string; title: string | null; sub_type: string | null }[];
    document_count: number | null;
    /** Always null today: the Scribe's runbook template records no such figure. */
    time_to_root_cause_s: number | null;
    capabilities_ref: string | null;
  };
  /**
   * The DataHub tool surface this run negotiated, and the part of it the agents
   * actually invoked.
   *
   * `offered` is the server's `tools/list` response for THIS run, not a
   * constant — a catalog holding no documents does not offer the document
   * tools, so the count differs between runs against the same server. That is
   * the point of showing it.
   */
  catalog: {
    server: { name: string | null; version: string | null } | null;
    protocol_version: string | null;
    offered: string[];
    offered_count: number | null;
    /** tool name → number of calls made in this run. */
    used: Record<string, number>;
    capabilities_ref: string | null;
  };
  blast_radius: BlastRadius | null;
  root_cause: {
    refused: boolean;
    decision: string | null;
    rationale: string | null;
    missing_evidence_classes: string[];
    datahub_error: string | null;
    statement: string | null;
    by_model: boolean;
    chain_ids: string[];
    raw_ref: string | null;
  };
  policy: {
    risk: string | null;
    mode: string | null;
    allowed_action: string | null;
    rationale: string | null;
    owners: { urn: string; display_name: string; ownership_type: string }[];
    owner_fallback: string | null;
    approver_urn: string | null;
    approver_name: string | null;
    decided_at: string | null;
    approval_note: string | null;
    branch_reviewed: string | null;
    proposed_diff: string | null;
    proposed_diff_ref: string | null;
    sentinel_scan: {
      files_touched: string[];
      added: number;
      removed: number;
      risk: string;
      blocked: boolean;
      findings: unknown[];
    } | null;
    validation: string | null;
    validation_ref: string | null;
  } | null;
  write_back: {
    performed: boolean;
    reason: string | null;
    incident_resolved: boolean;
    all_landed: boolean;
    artifacts: WriteBackArtifact[];
    guard: string;
  } | null;
  security: {
    is_demo: boolean;
    sources_scanned: number | null;
    attempts_detected: number | null;
    detections: unknown[];
    action_taken: string | null;
    untrusted_sources: string[];
  };
  metrics: {
    time_to_root_cause_ms: number | null;
    total_loop_ms: number | null;
    mcp_calls: number | null;
    tokens: number | null;
    tokens_note: string | null;
    cost_usd: number | null;
    models: string[];
    evidence_count: number;
    handoff_count: number;
    source: string;
  };
  raw: Record<
    string,
    {
      name?: string;
      bytes?: number;
      note?: string;
      written_at?: string;
      truncated_in_bundle?: boolean;
      content?: string;
      missing?: boolean;
    }
  >;
  missing: Record<string, string>;
  artifacts: { name: string; raw_ref: string; bytes: number; note: string }[];
}

export interface ManifestEntry {
  run_id: string;
  headline: string;
  incident_id: string | null;
  state: string | null;
  captured_at: string | null;
  evidence_count: number;
  artifact_count: number | null;
  refused: boolean;
  dry_run: boolean;
  bytes: number;
}

/** the metrics strip's rule, in one place. Null is N/A — never 0, never "—". */
export const fmt = {
  ms(v: number | null | undefined): string {
    if (v === null || v === undefined) return "N/A";
    if (v < 1000) return `${v.toFixed(v < 10 ? 1 : 0)} ms`;
    return `${(v / 1000).toFixed(2)} s`;
  },
  int(v: number | null | undefined): string {
    return v === null || v === undefined ? "N/A" : v.toLocaleString();
  },
  usd(v: number | null | undefined): string {
    return v === null || v === undefined ? "N/A" : `$${v.toFixed(4)}`;
  },
  time(v: string | null | undefined): string {
    if (!v) return "N/A";
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? "N/A" : d.toISOString().replace("T", " ").slice(0, 19) + "Z";
  },
};

/** The tail of a URN — what fits on a node label without losing identity. */
export function shortUrn(urn: string | null | undefined): string {
  if (!urn) return "N/A";
  const dataset = urn.match(/,([^,]+),(PROD|DEV|QA|UAT|STAGING|TEST)\)/);
  if (dataset) return dataset[1];
  const field = urn.match(/#(.+)$/);
  if (field) return field[1];
  const tail = urn.split(":").pop() ?? urn;
  return tail.length > 28 ? `${tail.slice(0, 25)}…` : tail;
}

export function entityKind(urn: string): string {
  const m = urn.match(/^urn:li:([a-zA-Z]+):/);
  return m ? m[1] : "entity";
}

/**
 * The data platform an entity belongs to, or null if the URN does not name one.
 *
 * This is not cosmetic. A dbt lineage trace returns the *same logical table*
 * twice — once as `dataPlatform:dbt` and once as `dataPlatform:postgres` —
 * which `shortUrn` collapses to one identical label. Rendered without the
 * platform, a correct 7-entity blast radius reads as four entities and three
 * duplicated rows, and a judge's first conclusion is that the panel is broken.
 */
export function platformOf(urn: string): string | null {
  const m = urn.match(/urn:li:dataPlatform:([^,)]+)/);
  if (m) return m[1];
  const flow = urn.match(/urn:li:dataFlow:\(([^,]+),/);
  return flow ? flow[1] : null;
}
