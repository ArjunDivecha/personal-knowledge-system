// =============================================================================
// MMR DIVERSITY SELECTION — greedy marginal-value selection over a candidate pool
// =============================================================================
// Query-time Phase B of contract PKS-INJECTION-RANKING-002
// (/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/injection-ranking-v2.spec.md).
// Replaces "sort filteredResults by final_score, slice top-K" with greedy MMR:
// at each step, pick the candidate maximizing
//     finalScore(C) - lambda * max_cosine(C, alreadySelected)
// subject to a per-domain-cluster cap and a token budget. Query-time only
// (Python/Dream never selects results, so there is no Python twin for this
// file — see distillation/utils/salience_v2.py's module docstring).
//
// INV4 (never displace the single best match): the very first pick is
// ALWAYS argmax(finalScore) over ALL candidates, with no diversity penalty
// and no domain/budget filtering applied to that first pick — diversity must
// never cost the best answer. Only picks #2+ apply the MMR penalty and the
// domain-cap / token-budget constraints.
//
// INV5 (budget + domain cap respected): no more than opts.maxPerDomain
// candidates sharing a domainCluster are ever selected, and the cumulative
// estTokens of the selection never exceeds opts.tokenBudget.
//
// Wired into cloudflare-mcp/mcp-server/src/index.ts's search tool handler,
// behind the RANKING_V2 flag (see selectSearchTopResults there).
//
// INPUT FILES:
// - shared/memory_policy.json (imported below; the salience_v2.mmr_* keys
//   are the default lambda/maxPerDomain/tokenBudget — never hardcoded twice)
// OUTPUT FILES:
// - None (pure logic; returns values, writes nothing)
// =============================================================================

import memoryPolicy from "../../../shared/memory_policy.json";

type JsonRecord = Record<string, unknown>;

const POLICY = memoryPolicy as unknown as JsonRecord;

function policySalienceV2(): JsonRecord {
	const cfg = POLICY.salience_v2 as JsonRecord | undefined;
	if (!cfg) {
		throw new Error("shared/memory_policy.json is missing the required 'salience_v2' block");
	}
	return cfg;
}

export interface MmrCandidate {
	id: string;
	finalScore: number;
	domainCluster: string;
	embedding: number[];
	estTokens: number;
}

export interface SelectWithDiversityOptions {
	limit: number;
	tokenBudget?: number;
	lambda?: number;
	maxPerDomain?: number;
}

// Standard cosine similarity; zero-magnitude vectors (or empty embeddings)
// return 0 similarity rather than dividing by zero or throwing.
export function cosineSimilarity(a: number[], b: number[]): number {
	const length = Math.min(a.length, b.length);
	if (length === 0) return 0;
	let dot = 0;
	let normA = 0;
	let normB = 0;
	for (let i = 0; i < length; i += 1) {
		dot += a[i] * b[i];
		normA += a[i] * a[i];
		normB += b[i] * b[i];
	}
	if (normA === 0 || normB === 0) return 0;
	return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

function maxCosineToSelected(candidate: MmrCandidate, selected: MmrCandidate[]): number {
	if (selected.length === 0) return 0;
	let max = 0;
	for (const s of selected) {
		const sim = cosineSimilarity(candidate.embedding, s.embedding);
		if (sim > max) max = sim;
	}
	return max;
}

export function selectWithDiversity(
	candidates: MmrCandidate[],
	opts: SelectWithDiversityOptions,
): MmrCandidate[] {
	const limit = Math.max(0, opts.limit);
	if (limit === 0 || candidates.length === 0) return [];

	const defaults = policySalienceV2();
	const lambda = opts.lambda ?? Number(defaults.mmr_lambda);
	const maxPerDomain = opts.maxPerDomain ?? Number(defaults.mmr_max_per_domain);
	const tokenBudget = opts.tokenBudget ?? Number(defaults.mmr_default_token_budget);

	// INV4: pick #1 is always the pure best match — no diversity penalty, no
	// domain/budget filtering.
	let bestIndex = 0;
	for (let i = 1; i < candidates.length; i += 1) {
		const current = candidates[i];
		const best = candidates[bestIndex];
		if (
			current.finalScore > best.finalScore ||
			(current.finalScore === best.finalScore && current.id < best.id)
		) {
			bestIndex = i;
		}
	}

	const selected: MmrCandidate[] = [candidates[bestIndex]];
	const remaining = candidates.filter((_, i) => i !== bestIndex);
	const domainCounts = new Map<string, number>();
	domainCounts.set(selected[0].domainCluster, 1);
	let cumulativeTokens = selected[0].estTokens;

	while (selected.length < limit) {
		let bestCandidate: MmrCandidate | null = null;
		let bestValue = -Infinity;
		for (const candidate of remaining) {
			if (selected.some((s) => s.id === candidate.id)) continue;
			const domainCount = domainCounts.get(candidate.domainCluster) ?? 0;
			if (domainCount >= maxPerDomain) continue;
			if (cumulativeTokens + candidate.estTokens > tokenBudget) continue;

			const value = candidate.finalScore - lambda * maxCosineToSelected(candidate, selected);
			if (
				bestCandidate === null ||
				value > bestValue ||
				(value === bestValue && candidate.finalScore > bestCandidate.finalScore) ||
				(value === bestValue && candidate.finalScore === bestCandidate.finalScore && candidate.id < bestCandidate.id)
			) {
				bestCandidate = candidate;
				bestValue = value;
			}
		}
		if (bestCandidate === null) break; // budget exhausted or every remaining candidate violates the domain cap

		selected.push(bestCandidate);
		domainCounts.set(bestCandidate.domainCluster, (domainCounts.get(bestCandidate.domainCluster) ?? 0) + 1);
		cumulativeTokens += bestCandidate.estTokens;
	}

	return selected;
}
