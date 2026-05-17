import { describe, expect, it } from "vitest";
import articleFixture from "./fixtures/fxtwitter-article.json";
import { flattenArticle } from "../src/tweets/article-flattener";

describe("article flattener", () => {
	it("flattens Draft.js article blocks into markdown", () => {
		expect(flattenArticle(articleFixture)).toEqual({
			title: "A long-form note",
			body_markdown: [
				"This is **bold** and clear.",
				"1. First point",
				"2. Second point",
				"![](https://pbs.twimg.com/media/example.jpg)",
				"- Final note",
			].join("\n\n"),
		});
	});

	it("accepts already-flattened article bodies", () => {
		expect(
			flattenArticle({ title: "Done", body_markdown: "Already markdown" }),
		).toEqual({
			title: "Done",
			body_markdown: "Already markdown",
		});
	});
});
