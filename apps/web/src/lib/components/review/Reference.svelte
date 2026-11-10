<script lang="ts">
	export let value: unknown;
	function href(value: unknown) {
		if (typeof value !== 'string') return undefined;
		try {
			const url = new URL(value);
			return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
		} catch {
			return undefined;
		}
	}
	function object(value: unknown): Record<string, unknown> {
		return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
	}
</script>

{#if typeof value === 'boolean'}
	<p class="text-sm">
		{value
			? '문서 근거로 답변 가능한 질문입니다.'
			: '문서에서 충분한 답을 찾을 수 없는 질문입니다.'}
	</p>
{:else if typeof value === 'string'}
	<p class="whitespace-pre-wrap text-sm leading-7">{value}</p>
{:else if Array.isArray(value)}
	{#if !value.length}<p class="text-sm text-gray-400">별도로 지정한 항목이 없습니다.</p>
	{:else}<ul class="space-y-3">
			{#each value as item, i}<li class="flex gap-3 text-sm leading-7">
					<span class="shrink-0 text-gray-400">{i + 1}.</span>
					<div class="min-w-0 flex-1">
						{#if typeof item === 'string'}<p class="whitespace-pre-wrap">{item}</p>
						{:else if object(item).quote || object(item).url}
							{#if href(object(item).url)}<a
									class="break-all text-xs text-[#3f5840] underline"
									href={href(object(item).url)}
									target="_blank"
									rel="noreferrer">{String(object(item).title ?? '참고 원문 열기')} ↗</a
								>{/if}
							{#if object(item).quote}<blockquote
									class="mt-2 whitespace-pre-wrap border-l-2 border-[#c9d7c3] pl-3"
								>
									{String(object(item).quote)}
								</blockquote>{/if}
						{:else}<details>
								<summary>세부 항목 보기</summary>
								<pre class="mt-2 whitespace-pre-wrap break-words text-xs">{JSON.stringify(
										item,
										null,
										2
									)}</pre>
							</details>{/if}
					</div>
				</li>{/each}
		</ul>{/if}
{:else if value !== null && value !== undefined}
	<details>
		<summary class="text-sm">세부 내용 보기</summary>
		<pre class="mt-2 whitespace-pre-wrap break-words text-xs">{JSON.stringify(value, null, 2)}</pre>
	</details>
{/if}
