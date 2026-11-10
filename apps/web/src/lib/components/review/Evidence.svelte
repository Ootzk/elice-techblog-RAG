<script lang="ts">
	import type { Citation } from '#lib/api.js';
	export let sources: Record<string, unknown>[] = [];
	export let citations: Citation[] = [];
	function safeUrl(value: unknown) {
		if (typeof value !== 'string') return undefined;
		try {
			const url = new URL(value);
			return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
		} catch {
			return undefined;
		}
	}
</script>

<div class="rounded-xl bg-[#f4f7f1] p-4 text-sm leading-7 text-[#3f5840]">
	답변의 모든 중요한 사실 주장이 아래 근거 중 일부로 뒷받침되면 됩니다. 모든 근거를 사용할 필요는
	없습니다. 인용한 주장에는 그 번호의 근거가 맞는지도 확인하세요.
</div>
<div class="mt-4 space-y-3">
	{#each sources as source, i}
		{@const cited = citations.some((c) => c.source_id === i + 1)}
		<details
			class="rounded-xl border {cited ? 'border-[#adbea4] bg-[#fcfdfb]' : 'border-gray-200'}"
			open={cited}
		>
			<summary class="cursor-pointer p-4 text-sm"
				><span class="mr-2 font-semibold text-[#3f5840]">[{i + 1}]</span><span class="font-medium"
					>{String(source.title ?? `근거 ${i + 1}`)}</span
				><span
					class="ml-2 rounded-full px-2 py-1 text-[11px] {cited
						? 'bg-[#e8f0e2] text-[#3f5840]'
						: 'bg-gray-100 text-gray-500'}">{cited ? '답변에서 인용' : '생성에 제공 · 미인용'}</span
				></summary
			>
			<div class="border-t border-gray-100 px-4 pb-4 pt-3">
				{#if source.heading_path}<p class="mb-2 text-xs text-gray-500">
						{String(source.heading_path)}
					</p>{/if}
				{#if safeUrl(source.url)}<a
						class="text-xs text-[#3f5840] underline"
						href={safeUrl(source.url)}
						target="_blank"
						rel="noreferrer">원문 열기 ↗</a
					>{/if}
				<p class="mt-3 whitespace-pre-wrap break-words text-sm leading-7">
					{String(source.text ?? '')}
				</p>
			</div>
		</details>
	{:else}<p class="rounded-xl border border-dashed border-gray-200 p-5 text-sm text-gray-500">
			최종 생성에 제공한 근거가 없습니다. 사실 주장이 있는지 먼저 확인해 주세요.
		</p>{/each}
</div>
