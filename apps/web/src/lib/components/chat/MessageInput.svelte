<!-- Adapted from Open WebUI MessageInput.svelte; see UPSTREAM.md. -->
<script lang="ts">
	import Textarea from '../common/Textarea.svelte';
	export let prompt = '';
	export let disabled = false;
	export let onSubmit: () => void;
	let composing = false;
</script>

<form class="w-full max-w-3xl mx-auto px-4 pb-4" on:submit|preventDefault={onSubmit}>
	<div class="rounded-3xl border border-gray-200 bg-gray-50 px-3 pt-3 pb-2 shadow-sm">
		<div
			on:compositionstart={() => (composing = true)}
			on:compositionend={() => (composing = false)}
			role="presentation"
		>
			<Textarea
				bind:value={prompt}
				id="chat-input"
				placeholder="테크블로그에 대해 질문해 보세요"
				ariaLabel="질문"
				{disabled}
				maxSize={180}
				className="w-full bg-transparent px-2 py-1 outline-none resize-none text-base leading-7"
				onKeydown={(event) => {
					if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !composing) {
						event.preventDefault();
						if (!disabled && prompt.trim()) onSubmit();
					}
				}}
			/>
		</div>
		<div class="flex items-center justify-between mt-2 pl-2">
			<span class="text-xs text-gray-400">Shift + Enter 줄바꿈</span>
			<button
				id="send-message-button"
				aria-label="질문 보내기"
				class="bg-black text-white hover:bg-gray-800 disabled:bg-gray-200 transition rounded-full p-[0.3125rem] self-center"
				type="submit"
				disabled={disabled || !prompt.trim()}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 16 16"
					fill="currentColor"
					class="size-6"
					><path
						fill-rule="evenodd"
						d="M8 14a.75.75 0 0 1-.75-.75V4.56L4.03 7.78a.75.75 0 0 1-1.06-1.06l4.5-4.5a.75.75 0 0 1 1.06 0l4.5 4.5a.75.75 0 0 1-1.06 1.06L8.75 4.56v8.69A.75.75 0 0 1 8 14Z"
						clip-rule="evenodd"
					/></svg
				>
			</button>
		</div>
	</div>
	<p class="text-center text-xs text-gray-400 mt-2">
		답변의 출처를 확인해 주세요. 민감한 정보는 입력하지 마세요.
	</p>
</form>
