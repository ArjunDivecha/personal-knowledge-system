type DraftInlineStyleRange = {
	offset?: unknown;
	length?: unknown;
	style?: unknown;
};

type DraftEntityRange = {
	offset?: unknown;
	length?: unknown;
	key?: unknown;
};

type DraftBlock = {
	key?: unknown;
	text?: unknown;
	type?: unknown;
	inlineStyleRanges?: unknown;
	entityRanges?: unknown;
};

type DraftEntity = {
	type?: unknown;
	data?: unknown;
};

function asRecord(value: unknown): Record<string, unknown> | null {
	if (!value || typeof value !== "object" || Array.isArray(value)) return null;
	return value as Record<string, unknown>;
}

function asBlocks(value: unknown): DraftBlock[] {
	return Array.isArray(value)
		? (value.filter((item) => asRecord(item)) as DraftBlock[])
		: [];
}

function asInlineRanges(value: unknown): DraftInlineStyleRange[] {
	return Array.isArray(value)
		? (value.filter((item) => asRecord(item)) as DraftInlineStyleRange[])
		: [];
}

function asEntityRanges(value: unknown): DraftEntityRange[] {
	return Array.isArray(value)
		? (value.filter((item) => asRecord(item)) as DraftEntityRange[])
		: [];
}

function toInteger(value: unknown): number | null {
	if (typeof value === "number" && Number.isInteger(value)) return value;
	if (typeof value === "string" && /^\d+$/.test(value)) return Number(value);
	return null;
}

function getEntity(entityMap: unknown, key: unknown): DraftEntity | null {
	const record = asRecord(entityMap);
	if (!record) return null;
	const keyString = String(key);
	const entity = asRecord(record[keyString]);
	return entity as DraftEntity | null;
}

function findUrl(value: unknown): string | null {
	if (typeof value === "string" && /^https?:\/\//i.test(value)) return value;
	if (Array.isArray(value)) {
		for (const item of value) {
			const found = findUrl(item);
			if (found) return found;
		}
	}
	const record = asRecord(value);
	if (!record) return null;
	for (const key of [
		"url",
		"media_url_https",
		"media_url",
		"expanded_url",
		"src",
	]) {
		const candidate = record[key];
		if (typeof candidate === "string" && /^https?:\/\//i.test(candidate))
			return candidate;
	}
	for (const nested of Object.values(record)) {
		const found = findUrl(nested);
		if (found) return found;
	}
	return null;
}

function entityMarkdown(entity: DraftEntity | null): string | null {
	if (!entity) return null;
	const url = findUrl(entity.data);
	if (!url) return null;
	const type = typeof entity.type === "string" ? entity.type.toLowerCase() : "";
	if (
		type.includes("image") ||
		type.includes("photo") ||
		type.includes("media")
	) {
		return `![](${url})`;
	}
	return url;
}

function applyBoldRanges(
	text: string,
	inlineRanges: DraftInlineStyleRange[],
): string {
	const insertions: Array<{ index: number; marker: string }> = [];
	for (const range of inlineRanges) {
		if (range.style !== "Bold" && range.style !== "BOLD") continue;
		const offset = toInteger(range.offset);
		const length = toInteger(range.length);
		if (offset === null || length === null || length <= 0) continue;
		if (offset < 0 || offset + length > text.length) continue;
		insertions.push({ index: offset + length, marker: "**" });
		insertions.push({ index: offset, marker: "**" });
	}
	return insertions
		.sort((a, b) => b.index - a.index)
		.reduce((current, insertion) => {
			return `${current.slice(0, insertion.index)}${insertion.marker}${current.slice(insertion.index)}`;
		}, text);
}

function replaceEntities(
	text: string,
	ranges: DraftEntityRange[],
	entityMap: unknown,
): string {
	const replacements: Array<{ start: number; end: number; value: string }> = [];
	for (const range of ranges) {
		const offset = toInteger(range.offset);
		const length = toInteger(range.length);
		if (offset === null || length === null || length < 0) continue;
		if (offset < 0 || offset + length > text.length) continue;
		const markdown = entityMarkdown(getEntity(entityMap, range.key));
		if (markdown) {
			replacements.push({
				start: offset,
				end: offset + length,
				value: markdown,
			});
		}
	}
	return replacements
		.sort((a, b) => b.start - a.start)
		.reduce((current, replacement) => {
			return `${current.slice(0, replacement.start)}${replacement.value}${current.slice(
				replacement.end,
			)}`;
		}, text);
}

function renderBlock(
	block: DraftBlock,
	entityMap: unknown,
	orderedIndex: number,
): string {
	const text = typeof block.text === "string" ? block.text : "";
	const blockType = typeof block.type === "string" ? block.type : "unstyled";
	const ranges = asEntityRanges(block.entityRanges);

	if (blockType === "atomic") {
		const media = ranges
			.map((range) => entityMarkdown(getEntity(entityMap, range.key)))
			.filter(
				(value): value is string =>
					typeof value === "string" && value.length > 0,
			);
		return media.join("\n");
	}

	const withEntities = replaceEntities(text, ranges, entityMap);
	const withStyles = applyBoldRanges(
		withEntities,
		asInlineRanges(block.inlineStyleRanges),
	);

	switch (blockType) {
		case "ordered-list-item":
			return `${orderedIndex}. ${withStyles}`;
		case "unordered-list-item":
			return `- ${withStyles}`;
		case "header-one":
			return `# ${withStyles}`;
		case "header-two":
			return `## ${withStyles}`;
		case "header-three":
			return `### ${withStyles}`;
		case "blockquote":
			return `> ${withStyles}`;
		default:
			return withStyles;
	}
}

export function flattenDraftArticleBlocks(
	blocksValue: unknown,
	entityMap: unknown,
): string {
	const blocks = asBlocks(blocksValue);
	const rendered: string[] = [];
	let orderedIndex = 1;

	for (const block of blocks) {
		const type = typeof block.type === "string" ? block.type : "unstyled";
		if (type === "ordered-list-item") {
			rendered.push(renderBlock(block, entityMap, orderedIndex));
			orderedIndex += 1;
			continue;
		}
		orderedIndex = 1;
		const value = renderBlock(block, entityMap, orderedIndex).trimEnd();
		if (value.trim().length > 0) {
			rendered.push(value);
		}
	}

	return rendered.join("\n\n").trim();
}

export function flattenArticle(
	value: unknown,
): { title: string; body_markdown: string } | null {
	const article = asRecord(value);
	if (!article) return null;

	const title =
		typeof article.title === "string"
			? article.title
			: typeof article.name === "string"
				? article.name
				: "";
	for (const key of ["body_markdown", "markdown", "body", "text"]) {
		const candidate = article[key];
		if (typeof candidate === "string" && candidate.trim().length > 0) {
			return { title, body_markdown: candidate.trim() };
		}
	}

	const content = asRecord(article.content);
	if (content) {
		const body = flattenDraftArticleBlocks(content.blocks, content.entityMap);
		if (body) return { title, body_markdown: body };
	}

	const body = flattenDraftArticleBlocks(article.blocks, article.entityMap);
	return body ? { title, body_markdown: body } : null;
}
