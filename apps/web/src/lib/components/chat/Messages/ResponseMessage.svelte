<!-- Adapted from Open WebUI response layout; see UPSTREAM.md. -->
<script lang="ts">
	import DOMPurify from 'dompurify';
	import { marked } from 'marked';
	import Name from './Name.svelte';
	import Spinner from '../../common/Spinner.svelte';
	import type { Turn } from '#lib/api.js';
	export let message: Turn;
	let copied = false;
	async function copyAnswer() {
		try {
			await navigator.clipboard.writeText(content);
			copied = true;
		} catch {
			copied = false;
		}
	}
	$: pending = ['queued', 'running'].includes(message.status);
	$: content =
		message.result?.answer ||
		(message.result?.status === 'needs_clarification'
			? '질문의 대상을 조금 더 구체적으로 알려주세요.'
			: '자료에서 충분한 근거를 찾지 못했습니다. 질문을 조금 더 구체적으로 적어주세요.');
	$: html = DOMPurify.sanitize(marked.parse(content, { async: false }) as string, {
		FORBID_TAGS: ['img', 'iframe', 'style', 'form', 'input', 'video', 'audio'],
		FORBID_ATTR: ['style']
	});
	function safeUrl(value: string) {
		try {
			const u = new URL(value);
			return ['https:', 'http:'].includes(u.protocol) ? u.href : '#';
		} catch {
			return '#';
		}
	}
</script>

{#key message.id}
	<div
		class="flex w-full message-{message.id}"
		id="message-{message.id}"
		style="scroll-margin-top:3rem"
	>
		<div class="flex-auto w-0 pl-1 relative">
			<Name><span class="line-clamp-1 text-black font-semibold">테크블로그 도우미</span></Name>
			<div class="chat-assistant w-full min-w-full mt-2">
				{#if pending}
					<div class="flex items-center gap-2 text-sm text-gray-500" role="status">
						<Spinner />{message.status === 'queued'
							? '답변을 기다리고 있어요'
							: '자료를 확인하고 답변을 준비하고 있어요'}
					</div>
				{:else if message.status !== 'completed'}
					<p class="text-sm text-gray-600" role="status">
						{message.status === 'interrupted'
							? '답변 작업이 중단되었습니다. 자동으로 다시 요청하지 않았습니다.'
							: '답변을 만들지 못했습니다. 잠시 후 새 질문으로 다시 시도해 주세요.'}
					</p>
				{:else}
					<div class="markdown">{@html html}</div>
					<button class="text-xs text-gray-400 mt-3 underline" on:click={copyAnswer}
						>{copied ? '복사했습니다' : '답변 복사'}</button
					>
					{#if message.result?.citations.length}
						<div class="mt-5 flex flex-col gap-2" aria-label="답변 출처">
							<p class="text-xs font-medium text-gray-400">확인한 출처</p>
							{#each message.result.citations as citation}
								<details class="border border-gray-200 rounded-xl px-4 py-3 text-sm">
									<summary class="cursor-pointer font-medium"
										>[{citation.source_id}] {citation.title}</summary
									>
									<p class="text-xs text-gray-500 mt-2">{citation.heading_path}</p>
									<blockquote
										class="border-l-2 border-gray-300 pl-3 my-3 text-gray-600 whitespace-pre-wrap"
									>
										{citation.quote}
									</blockquote>
									<a
										class="underline text-xs"
										href={safeUrl(citation.url)}
										target="_blank"
										rel="noopener noreferrer">원문에서 확인 ↗</a
									>
								</details>
							{/each}
						</div>
					{/if}
				{/if}
			</div>
		</div>
	</div>
{/key}
