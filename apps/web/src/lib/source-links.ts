/** External source links: preserve section anchors as a fallback for text directives. */
export function safeSourceUrl(value: string): string | null {
	try {
		const url = new URL(value);
		return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
	} catch {
		return null;
	}
}

function encodeText(value: string): string {
	// Hyphens delimit text-fragment context; encode them as well as URL delimiters.
	return encodeURIComponent(value).replace(
		/[-!'()*]/g,
		(char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`
	);
}

function edge(text: string, start: boolean): string {
	if (text.length <= 120) return text;
	const part = start ? text.slice(0, 120) : text.slice(-120);
	const boundary = start ? part.lastIndexOf(' ') : part.indexOf(' ');
	return boundary < 0 ? part : start ? part.slice(0, boundary) : part.slice(boundary + 1);
}

/** Each quote is split into visible blocks; individual selectors must stay in one block. */
export function textFragmentUrl(value: string, quotes: string[][]): string | null {
	const safe = safeSourceUrl(value);
	if (!safe) return null;
	const url = new URL(safe);
	const anchor = url.hash.split(':~:')[0];
	const directives: string[] = [];
	for (const quote of quotes) {
		const blocks = quote.map((text) => text.replace(/\s+/g, ' ').trim()).filter(Boolean);
		if (!blocks.length) continue;
		const exact = blocks.length === 1 && blocks[0].length <= 240;
		const selector = exact
			? encodeText(blocks[0])
			: `${encodeText(edge(blocks[0], true))},${encodeText(edge(blocks.at(-1)!, false))}`;
		const directive = `text=${selector}`;
		if (!directives.includes(directive)) {
			if (directives.join('&').length + directive.length > 6000) break;
			directives.push(directive);
		}
	}
	if (directives.length) url.hash = `${anchor}:~:${directives.join('&')}`;
	return url.href;
}
