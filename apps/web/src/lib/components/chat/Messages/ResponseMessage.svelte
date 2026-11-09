<!-- Adapted from Open WebUI response layout; see UPSTREAM.md. -->
<script lang="ts">
	import { tick } from 'svelte';
	import Name from './Name.svelte';
	import Spinner from '../../common/Spinner.svelte';
	import type { Turn } from '#lib/api.js';
	import { groupSources, renderAnswer, highlightSourceUrl, sourceId } from '#lib/citations.js';

	export let message: Turn;
	let copied = false;
	let selected: number | null = null;
	let expanded: number[] = [];
	let origin: HTMLAnchorElement | null = null;
	let container: HTMLDivElement;
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
	$: sources = groupSources(message.result?.citations ?? []);
	$: html = renderAnswer(content, message.id, sources);

	function revealCitations(node: HTMLDivElement) {
		async function activate(event: MouseEvent) {
			if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
				return;
			if (!(event.target instanceof Element)) return;
			const link = event.target.closest<HTMLAnchorElement>('a[data-citation-id]');
			if (!link || !node.contains(link)) return;
			const id = Number(link.dataset.citationId);
			if (!sources.some((source) => source.source_id === id)) return;
			event.preventDefault();
			origin = link;
			selected = id;
			if (!expanded.includes(id)) expanded = [...expanded, id];
			await tick();
			const card = container.querySelector<HTMLElement>(`#${CSS.escape(sourceId(message.id, id))}`);
			card?.focus({ preventScroll: true });
			card?.scrollIntoView({ block: 'start', behavior: scrollBehavior() });
		}
		node.addEventListener('click', activate);
		return { destroy: () => node.removeEventListener('click', activate) };
	}
	function scrollBehavior(): ScrollBehavior {
		return matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth';
	}
	function backToAnswer() {
		origin?.focus({ preventScroll: true });
		origin?.scrollIntoView({ block: 'center', behavior: scrollBehavior() });
	}
	function toggle(id: number) {
		expanded = expanded.includes(id) ? expanded.filter((value) => value !== id) : [...expanded, id];
	}
</script>

{#key message.id}
	<div
		bind:this={container}
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
					<div class="markdown" use:revealCitations>{@html html}</div>
					{#if sources.length}
						<section class="mt-6" aria-label="답변의 근거가 된 출처">
							<div class="flex flex-wrap items-baseline justify-between gap-2 mb-3">
								<h4 class="text-sm font-semibold text-gray-700">
									답변에 사용한 출처 <span class="ml-1 font-normal text-gray-400"
										>{sources.length}</span
									>
								</h4>
								<p class="text-xs text-gray-500">본문의 번호를 누르면 해당 근거로 이동해요</p>
							</div>
							<div class="flex flex-col gap-3">
								{#each sources as source (source.source_id)}
									{@const url = highlightSourceUrl(source.url, source.quotes)}
									{@const open = expanded.includes(source.source_id)}
									{@const collapsible =
										source.quotes.length > 1 ||
										source.quotes[0].length > 100 ||
										source.quotes[0].includes('\n')}
									<article
										id={sourceId(message.id, source.source_id)}
										tabindex="-1"
										aria-label={`출처 ${source.source_id}: ${source.title}`}
										class="scroll-mt-20 rounded-2xl border p-4 transition-colors {selected ===
										source.source_id
											? 'border-[#8fa48f] bg-[#f5f8f3]'
											: 'border-gray-200 bg-gray-50/50'}"
									>
										<div class="flex gap-3 items-start">
											<span
												class="flex size-6 shrink-0 items-center justify-center rounded-md bg-[#e8eee5] text-xs font-semibold text-[#3f5840]"
												aria-hidden="true">{source.source_id}</span
											>
											<div class="min-w-0 flex-1">
												{#if url}<a
														href={url}
														target="_blank"
														rel="noopener noreferrer"
														class="text-sm font-medium leading-6 hover:underline"
														aria-label={`${source.title} 인용 구간으로 이동 (새 탭)`}
														>{source.title}
														<span class="text-gray-400" aria-hidden="true">↗</span></a
													>
												{:else}<p class="text-sm font-medium leading-6">{source.title}</p>{/if}
												{#if source.heading_path}<p class="mt-1 text-xs text-gray-500 break-words">
														{source.heading_path
															.replace(/^\/+/, '')
															.split('/')
															.filter(Boolean)
															.join(' › ')}
													</p>{/if}
											</div>
										</div>
										<div
											id={`${sourceId(message.id, source.source_id)}-quotes`}
											class="mt-3 border-l-2 border-[#c5d2be] pl-3"
										>
											<p class="text-[11px] font-medium text-gray-400 mb-1">원문 인용</p>
											{#each open ? source.quotes : source.quotes.slice(0, 1) as quote}
												<blockquote
													class="text-sm leading-7 text-gray-600 whitespace-pre-wrap break-words {open ||
													!collapsible
														? ''
														: 'line-clamp-3'}"
												>
													{quote}
													{#if source.quotes.length > 1 && open}
														{@const quoteUrl = highlightSourceUrl(source.url, [quote])}
														{#if quoteUrl}<a
																href={quoteUrl}
																target="_blank"
																rel="noopener noreferrer"
																class="block text-xs text-[#466047] hover:underline mt-1 mb-3"
																aria-label={`출처 ${source.source_id}의 이 인용 구간으로 이동 (새 탭)`}
																>이 구간 보기 ↗</a
															>{/if}
													{/if}
												</blockquote>
											{/each}
										</div>
										<div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
											{#if collapsible}<button
													class="text-gray-500 hover:text-gray-900 underline underline-offset-2"
													aria-expanded={open}
													aria-controls={`${sourceId(message.id, source.source_id)}-quotes`}
													on:click={() => toggle(source.source_id)}
													>{open
														? '인용문 접기'
														: `인용문 전체 보기${source.quotes.length > 1 ? ` (${source.quotes.length})` : ''}`}</button
												>{/if}
											{#if url}<a
													href={url}
													target="_blank"
													rel="noopener noreferrer"
													class="font-medium text-[#466047] hover:underline"
													aria-label={`출처 ${source.source_id} 인용 구간으로 이동 (새 탭)`}
													>원문에서 인용 구간 보기 ↗</a
												>{/if}
											{#if selected === source.source_id && origin}<button
													class="ml-auto text-gray-500 hover:text-gray-900"
													on:click={backToAnswer}>답변으로 돌아가기 ↑</button
												>{/if}
										</div>
									</article>
								{/each}
							</div>
						</section>
					{/if}
					<button
						class="text-xs text-gray-400 mt-4 hover:text-gray-700 underline"
						on:click={copyAnswer}>{copied ? '복사했습니다' : '답변 복사'}</button
					>
				{/if}
			</div>
		</div>
	</div>
{/key}
