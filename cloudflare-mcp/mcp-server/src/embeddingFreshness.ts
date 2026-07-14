const EMBEDDING_MODEL = "text-embedding-3-large";
const EMBEDDING_DIMENSIONS = 3072;

export { EMBEDDING_DIMENSIONS, EMBEDDING_MODEL };

export async function sha256Hex(value: string): Promise<string> {
	const bytes = new TextEncoder().encode(value);
	const digest = await crypto.subtle.digest("SHA-256", bytes);
	return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export interface EmbeddingFreshnessMetadata {
	embedding_model?: unknown;
	embedding_dimensions?: unknown;
	embedding_input_sha256?: unknown;
	embedding_revision?: unknown;
}

export function embeddingMetadataMatches(
	metadata: EmbeddingFreshnessMetadata,
	expected: { inputSha256: string; revision: number },
): boolean {
	return metadata.embedding_model === EMBEDDING_MODEL &&
		metadata.embedding_dimensions === EMBEDDING_DIMENSIONS &&
		metadata.embedding_input_sha256 === expected.inputSha256 &&
		metadata.embedding_revision === expected.revision;
}
