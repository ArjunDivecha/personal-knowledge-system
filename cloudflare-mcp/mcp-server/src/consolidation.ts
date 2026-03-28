export function formatConsolidationNote(params: {
	timestamp: string;
	source: "dream" | "reconsolidation" | "operator";
	action: string;
	detail: string;
}): string {
	return `${params.timestamp} | source=${params.source} | action=${params.action} | detail=${params.detail}`;
}
