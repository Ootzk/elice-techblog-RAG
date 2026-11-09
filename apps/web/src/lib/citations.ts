import DOMPurify from 'dompurify';
import { marked } from 'marked';
import type { Citation } from './api.js';
import { textFragmentUrl } from './source-links.js';
export { safeSourceUrl } from './source-links.js';

export type Source = Citation & { quotes: string[] };

export function groupSources(citations: Citation[]): Source[] {
	const sources = new Map<number, Source>();
	for (const citation of citations) {
		const existing = sources.get(citation.source_id);
		if (existing) {
			if (!existing.quotes.includes(citation.quote)) existing.quotes.push(citation.quote);
		} else sources.set(citation.source_id, { ...citation, quotes: [citation.quote] });
	}
	return [...sources.values()].sort((a, b) => a.source_id - b.source_id);
}

export function sourceId(messageId: string, source: number): string {
	return `source-${messageId}-${source}`;
}

/** Convert the archived Markdown quote to the visible text the original page displays. */
export function highlightSourceUrl(value: string, quotes: string[]): string | null {
	const blocks = quotes.map((quote) => {
		const clean = DOMPurify.sanitize(marked.parse(quote, { async: false }) as string, {
			FORBID_TAGS: ['img', 'iframe', 'video', 'audio', 'source', 'style', 'link', 'base'],
			FORBID_ATTR: ['style']
		});
		const doc = new DOMParser().parseFromString(clean, 'text/html');
		for (const node of doc.body.querySelectorAll(
			'br,p,li,h1,h2,h3,h4,h5,h6,pre,blockquote,td,th,tr,div'
		)) {
			node.append('\n');
		}
		return (doc.body.textContent ?? '')
			.split('\n')
			.map((text) => text.trim())
			.filter(Boolean);
	});
	return textFragmentUrl(value, blocks);
}

/** Link verified citation markers in prose; leave code and existing links untouched. */
export function renderAnswer(content: string, messageId: string, sources: Source[]): string {
	const clean = DOMPurify.sanitize(marked.parse(content, { async: false }) as string, {
		FORBID_TAGS: ['img', 'iframe', 'style', 'form', 'input', 'video', 'audio'],
		FORBID_ATTR: ['style', 'data-citation-id']
	});
	const doc = new DOMParser().parseFromString(clean, 'text/html');
	const known = new Map(sources.map((source) => [source.source_id, source]));
	const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
	const nodes: Text[] = [];
	while (walker.nextNode()) {
		const node = walker.currentNode as Text;
		if (!node.parentElement?.closest('a, code, pre, button')) nodes.push(node);
	}
	for (const node of nodes) {
		const fragment = doc.createDocumentFragment();
		let offset = 0;
		for (const match of node.data.matchAll(/\[(\d+)\]/g)) {
			const source = known.get(Number(match[1]));
			if (!source) continue;
			fragment.append(node.data.slice(offset, match.index));
			const link = doc.createElement('a');
			link.href = `#${sourceId(messageId, source.source_id)}`;
			link.className = 'citation-reference';
			link.dataset.citationId = String(source.source_id);
			link.setAttribute('aria-label', `출처 ${source.source_id}: ${source.title}`);
			link.title = source.title;
			link.textContent = String(source.source_id);
			fragment.append(link);
			offset = match.index! + match[0].length;
		}
		if (offset) {
			fragment.append(node.data.slice(offset));
			node.replaceWith(fragment);
		}
	}
	return doc.body.innerHTML;
}
