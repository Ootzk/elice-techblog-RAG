<!-- Pruned Open WebUI Messages.svelte conversation wrapper; see UPSTREAM.md. -->
<script lang="ts">
	import type { Turn } from '#lib/api.js';
	import ResponseMessage from './Messages/ResponseMessage.svelte';
	export let messages: Turn[] = [];
</script>

<div class="w-full pt-2">
	<section class="w-full" aria-labelledby="chat-conversation">
		<h2 class="sr-only" id="chat-conversation">대화</h2>
		{#each messages as message (message.id)}
			<div class="flex justify-end mb-7 mt-7">
				<p class="bg-gray-100 rounded-3xl px-5 py-3 max-w-[90%] whitespace-pre-wrap break-words">
					{message.question}
				</p>
			</div>
			<div class="mb-10"><ResponseMessage {message} /></div>
		{/each}
	</section>
</div>
