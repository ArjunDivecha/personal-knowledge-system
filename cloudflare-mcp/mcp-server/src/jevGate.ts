// Jev abstention gate for source-first search.
//
// WHY: PKS's final_score measures similarity, not answerability. On the BEAM
// benchmark (beam-eval/runs/20260821T0415Z_baseline) the production score
// could not separate unanswerable questions from answerable ones at any
// threshold (Youden J 0.131; recomputed 0.075 on the 2026-09-06 run), so the
// 0.65 floor abstained on 0 of 40 unanswerable questions. TypeSafe's Jev — a
// decision-only model returning a yes/no probability — asked "does this passage
// state information usable in a direct answer to the query?" separated them at
// AUC 0.865 / Youden J 0.642 (beam-eval/runs/20260917T0220Z_jev_abstention).
//
// WHAT: after the fixed ranking has produced its results, one Jev request
// scores every returned passage for answer evidence. In "on" mode, if no
// passage clears the threshold the search abstains with
// abstain_reason "jev_no_answer_evidence". In "shadow" mode the scores are
// attached but never change results. Either way every result carries
// jev_evidence / jev_relevant so the decision is inspectable.
//
// INVARIANTS
// - Fail open. A Jev error, timeout, or malformed reply leaves the results
//   exactly as the ranker produced them and reports jev.status "unavailable".
//   A third-party outage must never make PKS return nothing.
// - Deterministic recovery is never gated. Results that entered through an
//   exact identifier, an explicitly named project, or a strong exact lexical
//   phrase are the ranker's guarantee that "1MTR" finds the one chunk naming
//   it; a semantic answerability judgment does not override that guarantee.
// - The gate can only ADD abstentions. It never admits evidence the floor
//   rejected and never reorders.
// - No config -> byte-identical behaviour. The BEAM harness and the probe
//   evaluator pass config only when TYPESAFE_API_KEY is set.

export type JevGateMode = "off" | "shadow" | "on";

export interface JevGateConfig {
	apiKey: string;
	mode: JevGateMode;
	/** Abstain when the best passage's evidence probability is below this. */
	threshold: number;
	timeoutMs?: number;
	/** Injectable for tests; defaults to globalThis.fetch. */
	fetch?: typeof fetch;
	endpoint?: string;
	model?: string;
}

export const JEV_DEFAULT_THRESHOLD = 0.5;
export const JEV_DEFAULT_TIMEOUT_MS = 2500;
export const JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone";
export const JEV_MODEL = "jev-latest";
// Jev's request budget is ~150k English characters shared by state and
// questions. Twenty 3,200-char chunks is ~64k, comfortably inside; the cap is
// a guard against a future larger chunker, not a tuning knob.
const MAX_PASSAGE_CHARS = 6000;

export interface JevGateEnv {
	TYPESAFE_API_KEY?: string;
	JEV_ABSTENTION_MODE?: string;
	JEV_ABSTENTION_THRESHOLD?: string;
}

/** Build a gate config from environment variables; undefined means "gate off". */
export function jevGateFromEnv(env: JevGateEnv): JevGateConfig | undefined {
	const mode = (env.JEV_ABSTENTION_MODE ?? "off").trim().toLowerCase();
	if (mode !== "shadow" && mode !== "on") return undefined;
	const apiKey = (env.TYPESAFE_API_KEY ?? "").trim();
	if (!apiKey) return undefined;
	const parsed = Number(env.JEV_ABSTENTION_THRESHOLD);
	const threshold = Number.isFinite(parsed) && parsed > 0 && parsed < 1 ? parsed : JEV_DEFAULT_THRESHOLD;
	return { apiKey, mode, threshold };
}

interface GateableResult {
	id: string;
	text: string;
	exact_identifier_match?: boolean;
	exact_lexical_match?: boolean;
	explicit_project_match?: boolean;
	jev_evidence?: number | null;
	jev_relevant?: number | null;
}

export interface JevGateReport {
	mode: JevGateMode;
	status: "ok" | "unavailable" | "skipped";
	threshold: number;
	model: string | null;
	max_evidence: number | null;
	answerable: number | null;
	latency_ms: number | null;
	/** True when the gate would abstain (or did, in "on" mode). */
	would_abstain: boolean;
	/** Number of results exempt because they came through deterministic recovery. */
	protected_results: number;
	error: string | null;
}

export function buildJevRequest(query: string, passages: Array<{ id: string; text: string }>, model: string): Record<string, unknown> {
	const questions: Record<string, unknown> = {
		answerable: {
			type: "noul",
			instructions: "Considering ALL the passages together, do they contain the information needed to answer the query directly and specifically?",
			criteria: {
				true: "The specific facts the query asks for are stated in at least one passage.",
				false: "The passages discuss related topics but do not state the specific facts the query asks for, or the query refers to events or details that never appear.",
			},
		},
	};
	passages.forEach((passage, index) => {
		const pid = `p${index}`;
		questions[`${pid}_evidence`] = {
			type: "noul",
			instructions: `Does passage ${pid} state information usable in a direct answer to the query?`,
			criteria: {
				true: "The passage states specific facts that answer what the query asks.",
				false: "The passage is on a related topic but does not state those facts.",
			},
		};
		questions[`${pid}_relevant`] = {
			type: "noul",
			instructions: `Does passage ${pid} address the subject of the query?`,
		};
	});
	return {
		state: {
			query,
			passages: passages.map((passage, index) => ({
				id: `p${index}`,
				rank: index + 1,
				text: passage.text.slice(0, MAX_PASSAGE_CHARS),
			})),
		},
		model,
		questions,
	};
}

function noul(answers: Record<string, unknown>, key: string): number | null {
	const answer = answers[key];
	if (!answer || typeof answer !== "object") return null;
	const value = Number((answer as Record<string, unknown>).noul);
	return Number.isFinite(value) ? Math.round(value * 1000) / 1000 : null;
}

/**
 * Score `results` with Jev and, in "on" mode, decide abstention. Mutates the
 * result objects to attach jev_evidence / jev_relevant. Returns the report and
 * the possibly-emptied result list; never throws.
 */
export async function applyJevGate<T extends GateableResult>(
	query: string,
	results: T[],
	config: JevGateConfig,
): Promise<{ results: T[]; abstain: boolean; report: JevGateReport }> {
	const report: JevGateReport = {
		mode: config.mode,
		status: "skipped",
		threshold: config.threshold,
		model: null,
		max_evidence: null,
		answerable: null,
		latency_ms: null,
		would_abstain: false,
		protected_results: 0,
		error: null,
	};
	if (results.length === 0) return { results, abstain: false, report };

	const fetchImpl = config.fetch ?? globalThis.fetch;
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), config.timeoutMs ?? JEV_DEFAULT_TIMEOUT_MS);
	const started = Date.now();
	try {
		const response = await fetchImpl(config.endpoint ?? JEV_ENDPOINT, {
			method: "POST",
			headers: {
				Authorization: `Bearer ${config.apiKey}`,
				"Content-Type": "application/json",
			},
			body: JSON.stringify(buildJevRequest(query, results, config.model ?? JEV_MODEL)),
			signal: controller.signal,
		});
		report.latency_ms = Date.now() - started;
		if (!response.ok) {
			report.status = "unavailable";
			report.error = `http_${response.status}`;
			return { results, abstain: false, report };
		}
		const payload = await response.json() as Record<string, unknown>;
		const answers = payload.answers;
		if (!answers || typeof answers !== "object") {
			report.status = "unavailable";
			report.error = "malformed_response";
			return { results, abstain: false, report };
		}
		const answerMap = answers as Record<string, unknown>;
		report.model = typeof payload.model === "string" ? payload.model : null;
		report.answerable = noul(answerMap, "answerable");
		let maxEvidence: number | null = null;
		let protectedCount = 0;
		results.forEach((result, index) => {
			result.jev_evidence = noul(answerMap, `p${index}_evidence`);
			result.jev_relevant = noul(answerMap, `p${index}_relevant`);
			if (result.jev_evidence !== null && (maxEvidence === null || result.jev_evidence > maxEvidence)) {
				maxEvidence = result.jev_evidence;
			}
			if (result.exact_identifier_match || result.exact_lexical_match || result.explicit_project_match) {
				protectedCount += 1;
			}
		});
		report.max_evidence = maxEvidence;
		report.protected_results = protectedCount;
		if (maxEvidence === null) {
			report.status = "unavailable";
			report.error = "no_passage_scores";
			return { results, abstain: false, report };
		}
		report.status = "ok";
		report.would_abstain = protectedCount === 0 && maxEvidence < config.threshold;
		const abstain = config.mode === "on" && report.would_abstain;
		return { results: abstain ? [] : results, abstain, report };
	} catch (error) {
		report.latency_ms = Date.now() - started;
		report.status = "unavailable";
		report.error = error instanceof Error && error.name === "AbortError"
			? "timeout"
			: error instanceof Error ? error.message.slice(0, 200) : String(error).slice(0, 200);
		return { results, abstain: false, report };
	} finally {
		clearTimeout(timer);
	}
}
